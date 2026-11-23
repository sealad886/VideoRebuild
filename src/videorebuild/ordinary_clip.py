"""Project independently selected samples into a local video-only MP4 timeline."""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate
import struct

from .bmff import FormatError, Movie, boxes, children, make_box, one, parse_movie, table


def _audited_children(data, parent, allowed):
    items = children(data, parent)
    kinds = {child.kind for child in items}
    if len(kinds) != len(items) or kinds - allowed:
        raise FormatError(f'unsupported clip tables in {parent.kind.decode("ascii")}')


def _duration_box(data, box, duration, offset, width, minimum):
    payload = bytearray(data[box.payload:box.end])
    if len(payload) < minimum or not 0 <= duration < 1 << (8 * width):
        raise FormatError(f'clip duration does not fit {box.kind.decode("ascii")}')
    payload[offset:offset+width] = duration.to_bytes(width, 'big')
    return make_box(box.kind, payload)


def _version(data, box):
    if box.end-box.payload < 4 or data[box.payload] not in (0, 1):
        raise FormatError(f'unsupported clip {box.kind.decode("ascii")} version')
    return data[box.payload]


class _SampleRuns:
    def __init__(self, rows):
        self.rows = rows
        self.ends = tuple(accumulate(count for count, _ in rows))

    def slice(self, first, stop):
        index = bisect_right(self.ends, first)
        while first < stop:
            end = min(self.ends[index], stop)
            yield end-first, self.rows[index][1]
            first = end
            index += 1


class OrdinaryClip:
    """Read an audited source timeline and project a half-open decode-order span."""

    def __init__(self, movie: Movie):
        videos = [track for track in movie.tracks if track.handler == b'vide']
        if len(videos) != 1 or videos[0].codec not in {b'avc1', b'hvc1'}:
            raise FormatError('clip requires one AVC1 or HVC1 video track')
        self.track = videos[0]
        self.data = data = movie.data
        self.source_digest = movie.digest
        self.metadata_bytes = len(data)
        root = next(boxes(data))
        if any(child.kind == b'mvex' for child in children(data, root)):
            raise FormatError('fragmented container is not an ordinary clip source')
        self.mvhd = one(data, root, b'mvhd')
        trak = [child for child in children(data, root) if child.kind == b'trak'][self.track.index]
        _audited_children(data, trak, {b'tkhd', b'edts', b'mdia', b'udta', b'meta'})
        self.tkhd = one(data, trak, b'tkhd')
        self.mdia = one(data, trak, b'mdia')
        _audited_children(data, self.mdia, {b'mdhd', b'hdlr', b'minf', b'elng'})
        self.mdhd = one(data, self.mdia, b'mdhd')
        self.minf = one(data, self.mdia, b'minf')
        _audited_children(data, self.minf, {b'vmhd', b'dinf', b'stbl'})
        one(data, self.minf, b'vmhd')
        stbl = one(data, self.minf, b'stbl')
        _audited_children(data, stbl, {b'stsd', b'stts', b'ctts', b'stss', b'stsc',
                                       b'stsz', b'stz2', b'stco', b'co64', b'sdtp'})
        entries = {child.kind: child for child in children(data, stbl)}
        self.stsd = entries[b'stsd']
        if data[self.stsd.payload:self.stsd.payload+4] != b'\0'*4:
            raise FormatError('unsupported clip stsd version or flags')
        self.decode_runs = _SampleRuns(table(data, entries[b'stts'], 8, '>II'))
        self.decode_ends = tuple(accumulate(count*duration for count, duration in self.decode_runs.rows))
        self.composition_runs = None
        self.composition_version = 0
        if ctts := entries.get(b'ctts'):
            self.composition_version = _version(data, ctts)
            if data[ctts.payload+1:ctts.payload+4] != b'\0'*3:
                raise FormatError('unsupported clip ctts flags')
            # parse_movie already checked the full ctts bounds and total sample count.
            fmt = '>Ii' if self.composition_version else '>II'
            self.composition_runs = _SampleRuns(list(struct.iter_unpack(fmt, data[ctts.payload+8:ctts.end])))
        sync = entries.get(b'stss')
        self.keyframes = (tuple(sample-1 for sample, in table(data, sync, 4, '>I'))
                          if sync is not None else range(self.track.sample_count))
        self.dependencies = entries.get(b'sdtp')
        if self.dependencies is not None:
            start = self.dependencies.payload
            if (data[start:start+4] != b'\0'*4
                    or self.dependencies.end-start != 4+self.track.sample_count):
                raise FormatError('invalid clip sample dependency table')
        self.mvhd_version = _version(data, self.mvhd)
        self.tkhd_version = _version(data, self.tkhd)
        self.mdhd_version = _version(data, self.mdhd)
        scale_offset = self.mvhd.payload + (20 if self.mvhd_version else 12)
        if scale_offset+4 > self.mvhd.end:
            raise FormatError('truncated clip mvhd')
        self.movie_timescale = struct.unpack_from('>I', data, scale_offset)[0]
        if not self.movie_timescale:
            raise FormatError('zero clip movie timescale')
        self._chunk_prefixes = {}

    def decode_start(self, first: int) -> int:
        if not 0 <= first <= self.track.sample_count:
            raise FormatError('clip sample index out of bounds')
        index = bisect_right(self.decode_runs.ends, first)
        samples = self.decode_runs.ends[index-1] if index else 0
        ticks = self.decode_ends[index-1] if index else 0
        if samples == first:
            return ticks
        return ticks + (first-samples)*self.decode_runs.rows[index][1]

    def _sample_tables(self, first, stop):
        sizes = bytearray()
        offsets = bytearray()
        chunk_runs = []
        index = bisect_right(self.track.chunks, first, key=lambda chunk: chunk.sample_start)-1
        chunk_count = 0
        while index < len(self.track.chunks):
            chunk = self.track.chunks[index]
            if chunk.sample_start >= stop:
                break
            lo = max(0, first-chunk.sample_start)
            hi = min(len(chunk.sizes), stop-chunk.sample_start)
            count = hi-lo
            offset = chunk.offset
            if lo:
                prefix = self._chunk_prefixes.get(chunk.index)
                if prefix is None:
                    prefix = array('Q', accumulate(chunk.sizes, initial=0))
                    self._chunk_prefixes[chunk.index] = prefix
                offset += prefix[lo]
            if offset >= 1 << 64:
                raise FormatError('clip chunk offset overflow')
            sizes.extend(struct.pack(f'>{count}I', *chunk.sizes[lo:hi]))
            offsets.extend(struct.pack('>Q', offset))
            chunk_count += 1
            if not chunk_runs or chunk_runs[-1][1] != count:
                chunk_runs.append((chunk_count, count, 1))
            index += 1
        return (
            make_box(b'stsc', b'\0'*4 + struct.pack('>I', len(chunk_runs))
                     + b''.join(struct.pack('>III', *row) for row in chunk_runs)),
            make_box(b'stsz', b'\0'*8 + struct.pack('>I', stop-first) + sizes),
            make_box(b'co64', b'\0'*4 + struct.pack('>I', chunk_count) + offsets),
        )

    def project(self, first: int, stop: int, *, sync_samples=None) -> Movie:
        if type(first) is not int or type(stop) is not int or not 0 <= first < stop <= self.track.sample_count:
            raise FormatError('clip sample range out of bounds')
        sync_first = bisect_left(self.keyframes, first)
        if sync_first == len(self.keyframes) or self.keyframes[sync_first] != first:
            raise FormatError('clip start is not a nominated sync sample')
        data = self.data
        start_tick = self.decode_start(first)
        decode_duration = self.decode_start(stop)-start_tick
        presentation_end = decode_duration
        stts = list(self.decode_runs.slice(first, stop))
        timing = [make_box(b'stts', b'\0'*4 + struct.pack('>I', len(stts))
                           + b''.join(struct.pack('>II', *row) for row in stts))]
        if self.composition_runs is not None:
            ctts = list(self.composition_runs.slice(first, stop))
            index = first
            for count, offset in ctts:
                if self.decode_start(index)-start_tick+offset < 0:
                    raise FormatError('clip sample presentation would begin before its local timeline')
                index += count
                presentation_end = max(presentation_end, self.decode_start(index)-start_tick+offset)
            fmt = '>Ii' if self.composition_version else '>II'
            timing.append(make_box(b'ctts', bytes([self.composition_version])+b'\0'*3
                                   + struct.pack('>I', len(ctts))
                                   + b''.join(struct.pack(fmt, *row) for row in ctts)))
        if sync_samples is not None:
            sync = tuple(sync_samples)
            if not sync or sync[0] != first or sync[-1] >= stop or any(a >= b for a, b in zip(sync, sync[1:])):
                raise FormatError('invalid verified clip sync points')
        elif not isinstance(self.keyframes, range):
            sync_stop = bisect_left(self.keyframes, stop, lo=sync_first)
            sync = self.keyframes[sync_first:sync_stop]
        else:
            sync = ()
        if sync:
            timing.append(make_box(b'stss', b'\0'*4 + struct.pack('>I', len(sync))
                                   + b''.join(struct.pack('>I', sample-first+1) for sample in sync)))
        if self.dependencies is not None:
            start = self.dependencies.payload+4
            timing.append(make_box(b'sdtp', b'\0'*4 + data[start+first:start+stop]))
        stbl = make_box(b'stbl', data[self.stsd.start:self.stsd.end] + b''.join(timing)
                        + b''.join(self._sample_tables(first, stop)))
        movie_duration = (presentation_end*self.movie_timescale+self.track.timescale-1)//self.track.timescale
        mvhd = _duration_box(data, self.mvhd, movie_duration, 24, 8, 112) if self.mvhd_version else (
            _duration_box(data, self.mvhd, movie_duration, 16, 4, 100))
        tkhd = _duration_box(data, self.tkhd, movie_duration, 28, 8, 96) if self.tkhd_version else (
            _duration_box(data, self.tkhd, movie_duration, 20, 4, 84))
        mdhd = _duration_box(data, self.mdhd, decode_duration, 24, 8, 36) if self.mdhd_version else (
            _duration_box(data, self.mdhd, decode_duration, 16, 4, 24))
        minf = make_box(b'minf', b''.join(stbl if child.kind == b'stbl' else data[child.start:child.end]
                                        for child in children(data, self.minf)))
        mdia = make_box(b'mdia', b''.join(
            mdhd if child.kind == b'mdhd' else minf if child.kind == b'minf' else data[child.start:child.end]
            for child in children(data, self.mdia)))
        return parse_movie(make_box(b'moov', mvhd + make_box(b'trak', tkhd + mdia)))

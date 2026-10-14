"""Bounded ISO BMFF parser and chunk-offset rewriter.

This is deliberately not a general-purpose media parser. FFmpeg handles decoding.
We parse only the structural evidence needed to match and relocate AVC/HEVC/AAC
samples. Unknown offset-bearing/encrypted layouts fail closed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import struct
from typing import Iterator

MAX_METADATA = 64 * 1024 * 1024
MAX_SAMPLES = 2_000_000
MAX_TRACKS = 16
CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'dinf', b'edts'}
UNSAFE = {b'cmov', b'mvex', b'saio', b'saiz', b'senc', b'sinf', b'ref ', b'rmra'}
VIDEO_CODECS = {b'avc1', b'avc3', b'hvc1', b'hev1'}


class FormatError(ValueError):
    """Malformed or intentionally unsupported metadata, never a silent fallback."""


@dataclass(frozen=True)
class Box:
    start: int
    end: int
    kind: bytes
    header: int = 8

    @property
    def payload(self) -> int:
        return self.start + self.header


def boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Walk exact sibling bounds; reject truncation, overflow, and stray bytes."""
    end = len(data) if end is None else end
    if not 0 <= start <= end <= len(data):
        raise FormatError('invalid box range')
    pos = start
    while pos < end:
        if end - pos < 8:
            raise FormatError(f'trailing bytes at {pos}')
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise FormatError('truncated extended box')
            size = struct.unpack_from('>Q', data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or size > end - pos:
            raise FormatError(f'invalid {kind!r} size {size} at {pos}')
        yield Box(pos, pos + size, kind, header)
        pos += size


def make_box(kind: bytes, payload: bytes) -> bytes:
    if len(payload) + 8 > 0xFFFFFFFF:
        return struct.pack('>I4sQ', 1, kind, len(payload) + 16) + payload
    return struct.pack('>I4s', len(payload) + 8, kind) + payload


def children(data: bytes, parent: Box) -> list[Box]:
    return list(boxes(data, parent.payload, parent.end))


def one(data: bytes, parent: Box, kind: bytes) -> Box:
    matches = [b for b in children(data, parent) if b.kind == kind]
    if len(matches) != 1:
        raise FormatError(f'expected one {kind.decode("ascii", "replace")}, found {len(matches)}')
    return matches[0]


def path(data: bytes, parent: Box, *kinds: bytes) -> Box:
    for kind in kinds:
        parent = one(data, parent, kind)
    return parent


def u32(data: bytes, at: int, end: int) -> int:
    if at < 0 or at + 4 > end:
        raise FormatError('truncated integer')
    return struct.unpack_from('>I', data, at)[0]


def table(data: bytes, box: Box, width: int, fmt: str) -> list[tuple[int, ...]]:
    p = box.payload
    if data[p:p+4] != b'\0\0\0\0':
        raise FormatError(f'unsupported {box.kind!r} version/flags')
    count = u32(data, p + 4, box.end)
    if count > MAX_SAMPLES or p + 8 + count * width != box.end:
        raise FormatError(f'inconsistent or oversized {box.kind!r} table')
    return list(struct.iter_unpack(fmt, data[p+8:box.end]))


@dataclass(frozen=True)
class Chunk:
    track: int
    index: int
    offset: int
    sizes: tuple[int, ...]
    sample_start: int

    @property
    def size(self) -> int:
        return sum(self.sizes)


@dataclass(frozen=True)
class Track:
    index: int
    track_id: int
    handler: bytes
    codec: bytes
    nal_length: int
    sample_count: int
    timescale: int
    duration: int
    chunks: tuple[Chunk, ...]
    config_hash: str


@dataclass(frozen=True)
class Movie:
    data: bytes
    tracks: tuple[Track, ...]
    chunks: tuple[Chunk, ...]
    digest: str

    @property
    def span(self) -> int:
        return max(c.offset + c.size for c in self.chunks) - self.chunks[0].offset

    @property
    def media_size(self) -> int:
        return sum(c.size for c in self.chunks)

    def summary(self) -> dict:
        return {'sha256': self.digest, 'span': self.span, 'media_size': self.media_size,
                'chunks': len(self.chunks), 'tracks': [
                    {'index': t.index, 'id': t.track_id, 'codec': t.codec.decode(),
                     'samples': t.sample_count, 'duration': t.duration / t.timescale,
                     'timescale': t.timescale, 'config_sha256': t.config_hash}
                    for t in self.tracks]}


def _check_tree(data: bytes, parent: Box, depth: int = 0) -> None:
    if depth > 12:
        raise FormatError('metadata nesting too deep')
    for b in children(data, parent):
        if b.kind in UNSAFE:
            raise FormatError(f'unsupported layout: {b.kind.decode("ascii", "replace")}')
        if b.kind in CONTAINERS:
            _check_tree(data, b, depth + 1)


def _sample_sizes(data: bytes, stbl: Box) -> list[int]:
    choices = [b for b in children(data, stbl) if b.kind in {b'stsz', b'stz2'}]
    if len(choices) != 1:
        raise FormatError('expected one sample-size table')
    b = choices[0]
    p = b.payload
    if data[p:p+4] != b'\0' * 4:
        raise FormatError('unsupported sample-size version')
    count = u32(data, p + 8, b.end)
    if not 0 < count <= MAX_SAMPLES:
        raise FormatError('empty or oversized sample-size table')
    if b.kind == b'stsz':
        fixed = u32(data, p + 4, b.end)
        expected = p + 12 + (0 if fixed else 4 * count)
        if expected != b.end:
            raise FormatError('inconsistent stsz size')
        sizes = [fixed] * count if fixed else [x[0] for x in struct.iter_unpack('>I', data[p+12:b.end])]
    else:
        width = data[p + 7]
        if width not in (4, 8, 16) or p + 12 + (count * width + 7)//8 != b.end:
            raise FormatError('invalid stz2 size')
        raw = data[p+12:b.end]
        if width == 4:
            sizes = [((raw[i//2] >> 4) if i % 2 == 0 else (raw[i//2] & 15)) for i in range(count)]
        elif width == 8:
            sizes = list(raw)
        else:
            sizes = [x[0] for x in struct.iter_unpack('>H', raw)]
    if any(n <= 0 or n > 64 * 1024 * 1024 for n in sizes):
        raise FormatError('zero or oversized sample')
    return sizes


def parse_movie(data: bytes) -> Movie:
    if len(data) > MAX_METADATA:
        raise FormatError('moov exceeds metadata limit')
    roots = list(boxes(data))
    if len(roots) != 1 or roots[0].kind != b'moov':
        raise FormatError('expected a single complete moov box')
    root = roots[0]
    _check_tree(data, root)
    traks = [b for b in children(data, root) if b.kind == b'trak']
    if not 0 < len(traks) <= MAX_TRACKS:
        raise FormatError('missing or excessive tracks')
    tracks = []
    all_chunks = []
    ids = set()
    for ti, trak in enumerate(traks):
        tkhd = one(data, trak, b'tkhd')
        version = data[tkhd.payload] if tkhd.end > tkhd.payload else -1
        if version not in (0, 1):
            raise FormatError('unsupported tkhd version')
        track_id = u32(data, tkhd.payload + (12 if version == 0 else 20), tkhd.end)
        if not track_id or track_id in ids:
            raise FormatError('invalid or duplicate track id')
        ids.add(track_id)
        mdia = one(data, trak, b'mdia')
        hdlr = one(data, mdia, b'hdlr')
        handler = data[hdlr.payload+8:hdlr.payload+12]
        if handler not in (b'vide', b'soun'):
            raise FormatError(f'unsupported track handler {handler!r}; no tracks are silently dropped')
        mdhd = one(data, mdia, b'mdhd')
        p = mdhd.payload
        if p >= mdhd.end or data[p] not in (0, 1):
            raise FormatError('unsupported mdhd')
        tp = p + (12 if data[p] == 0 else 20)
        timescale = u32(data, tp, mdhd.end)
        if not timescale:
            raise FormatError('zero timescale')
        # Duration is validated from stts below, not trusted from the header.
        minf = one(data, mdia, b'minf')
        dref = path(data, minf, b'dinf', b'dref')
        dp = dref.payload
        if u32(data, dp + 4, dref.end) != 1:
            raise FormatError('multiple/external data references are unsupported')
        refs = list(boxes(data, dp+8, dref.end))
        if (len(refs) != 1 or refs[0].kind != b'url ' or
                data[refs[0].payload:refs[0].end] != b'\0\0\0\1'):
            raise FormatError('external data reference rejected')
        stbl = one(data, minf, b'stbl')
        stsd = one(data, stbl, b'stsd')
        sp = stsd.payload
        if u32(data, sp+4, stsd.end) != 1:
            raise FormatError('multiple sample descriptions are unsupported')
        entries = list(boxes(data, sp+8, stsd.end))
        if len(entries) != 1:
            raise FormatError('invalid stsd')
        entry = entries[0]
        ep = entry.payload
        if ep + 8 > entry.end or struct.unpack_from('>H', data, ep+6)[0] != 1:
            raise FormatError('unsupported sample data reference')
        codec = entry.kind
        nal_length = 0
        config_hash = ''
        if handler == b'vide':
            if codec not in VIDEO_CODECS:
                raise FormatError(f'unsupported video codec {codec!r}')
            subs = list(boxes(data, ep + 78, entry.end))
            if any(b.kind == b'sinf' for b in subs):
                raise FormatError('encrypted video rejected')
            config_type = b'avcC' if codec in {b'avc1', b'avc3'} else b'hvcC'
            configs = [b for b in subs if b.kind == config_type]
            if len(configs) != 1:
                raise FormatError('missing codec configuration')
            cfg = data[configs[0].payload:configs[0].end]
            ix = 4 if config_type == b'avcC' else 21
            if len(cfg) <= ix or cfg[0] != 1:
                raise FormatError('invalid codec configuration')
            nal_length = (cfg[ix] & 3) + 1
            if nal_length == 3:
                raise FormatError('reserved NAL length size')
            config_hash = hashlib.sha256(cfg).hexdigest()
        else:
            if codec != b'mp4a':
                raise FormatError(f'unsupported audio codec {codec!r}')
            # Version 0 MPEG-4 AudioSampleEntry; later QuickTime layouts are not guessed.
            if ep + 28 > entry.end or data[ep+8:ep+10] != b'\0\0':
                raise FormatError('unsupported audio sample-entry version')
            asubs = list(boxes(data, ep+28, entry.end))
            if any(b.kind == b'sinf' for b in asubs):
                raise FormatError('encrypted audio rejected')
            config_hash = hashlib.sha256(data[ep+28:entry.end]).hexdigest()
        sizes = _sample_sizes(data, stbl)
        sc = table(data, one(data, stbl, b'stsc'), 12, '>III')
        if not sc or sc[0][0] != 1 or any(n < 1 or desc != 1 for _, n, desc in sc):
            raise FormatError('invalid sample-to-chunk table')
        if any(a[0] >= b[0] for a, b in zip(sc, sc[1:])):
            raise FormatError('nonincreasing stsc runs')
        offset_boxes = [b for b in children(data, stbl) if b.kind in {b'stco', b'co64'}]
        if len(offset_boxes) != 1:
            raise FormatError('expected one chunk offset table')
        ob = offset_boxes[0]
        offsets = [x[0] for x in table(data, ob, 4 if ob.kind == b'stco' else 8,
                                      '>I' if ob.kind == b'stco' else '>Q')]
        if not offsets or sc[-1][0] > len(offsets):
            raise FormatError('invalid chunk count')
        tt = table(data, one(data, stbl, b'stts'), 8, '>II')
        if sum(n for n, _ in tt) != len(sizes) or any(n < 1 or dur < 1 for n, dur in tt):
            raise FormatError('inconsistent sample timing')
        for b in children(data, stbl):
            if b.kind == b'ctts':
                cp = b.payload
                count = u32(data, cp+4, b.end)
                if (data[cp] not in (0, 1) or count > MAX_SAMPLES or
                        cp + 8 + count * 8 != b.end):
                    raise FormatError('invalid ctts')
                if sum(n for n, _ in struct.iter_unpack('>II', data[cp+8:b.end])) != len(sizes):
                    raise FormatError('inconsistent composition timing')
            elif b.kind == b'stss':
                sync = [x[0] for x in table(data, b, 4, '>I')]
                if any(n < 1 or n > len(sizes) for n in sync) or sync != sorted(set(sync)):
                    raise FormatError('invalid sync-sample table')
        duration = sum(n * d for n, d in tt)
        chunks = []
        si = run = 0
        for ci, off in enumerate(offsets):
            while run + 1 < len(sc) and sc[run+1][0] <= ci+1:
                run += 1
            count = sc[run][1]
            if si + count > len(sizes):
                raise FormatError('chunk consumes nonexistent samples')
            chunk = Chunk(ti, ci, off, tuple(sizes[si:si+count]), si)
            chunks.append(chunk)
            si += count
        if si != len(sizes):
            raise FormatError('unassigned samples')
        tracks.append(Track(ti, track_id, handler, codec, nal_length, len(sizes),
                            timescale, duration, tuple(chunks), config_hash))
        all_chunks.extend(chunks)
    if not any(t.handler == b'vide' for t in tracks):
        raise FormatError('no supported video track')
    all_chunks.sort(key=lambda c: c.offset)
    if any(a.offset+a.size > b.offset for a, b in zip(all_chunks, all_chunks[1:])):
        raise FormatError('overlapping source chunk references')
    return Movie(data, tuple(tracks), tuple(all_chunks), hashlib.sha256(data).hexdigest())


def rewrite_offsets(movie: Movie, offsets: dict[tuple[int, int], int]) -> bytes:
    """Rebuild ancestors and promote every stco to co64 (no 4-GiB overflow)."""
    ti = -1

    def rebuild(b: Box, track: int = -1) -> bytes:
        nonlocal ti
        if b.kind == b'trak':
            ti += 1
            track = ti
        if b.kind in {b'stco', b'co64'}:
            count = len(movie.tracks[track].chunks)
            vals = [offsets[(track, ci)] for ci in range(count)]
            if any(x < 0 or x > 0xFFFFFFFFFFFFFFFF for x in vals):
                raise FormatError('rewritten offset out of bounds')
            return make_box(b'co64', b'\0'*4 + struct.pack('>I', count) +
                            b''.join(struct.pack('>Q', x) for x in vals))
        if b.kind in CONTAINERS:
            return make_box(b.kind, b''.join(rebuild(c, track) for c in children(movie.data, b)))
        return movie.data[b.start:b.end]

    return rebuild(next(boxes(movie.data)))


def valid_sample(data: bytes, codec: bytes, length_size: int) -> bool:
    """Validate *all* NAL boundaries and basic headers, not merely the first NAL."""
    p = count = 0
    has_vcl = False
    while p < len(data):
        if p + length_size > len(data):
            return False
        n = int.from_bytes(data[p:p+length_size], 'big')
        p += length_size
        if n < (2 if codec in {b'hvc1', b'hev1'} else 1) or p+n > len(data):
            return False
        h = data[p]
        if h & 0x80:
            return False
        if codec in {b'avc1', b'avc3'}:
            typ = h & 31
            if not 1 <= typ <= 23:
                return False
            has_vcl |= typ in (1, 2, 3, 4, 5)
        elif codec in {b'hvc1', b'hev1'}:
            typ = (h >> 1) & 63
            if typ > 40 or not (data[p+1] & 7):
                return False
            has_vcl |= typ <= 31
        else:
            return False
        p += n
        count += 1
        if count > 65536:
            return False
    return p == len(data) and count > 0 and has_vcl

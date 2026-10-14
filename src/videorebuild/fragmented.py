"""Evidence-driven fragmented-MP4 parsing; no sector-order guessing.

This backend accepts empty initialization sample tables and fragment-relative
sample addressing. It deliberately permits absent tfdt: for an unbroken stream
starting at its initialization, durations accumulate separately for each track.
It is not a browser MSE conformance checker. Unsupported addressing, encryption,
missing predecessors, overlapping payloads, and discontinuities fail closed.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import struct

from .bmff import Box, FormatError, boxes, children, one, path, u32

MAX_INIT = 1024 * 1024
MAX_MOOF = 4 * 1024 * 1024
MAX_SAMPLES = 200_000
# Operational retention budget across all fragments, not a format limit.
MAX_PREFIX_SAMPLES = 200_000


@dataclass(frozen=True)
class FragmentTrack:
    track_id: int
    handler: str
    codec: str
    timescale: int
    nal_length: int
    default_duration: int
    default_size: int
    default_flags: int
    config_sha256: str
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class Initialization:
    data: bytes
    tracks: dict[int, FragmentTrack]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class FragmentSample:
    track_id: int
    offset: int
    size: int
    duration: int
    flags: int
    composition_offset: int
    nal_types: tuple[int, ...] = ()


@dataclass(frozen=True)
class Fragment:
    start: int
    end: int
    sequence: int
    samples: tuple[FragmentSample, ...]
    decode_times: dict[int, int | None]


@dataclass
class Prefix:
    initialization: Initialization
    end: int
    fragments: list[Fragment] = field(default_factory=list)
    stop_reason: str = 'end_of_window'
    stop_offset: int = 0
    clocks: dict[int, int] = field(default_factory=dict)
    starts: dict[int, int] = field(default_factory=dict)

    @property
    def counts(self) -> dict[int, int]:
        return dict(Counter(s.track_id for f in self.fragments for s in f.samples))

    def eligibility_error(self) -> str | None:
        if not self.fragments:
            return 'no_complete_fragment_after_initialization'
        first = {}
        for f in self.fragments:
            for s in f.samples:
                first.setdefault(s.track_id, s)
        for tid, track in self.initialization.tracks.items():
            if tid not in first:
                return f'no_samples_for_track_{tid}; refusing_to_drop_a_track'
            if track.handler == 'vide':
                s = first[tid]
                # CRA can require handling leading pictures; not claimed here.
                idrs = {5} if track.codec in ('avc1', 'avc3') else {19, 20}
                if not idrs.intersection(s.nal_types) or s.flags & 0x10000:
                    return f'first_video_sample_is_not_an_IDR_track_{tid}'
        return None


def box_at(data: bytes, pos: int, limit: int | None = None) -> Box:
    """Read one finite, explicit-length box without trusting a enclosing carve."""
    end = len(data) if limit is None else limit
    if not 0 <= pos <= end <= len(data) or end - pos < 8:
        raise FormatError(f'truncated_box_header_at_{pos}')
    size, typ = struct.unpack_from('>I4s', data, pos)
    head = 8
    if size == 1:
        if end - pos < 16:
            raise FormatError(f'truncated_extended_header_at_{pos}')
        size = struct.unpack_from('>Q', data, pos + 8)[0]
        head = 16
    if size == 0:
        raise FormatError(f'open_ended_{typ!r}_at_{pos}_requires_an_original_file_boundary')
    if size < head or size > end - pos:
        raise FormatError(f'truncated_or_invalid_{typ!r}_at_{pos}_declares_{size}_available_{end-pos}')
    return Box(pos, pos + size, typ, head)


def _full(data: bytes, b: Box, versions=(0,)) -> tuple[int, int, int]:
    n = u32(data, b.payload, b.end)
    version, flags = n >> 24, n & 0xffffff
    if version not in versions:
        raise FormatError(f'unsupported_{b.kind!r}_version_{version}')
    return version, flags, b.payload + 4


def parse_initialization(data: bytes) -> Initialization:
    if not 16 <= len(data) <= MAX_INIT:
        raise FormatError('initialization_size_outside_limit')
    roots = list(boxes(data))
    if len(roots) != 2 or [b.kind for b in roots] != [b'ftyp', b'moov']:
        raise FormatError('expected_exact_ftyp_plus_moov_initialization')
    ftyp, moov = roots
    if ftyp.end - ftyp.payload < 8 or (ftyp.end-ftyp.payload) % 4:
        raise FormatError('invalid_ftyp')
    mvex = one(data, moov, b'mvex')
    defaults = {}
    for b in children(data, mvex):
        if b.kind == b'mehd':
            continue
        if b.kind != b'trex':
            raise FormatError(f'unsupported_mvex_child_{b.kind!r}')
        _, flags, p = _full(data, b)
        if flags or b.end - p != 20:
            raise FormatError('invalid_trex')
        tid, desc, dur, size, sflags = struct.unpack_from('>IIIII', data, p)
        if not tid or tid in defaults or desc != 1:
            raise FormatError('invalid_trex_track_or_description')
        defaults[tid] = (dur, size, sflags)
    tracks = {}
    for trak in (b for b in children(data, moov) if b.kind == b'trak'):
        if any(b.kind in (b'edts', b'tref') for b in children(data, trak)):
            raise FormatError('edit_lists_and_track_references_not_supported_in_fragmented_backend')
        tkhd = one(data, trak, b'tkhd')
        version, _, _ = _full(data, tkhd, (0, 1))
        tid = u32(data, tkhd.payload + (12 if version == 0 else 20), tkhd.end)
        if tid not in defaults or tid in tracks:
            raise FormatError('missing_trex_or_duplicate_track')
        mdia = one(data, trak, b'mdia')
        mdhd = one(data, mdia, b'mdhd')
        version, _, _ = _full(data, mdhd, (0, 1))
        timescale = u32(data, mdhd.payload + (12 if version == 0 else 20), mdhd.end)
        if not timescale:
            raise FormatError('zero_timescale')
        hdlr = one(data, mdia, b'hdlr')
        handler = data[hdlr.payload+8:hdlr.payload+12].decode('ascii', 'replace')
        if handler not in ('vide', 'soun'):
            raise FormatError('unsupported_track_handler')
        minf = one(data, mdia, b'minf')
        dref = path(data, minf, b'dinf', b'dref')
        if u32(data, dref.payload, dref.end) or u32(data, dref.payload+4, dref.end) != 1:
            raise FormatError('invalid_data_reference_count')
        refs = list(boxes(data, dref.payload+8, dref.end))
        if (len(refs) != 1 or refs[0].kind != b'url ' or
                data[refs[0].payload:refs[0].end] != b'\0\0\0\1'):
            raise FormatError('external_data_reference_rejected')
        stbl = one(data, minf, b'stbl')
        for typ in (b'stts', b'stsc', b'stco'):
            b = one(data, stbl, typ)
            if data[b.payload:b.end] != b'\0' * 8:
                raise FormatError('initialization_has_ordinary_samples')
        stsz = one(data, stbl, b'stsz')
        if data[stsz.payload:stsz.end] != b'\0' * 12:
            raise FormatError('initialization_has_ordinary_sample_sizes')
        stsd = one(data, stbl, b'stsd')
        if u32(data, stsd.payload, stsd.end) or u32(data, stsd.payload+4, stsd.end) != 1:
            raise FormatError('multiple_or_invalid_sample_descriptions')
        entries = list(boxes(data, stsd.payload+8, stsd.end))
        if len(entries) != 1:
            raise FormatError('invalid_sample_entry')
        e = entries[0]
        ep = e.payload
        if e.end - ep < 8 or data[ep+6:ep+8] != b'\0\1':
            raise FormatError('invalid_sample_data_reference')
        codec = e.kind.decode('ascii', 'replace')
        width = height = nlen = 0
        if handler == 'vide':
            if codec not in ('hvc1', 'hev1', 'avc1', 'avc3') or e.end-ep < 78:
                raise FormatError('unsupported_video_sample_entry')
            width, height = struct.unpack_from('>HH', data, ep+24)
            cb = list(boxes(data, ep+78, e.end))
            ctyp = b'hvcC' if codec in ('hvc1', 'hev1') else b'avcC'
            config = [c for c in cb if c.kind == ctyp]
            if len(config) != 1 or any(c.kind == b'sinf' for c in cb):
                raise FormatError('missing_codec_config_or_encryption')
            cfg = data[config[0].payload:config[0].end]
            ix = 21 if ctyp == b'hvcC' else 4
            if len(cfg) <= ix or cfg[0] != 1:
                raise FormatError('invalid_codec_config')
            nlen = (cfg[ix] & 3)+1
            if nlen == 3:
                raise FormatError('reserved_NAL_length_size')
        else:
            if codec != 'mp4a' or e.end-ep < 28 or data[ep+8:ep+10] != b'\0\0':
                raise FormatError('unsupported_audio_entry')
            cb = list(boxes(data, ep+28, e.end))
            if len([c for c in cb if c.kind == b'esds']) != 1 or any(c.kind == b'sinf' for c in cb):
                raise FormatError('missing_audio_config_or_encryption')
            cfg = data[ep+28:e.end]
        tracks[tid] = FragmentTrack(tid, handler, codec, timescale, nlen,
                                    *defaults[tid], hashlib.sha256(cfg).hexdigest(), width, height)
    if not 1 <= len(tracks) <= 16 or set(defaults) != set(tracks):
        raise FormatError('invalid_track_set')
    if not any(t.handler == 'vide' for t in tracks.values()):
        raise FormatError('video_track_required')
    return Initialization(data, tracks)


def nal_types(data: bytes, offset: int, size: int, track: FragmentTrack) -> tuple[int, ...]:
    if track.handler != 'vide':
        return ()
    p, end, result = offset, offset+size, []
    while p < end:
        if p + track.nal_length > end:
            raise FormatError('truncated_NAL_length')
        n = int.from_bytes(data[p:p+track.nal_length], 'big')
        p += track.nal_length
        hevc = track.codec in ('hvc1', 'hev1')
        if n < (2 if hevc else 1) or p+n > end:
            raise FormatError('invalid_NAL_length')
        if data[p] & 128 or (hevc and not data[p+1] & 7):
            raise FormatError('invalid_NAL_header')
        typ = ((data[p] >> 1) & 63) if hevc else (data[p] & 31)
        if typ > (40 if hevc else 23) or (not hevc and typ == 0):
            raise FormatError('unsupported_NAL_type')
        result.append(typ)
        if len(result) > 4096:
            raise FormatError('too_many_NALs_in_sample')
        p += n
    if not result:
        raise FormatError('empty_video_sample')
    return tuple(result)


def parse_fragment(data: bytes, pos: int, init: Initialization, *, sample_budget: int | None = None) -> Fragment:
    moof = box_at(data, pos)
    if moof.kind != b'moof' or moof.end-moof.start > MAX_MOOF:
        raise FormatError('expected_bounded_moof')
    mdat = box_at(data, moof.end)
    if mdat.kind != b'mdat':
        raise FormatError('expected_mdat_immediately_after_moof')
    roots = children(data, moof)
    if any(b.kind not in (b'mfhd', b'traf') for b in roots):
        raise FormatError('unsupported_moof_child')
    mfhd = one(data, moof, b'mfhd')
    _, flags, p = _full(data, mfhd)
    if flags or p+4 != mfhd.end:
        raise FormatError('invalid_mfhd')
    seq = u32(data, p, mfhd.end)
    trafs = [b for b in roots if b.kind == b'traf']
    if not 1 <= len(trafs) <= len(init.tracks):
        raise FormatError('invalid_traf_count')
    samples, decode_times = [], {}
    for traf in trafs:
        subs = children(data, traf)
        if any(b.kind not in (b'tfhd', b'tfdt', b'trun') for b in subs):
            raise FormatError('unsupported_or_encrypted_traf_child')
        tfhd = one(data, traf, b'tfhd')
        _, flags, p = _full(data, tfhd)
        # Every run is tied to this moof, never an arbitrary disk/file address.
        if not flags & 0x020000 or flags & ~0x02003a:
            raise FormatError('unsupported_tfhd_addressing_or_flags')
        tid = u32(data, p, tfhd.end); p += 4
        if tid not in init.tracks or tid in decode_times:
            raise FormatError('unknown_or_duplicate_fragment_track')
        t = init.tracks[tid]
        if flags & 2:
            if u32(data, p, tfhd.end) != 1:
                raise FormatError('unsupported_sample_description')
            p += 4
        duration, size, sflags = t.default_duration, t.default_size, t.default_flags
        if flags & 8:
            duration = u32(data, p, tfhd.end); p += 4
        if flags & 16:
            size = u32(data, p, tfhd.end); p += 4
        if flags & 32:
            sflags = u32(data, p, tfhd.end); p += 4
        if p != tfhd.end:
            raise FormatError('invalid_tfhd_length')
        tfdts = [b for b in subs if b.kind == b'tfdt']
        if len(tfdts) > 1:
            raise FormatError('duplicate_tfdt')
        dts = None
        if tfdts:
            b = tfdts[0]
            ver, fl, p = _full(data, b, (0, 1))
            n = 8 if ver else 4
            if fl or p+n != b.end:
                raise FormatError('invalid_tfdt')
            dts = int.from_bytes(data[p:b.end], 'big')
        decode_times[tid] = dts
        runs = [b for b in subs if b.kind == b'trun']
        if not runs:
            raise FormatError('missing_trun')
        next_offset = None
        for b in runs:
            version, fl, p = _full(data, b, (0, 1))
            if fl & ~0xf05 or (fl & 4 and fl & 0x400):
                raise FormatError('unsupported_or_conflicting_trun_flags')
            count = u32(data, p, b.end); p += 4
            if not 0 < count <= MAX_SAMPLES or len(samples)+count > MAX_SAMPLES:
                raise FormatError('invalid_fragment_sample_count')
            if sample_budget is not None and len(samples)+count > sample_budget:
                raise FormatError('prefix_sample_budget_exceeded')
            if fl & 1:
                off = u32(data, p, b.end); p += 4
                if off >= 0x80000000: off -= 0x100000000
                next_offset = pos+off
            elif next_offset is None:
                raise FormatError('first_trun_requires_explicit_data_offset')
            first_flags = None
            if fl & 4:
                first_flags = u32(data, p, b.end); p += 4
            for index in range(count):
                dur, sz, sf, cto = duration, size, sflags, 0
                if fl & 0x100:
                    dur = u32(data, p, b.end); p += 4
                if fl & 0x200:
                    sz = u32(data, p, b.end); p += 4
                if fl & 0x400:
                    sf = u32(data, p, b.end); p += 4
                elif index == 0 and first_flags is not None:
                    sf = first_flags
                if fl & 0x800:
                    cto = u32(data, p, b.end); p += 4
                    if version == 1 and cto >= 0x80000000: cto -= 0x100000000
                if not 0 < dur <= 0xffffffff or not 0 < sz <= 64*1024*1024:
                    raise FormatError('missing_or_invalid_sample_duration_or_size')
                if next_offset < mdat.payload or next_offset+sz > mdat.end:
                    raise FormatError('sample_outside_associated_mdat')
                nts = nal_types(data, next_offset, sz, t)
                samples.append(FragmentSample(tid, next_offset, sz, dur, sf, cto, nts))
                next_offset += sz
            if p != b.end:
                raise FormatError('trun_size_mismatch')
    ranges = sorted((s.offset, s.offset+s.size) for s in samples)
    current = mdat.payload
    for a, b in ranges:
        if a != current:
            raise FormatError('overlapping_or_unaccounted_mdat_bytes')
        current = b
    if current != mdat.end:
        raise FormatError('unaccounted_trailing_mdat_bytes')
    return Fragment(pos, mdat.end, seq, tuple(samples), decode_times)


def walk_prefix(data: bytes, init_size: int) -> Prefix:
    init = parse_initialization(data[:init_size])
    result = Prefix(init, init_size, stop_offset=init_size)
    pos, previous = init_size, None
    retained_samples = 0
    while pos < len(data):
        result.stop_offset = pos
        try:
            b = box_at(data, pos)
            if b.kind in (b'free', b'skip'):
                # Retain the box unchanged; do not infer an alignment from zeros.
                pos = b.end
                continue
            if b.kind in (b'mfra', b'ftyp'):
                result.stop_reason = 'terminal_index' if b.kind == b'mfra' else 'next_initialization'
                break
            if b.kind != b'moof':
                raise FormatError(f'expected_moof_at_{pos}_found_{b.kind!r}')
            f = parse_fragment(data, pos, init, sample_budget=MAX_PREFIX_SAMPLES-retained_samples)
            if previous is None and f.sequence not in (0, 1):
                raise FormatError(f'missing_initial_fragments_sequence_starts_at_{f.sequence}')
            if previous is not None and f.sequence != previous+1:
                raise FormatError(f'fragment_sequence_gap_expected_{previous+1}_found_{f.sequence}')
            proposed = dict(result.clocks)
            starts = dict(result.starts)
            for tid, base in f.decode_times.items():
                if tid in proposed and base is not None and base != proposed[tid]:
                    raise FormatError(f'decode_time_discontinuity_track_{tid}')
                if tid not in proposed:
                    proposed[tid] = 0 if base is None else base
                    starts[tid] = proposed[tid]
            for s in f.samples:
                proposed[s.track_id] += s.duration
            result.clocks, result.starts = proposed, starts
            result.fragments.append(f)
            retained_samples += len(f.samples)
            result.end = f.end
            previous, pos = f.sequence, f.end
        except (FormatError, struct.error) as e:
            result.stop_reason = str(e)
            break
    else:
        result.stop_offset = len(data)
        result.stop_reason = 'end_of_bounded_window'
    return result


def inspect_embedded(data: bytes, init: Initialization, maximum: int = 4096) -> dict:
    """Locate and validate moof/mdat pairs even inside an over-carved outer mdat.

    This is an inventory, not permission to concatenate independent fragments.
    """
    hits, errors, p, searched = [], [], 0, 0
    while True:
        ix = data.find(b'moof', p)
        if ix < 0:
            break
        p = ix+4
        if ix < 4: continue
        searched += 1
        if searched > maximum:
            errors.append({'reason': 'signature_search_limit', 'offset': ix-4})
            break
        try:
            f = parse_fragment(data, ix-4, init)
            hits.append({'offset': f.start, 'end': f.end, 'sequence': f.sequence,
                         'decode_times': f.decode_times,
                         'samples': [{'track_id': s.track_id, 'offset': s.offset, 'size': s.size,
                                      'duration': s.duration, 'flags': s.flags,
                                      'nal_types': list(s.nal_types)} for s in f.samples]})
        except (FormatError, struct.error) as e:
            errors.append({'offset': ix-4, 'reason': str(e)})
    return {'valid_fragments': hits, 'rejected_candidates': errors,
            'warning': 'Embedded fragments are not assigned to recordings by sequence alone.'}

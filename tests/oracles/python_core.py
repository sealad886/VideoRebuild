"""Frozen 0.5.0 CPU oracle (43671fdf4ecb20419fa0e47df9d494ffbe615f81). Test-only; never imported by production."""

import re
from collections import deque
import heapq
from types import SimpleNamespace
from videorebuild.bmff import FormatError
VIDEO_CODECS = {b"avc1", b"avc3", b"hvc1", b"hev1"}

def iter_sample_nals(data: bytes, codec: bytes, length_size: int):
    """Yield fully bounded NAL views; do not interpret picture/slice contents."""
    if length_size not in (1, 2, 4) or codec not in VIDEO_CODECS:
        raise FormatError('unsupported_NAL_framing')
    view = memoryview(data)
    p = count = 0
    hevc = codec in {b'hvc1', b'hev1'}
    while p < len(view):
        if p+length_size > len(view):
            raise FormatError('truncated_NAL_length')
        n = int.from_bytes(view[p:p+length_size], 'big')
        p += length_size
        if n < (2 if hevc else 1) or p+n > len(view):
            raise FormatError('invalid_NAL_length')
        nal = view[p:p+n]
        typ = (nal[0] >> 1) & 63 if hevc else nal[0] & 31
        if nal[0] & 128 or (hevc and (typ > 40 or not nal[1] & 7)) or (not hevc and not 1 <= typ <= 23):
            raise FormatError('invalid_NAL_header')
        count += 1
        if count > 65536:
            raise FormatError('too_many_NALs_in_sample')
        yield nal
        p += n
    if not count:
        raise FormatError('empty_video_sample')

def _valid_avc_sei(nal) -> bool:
    """Validate SEI message envelopes and EBSP escaping, without guessing payloads."""
    if nal[0] != 6:
        return False
    rbsp = bytearray()
    zeroes = 0
    i = 1
    while i < len(nal):
        value = nal[i]
        if zeroes >= 2:
            if value == 3:
                if i+1 == len(nal) or nal[i+1] > 3:
                    return False
                zeroes = 0
                i += 1
                continue
            if value <= 2:
                return False
        rbsp.append(value)
        zeroes = zeroes+1 if value == 0 else 0
        i += 1
    if not rbsp or rbsp[-1] != 128:
        return False
    end = len(rbsp)-1
    p = messages = 0
    while p < end:
        for field in range(2):
            value = 0
            while p < end and rbsp[p] == 255:
                value += 255
                p += 1
            if p >= end:
                return False
            value += rbsp[p]
            p += 1
            if field == 1:
                p += value
        if p > end:
            return False
        messages += 1
    return p == end and messages > 0

def sample_kind(data: bytes, codec: bytes, length_size: int) -> str | None:
    """Recognize VCL samples or well-framed AVC standalone SEI, never HEVC-only metadata."""
    vcl = False
    sei_only = codec in {b'avc1', b'avc3'}
    try:
        for nal in iter_sample_nals(data, codec, length_size):
            typ = (nal[0] >> 1) & 63 if codec in {b'hvc1', b'hev1'} else nal[0] & 31
            vcl |= typ <= 31 if codec in {b'hvc1', b'hev1'} else typ in (1, 2, 3, 4, 5)
            if codec in {b'avc1', b'avc3'} and typ == 6 and not _valid_avc_sei(nal):
                return None
            sei_only &= typ == 6
    except FormatError:
        return None
    return 'vcl' if vcl else 'avc_sei' if sei_only else None

def valid_sample(data: bytes, codec: bytes, length_size: int) -> bool:
    """Check complete sample framing, including narrowly supported standalone AVC SEI."""
    return sample_kind(data, codec, length_size) is not None

def prune_domains(domains, *, max_edges: int = 200_000, max_checks: int = 800_000):
    """Return supported domains and counters, preserving candidate order.

    A domain entry is ``(key, physical_extents)``; each extent contains
    ``(device, inode, start, end)``.  Different groups must not overlap the same
    physical bytes.  A sweep builds only overlapping candidate-pair conflicts.
    If that derived graph exceeds ``max_edges`` or the sweep performs more than
    ``max_checks`` overlapping extent comparisons, return the original domains
    unchanged. The second bound also covers repeated extents that describe the
    same candidate conflict. This fallback is never an absence conclusion.
    """
    if max_edges < 0 or max_checks < 0:
        raise ValueError('constraint budgets must be nonnegative')
    original = [list(domain) for domain in domains]
    stats = dict(constraint_complete=True, constraint_conflicts=0,
                 constraint_overlap_checks=0, constraint_support_checks=0,
                 constraint_removed=0)
    if any(not domain for domain in original) or len(original) < 2:
        return original, stats

    intervals = []
    for group, domain in enumerate(original):
        for candidate, (_, extents) in enumerate(domain):
            for device, inode, start, end in extents:
                if end < start:
                    raise ValueError('physical extent ends before its start')
                if end > start:
                    intervals.append((device, inode, start, end, group, candidate))
    intervals.sort()
    conflicts = [[{} for _ in domain] for domain in original]
    neighbors = [set() for _ in original]
    active = {}
    expiry = []
    identity = None
    for token, (device, inode, start, end, group, candidate) in enumerate(intervals):
        key = device, inode
        if key != identity:
            active.clear()
            expiry.clear()
            identity = key
        while expiry and expiry[0][0] <= start:
            _, old_token, old_group = heapq.heappop(expiry)
            del active[old_group][old_token]
            if not active[old_group]:
                del active[old_group]
        for other_group, entries in active.items():
            if other_group == group:
                continue
            for other_candidate in entries.values():
                stats['constraint_overlap_checks'] += 1
                if stats['constraint_overlap_checks'] > max_checks:
                    stats['constraint_complete'] = False
                    return original, stats
                previous = conflicts[group][candidate].get(other_group, 0)
                bit = 1 << other_candidate
                if previous & bit:
                    continue
                stats['constraint_conflicts'] += 1
                if stats['constraint_conflicts'] > max_edges:
                    stats['constraint_complete'] = False
                    return original, stats
                conflicts[group][candidate][other_group] = previous | bit
                reverse = conflicts[other_group][other_candidate]
                reverse[group] = reverse.get(group, 0) | (1 << candidate)
                neighbors[group].add(other_group)
                neighbors[other_group].add(group)
        active.setdefault(group, {})[token] = candidate
        heapq.heappush(expiry, (end, token, group))

    alive = [(1 << len(domain)) - 1 for domain in original]
    queue = deque((group, other) for group, values in enumerate(neighbors)
                  for other in sorted(values))
    queued = set(queue)
    while queue:
        group, other = queue.popleft()
        queued.remove((group, other))
        remaining = alive[group]
        removed = 0
        while remaining:
            bit = remaining & -remaining
            remaining -= bit
            candidate = bit.bit_length() - 1
            stats['constraint_support_checks'] += 1
            blocked = conflicts[group][candidate].get(other, 0)
            if blocked & alive[other] == alive[other]:
                removed |= bit
        if not removed:
            continue
        alive[group] &= ~removed
        stats['constraint_removed'] += removed.bit_count()
        if not alive[group]:
            # An empty domain already proves there is no full assignment.
            break
        for neighbor in sorted(neighbors[group]):
            arc = neighbor, group
            if arc not in queued:
                queue.append(arc)
                queued.add(arc)
    return [[entry for candidate, entry in enumerate(domain)
             if alive[group] & (1 << candidate)]
            for group, domain in enumerate(original)], stats

def header(read, position):
    """Return a codec-union framing superset without reading beyond AVC NALs."""
    try:
        raw = read(position, 5)
        n = int.from_bytes(raw[:4], 'big')
        if n >= 2:
            raw += read(position+5, 1)
        first = raw[4]
        avc = not (first & 128) and 1 <= (first & 31) <= 23
        hevc = n >= 2 and not (first & 128) and ((first >> 1) & 63) <= 40 and (raw[5] & 7)
        return n, bool(avc or hevc)
    except FormatError:
        return 0, False

def cumulative_lengths(get_header, start, source_size, maximum, visit=None):
    """Yield sample-size-minus-four at every valid cumulative NAL boundary.

    Superset nominations deliberately do not decode payloads. Every final sample
    still requires the codec-specific full validator and complete range proof.
    """
    position, total = start, 0
    for _ in range(65536):
        if visit is not None and not visit():
            return
        if position < 0 or position+5 > source_size:
            return
        n, legal = get_header(position)
        if not legal or n < 1 or total+4+n > maximum or position+4+n > source_size:
            return
        total += 4+n
        yield total-4
        position += 4+n

def _anchor_locations(pool, maximum_sample, wanted, opt, state, task, *, source_ids=None):
    """Canonical bounded NAL-chain discovery, independent of target batches."""
    high = ((maximum_sample - 4) >> 16) & 255
    # Zero-width matching preserves overlapping length prefixes at block edges.
    pattern = re.compile(fr'(?=\x00[\x00-\x{high:02x}][\s\S]{{2}})(?!\x00{{4}})'.encode('ascii'))

    # Header facts are independent of the sample start and cumulative length.
    # Bound cache memory; cache no source bytes and clear at each read block.
    heads = {}

    def header(sid, block_base, data, pos):
        if pos in heads:
            state['discovery_header_cache_hits'] = state.get('discovery_header_cache_hits', 0) + 1
            return heads[pos]
        state['discovery_header_reads'] = state.get('discovery_header_reads', 0) + 1
        def read(offset, count):
            local = offset-block_base
            if 0 <= local and local+count <= len(data):
                return data[local:local+count]
            return pool.read(sid, offset, count)
        value = nal_search.header(read, pos)
        if len(heads) >= 8192:
            heads.clear()
        heads[pos] = value
        return value

    def totals(sid, block_base, data, start):
        def visit():
            if opt.max_anchor_checks is not None and state['nal_checks'] >= opt.max_anchor_checks:
                state.update(limited=True, stop_reason='nal_check_budget')
                return False
            state['nal_checks'] += 1
            if state['nal_checks'] % 4096 == 0:
                task.heartbeat(nal_checks=state['nal_checks'], anchor_checks=state['anchor_checks'])
            return True
        for length in nal_search.cumulative_lengths(lambda pos: header(sid, block_base, data, pos),
                                                   start, pool.sources[sid]['size'], maximum_sample, visit):
            if wanted(length):
                yield length

    scanned = 0
    for sid in (pool.sources if source_ids is None else source_ids):
        pool.check(sid)
        try:
            for base, data, owned in pool.iter_blocks(sid, overlap=16):
                heads.clear()
                at = 0
                for match in pattern.finditer(data):
                    at = match.start()
                    if at >= owned or at+4 > len(data):
                        continue
                    for length in totals(sid, base, data, base+at):
                        yield sid, base+at, length
                        if state['limited']:
                            break
                    if state['limited']:
                        break
                scanned += min(owned, at+1) if state['limited'] else owned
                task.update(scanned, anchor_checks=state['anchor_checks'], nal_checks=state['nal_checks'],
                            located_extents=state.get('located_extents', 0), discovered_locations=state.get('locations', 0))
                if state['limited']:
                    return
        finally:
            pool.check(sid)

nal_search = SimpleNamespace(header=header, cumulative_lengths=cumulative_lengths)


def fragment_nal_types(data: bytes, offset: int, size: int, track) -> tuple[int, ...]:
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

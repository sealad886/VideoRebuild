"""Canonical NAL header facts and complete cumulative sample-length clues."""
from .bmff import FormatError


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

"""Canonical NAL header facts and complete cumulative sample-length clues."""
from .bmff import FormatError
from . import _native


def header(read, position):
    """Return a codec-union framing superset without reading beyond AVC NALs."""
    try:
        raw = read(position, 5)
        n = int.from_bytes(raw[:4], 'big')
        if n >= 2:
            raw += read(position+5, 1)
        return _native.nal_header(raw)
    except FormatError:
        return 0, False


def cumulative_lengths(get_header, start, source_size, maximum, visit=None):
    """Yield sample-size-minus-four at every valid cumulative NAL boundary.

    Superset nominations deliberately do not decode payloads. Every final sample
    still requires the codec-specific full validator and complete range proof.
    """
    if start < 0 or start+5 > source_size:
        if visit is not None:
            visit()
        return
    chain = _native.NalChain(start, source_size, maximum)
    for _ in range(65536):
        if visit is not None and not visit():
            return
        position = chain.position()
        if position is None:
            return
        n, legal = get_header(position)
        length = chain.accept(n, legal)
        if length is None:
            return
        yield length

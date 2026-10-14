from __future__ import annotations
import hashlib
from pathlib import Path
import random
import subprocess

from videorebuild.bmff import boxes, parse_movie
from videorebuild.engine import groups_for


def generate(path: Path, codec: str = 'h264', audio: bool = True, faststart: bool = False,
             bframes: bool = False, seconds: float = 2.0) -> None:
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
            '-f', 'lavfi', '-i', f'testsrc2=size=160x96:rate=12:duration={seconds}']
    if audio:
        args += ['-f', 'lavfi', '-i', f'sine=frequency=440:sample_rate=48000:duration={seconds}']
    args += ['-c:v', 'libx264' if codec == 'h264' else 'libx265', '-preset', 'ultrafast',
             '-pix_fmt', 'yuv420p', '-g', '12', '-threads', '1']
    if codec == 'hevc':
        args += ['-x265-params', 'log-level=error:pools=none:frame-threads=1', '-tag:v', 'hvc1']
    else:
        args += ['-bf', '2' if bframes else '0']
    if audio:
        args += ['-c:a', 'aac', '-b:a', '64k', '-shortest']
    if faststart:
        args += ['-movflags', '+faststart']
    args += ['-y', str(path)]
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=60)


def movie_from(raw: bytes):
    b = next(b for b in boxes(raw) if b.kind == b'moov')
    return parse_movie(raw[b.start:b.end])


def split(original: Path, root: Path, duplicate: bool = False, decoy: bool = False) -> None:
    root.mkdir()
    raw = original.read_bytes()
    for b in boxes(raw):
        if b.kind == b'moov':
            (root / 'f900000_ftyp.mov').write_bytes(raw[b.start:b.end])
        elif b.kind == b'mdat':
            data = raw[b.start:b.end]
            (root / 'f000040_mdat.mov').write_bytes(data)
            if duplicate:
                (root / 'f777777_mdat.mov').write_bytes(data)
            if decoy:
                (root / 'f000039_mdat.mov').write_bytes(data[:b.header] + b'\xff'*(len(data)-b.header))


def scatter(original: Path, output: Path, *, drop: bool = False, separate_files: bool = False) -> None:
    raw = original.read_bytes()
    movie = movie_from(raw)
    groups = groups_for(movie)
    rng = random.Random(773)
    rng.shuffle(groups)
    if drop:
        groups.pop()
    if separate_files:
        output.mkdir()
        (output / 'f999_ftyp.mov').write_bytes(movie.data)
        for i, g in enumerate(groups):
            (output / f'f{100+i*17}.bin').write_bytes(raw[g.start:g.start+g.span])
    else:
        # No ftyp and no mdat headers remain. Only moov and shuffled extents.
        parts = [b'X'*8192, movie.data, b'Q'*111]
        for g in groups:
            parts.extend([raw[g.start:g.start+g.span], b'Z'*rng.randint(64, 4096)])
        output.write_bytes(b''.join(parts))


def payload(original: Path) -> bytes:
    raw = original.read_bytes()
    movie = movie_from(raw)
    return b''.join(raw[c.offset:c.offset+c.size] for c in movie.chunks)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

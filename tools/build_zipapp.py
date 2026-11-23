#!/usr/bin/env python3
"""Build a deterministic platform-specific zipapp with the required Rust core."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import sysconfig
import zipfile

root = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    sys.path.insert(0, str(root / 'src'))
from videorebuild.progress import phase, reporting


def build_archive(target, wheel=None):
    source = root / 'src'
    files = {str(p.relative_to(source)): p.read_bytes()
             for p in sorted((source / 'videorebuild').rglob('*.py'))
             if p.name != '.DS_Store'}
    if wheel is not None:
        with zipfile.ZipFile(wheel) as archive:
            natives = [name for name in archive.namelist()
                       if name.startswith('videorebuild/_native.') and name.endswith('.so')]
            if len(natives) != 1 or '/' in natives[0].removeprefix('videorebuild/'):
                raise ValueError('wheel must contain exactly one native extension')
            for name, data in files.items():
                if archive.read(name) != data:
                    raise ValueError('wheel package source differs from the checkout')
            native_name, native = natives[0], archive.read(natives[0])
            for name in archive.namelist():
                if '.dist-info/licenses/' in name and not name.endswith('/'):
                    files['_licenses/' + name.split('.dist-info/licenses/', 1)[1]] = archive.read(name)
        parts = wheel.stem.rsplit('-', 3)
        if len(parts) != 4 or parts[1:3] != ['cp313', 'abi3']:
            raise ValueError('zipapp requires a CPython 3.13 stable-ABI wheel')
        platform_tag = parts[3]
    else:
        natives = sorted((source / 'videorebuild').glob('_native.*.so'))
        if len(natives) != 1:
            raise ValueError('build the Rust extension first, or pass --wheel PATH')
        native_name = 'videorebuild/' + natives[0].name
        native = natives[0].read_bytes()
        platform_tag = sysconfig.get_platform().replace('-', '_').replace('.', '_')
        for name in ('LICENSE', 'native/THIRD_PARTY_NOTICES.md'):
            files['_licenses/' + name] = (root / name).read_bytes()
    if len(native) > 64 * 1024 * 1024:
        raise ValueError('native extension exceeds standalone extraction bound')
    files[native_name] = native
    files['_videorebuild_native.json'] = json.dumps({
        'path': native_name, 'sha256': hashlib.sha256(native).hexdigest(),
        'platform': platform_tag, 'python': 'cp313', 'abi': 'abi3'},
        sort_keys=True, separators=(',', ':')).encode()
    files['__main__.py'] = (root / 'tools/zipapp_main.py').read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('wb') as stream:
        stream.write(b'#!/usr/bin/env python3\n')
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(files.items()):
                entry = zipfile.ZipInfo(name, date_time=(2026, 9, 29, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, data)
    target.chmod(0o755)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-progress', action='store_true', help='Disable progress messages on stderr')
    parser.add_argument('--wheel', type=Path, help='Bundle the native core from this matching wheel')
    parser.add_argument('--output', type=Path, default=root / 'videorebuild.pyz')
    args = parser.parse_args()
    try:
        with reporting(enabled=not args.no_progress):
            with phase('Build standalone archive', total=1, unit='files') as task:
                target = build_archive(args.output, args.wheel)
                task.advance()
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Standalone build failed: {error}\n')
    print(target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

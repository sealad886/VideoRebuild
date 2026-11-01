#!/usr/bin/env python3
"""Build a deterministic standard-library zipapp from this repository."""
import argparse
from pathlib import Path
import sys
import zipfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'src'))
from videorebuild.progress import phase, reporting


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-progress', action='store_true', help='Disable progress messages on stderr')
    args = parser.parse_args()
    target = root / 'videorebuild.pyz'
    source = root / 'src'
    with reporting(enabled=not args.no_progress):
        with phase('Build standalone archive', total=1, unit='files') as task:
            files = {str(p.relative_to(source)): p.read_bytes() for p in sorted(source.rglob('*.py'))}
            files['__main__.py'] = b'from videorebuild.cli import main\nraise SystemExit(main())\n'
            with target.open('wb') as f:
                f.write(b'#!/usr/bin/env python3\n')
                with zipfile.ZipFile(f, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                    for name, data in sorted(files.items()):
                        entry = zipfile.ZipInfo(name, date_time=(2026, 9, 29, 0, 0, 0))
                        entry.compress_type = zipfile.ZIP_DEFLATED
                        entry.external_attr = 0o644 << 16
                        z.writestr(entry, data)
            target.chmod(0o755)
            task.advance()
    print(target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

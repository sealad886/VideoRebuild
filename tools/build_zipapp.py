#!/usr/bin/env python3
"""Build a deterministic standard-library zipapp from this repository."""
from pathlib import Path
import zipfile

root = Path(__file__).resolve().parents[1]
target = root / 'videorebuild.pyz'
source = root / 'src'
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
print(target)

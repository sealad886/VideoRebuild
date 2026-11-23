#!/usr/bin/env python3
"""Install one native wheel outside the checkout and exercise its public CLI."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile


def check(python, wheel, standalone):
    wheel, standalone = wheel.resolve(), standalone.resolve()
    with tempfile.TemporaryDirectory(prefix='videorebuild-consumer-') as directory:
        work = Path(directory)
        environment = work / 'environment'
        subprocess.run([str(python), '-m', 'venv', str(environment)], check=True, cwd=work)
        interpreter = environment / 'bin/python'
        subprocess.run([str(interpreter), '-m', 'pip', 'install', '--no-index', '--no-deps',
                        str(wheel)], check=True, cwd=work)
        script = '''import importlib.metadata, pathlib, sys
import videorebuild, videorebuild._native
from videorebuild.bmff import valid_sample
assert importlib.metadata.version('videorebuild') == videorebuild.__version__
for module in (videorebuild, videorebuild._native):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
assert valid_sample(b'\\0\\0\\0\\2\\x65x', b'avc1', 4)
assert not valid_sample(b'\\0\\0\\0\\3\\x65x', b'avc1', 4)
print('Installed native consumer:', sys.version.split()[0], videorebuild.__version__)
'''
        subprocess.run([str(interpreter), '-I', '-c', script], check=True, cwd=work)
        for arguments in (['--version'], ['--help'], ['tui', '--help'], ['doctor']):
            subprocess.run([str(interpreter), '-I', '-m', 'videorebuild', *arguments],
                           check=True, cwd=work)
        # The original interpreter has no consumer environment on its path;
        # standalone must carry and load its own extension successfully.
        for arguments in (['--version'], ['doctor']):
            subprocess.run([str(python), '-I', str(standalone), *arguments], check=True, cwd=work)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--zipapp', type=Path, required=True)
    args = parser.parse_args()
    check(args.python, args.wheel, args.zipapp)


if __name__ == '__main__':
    main()

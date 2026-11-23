"""Standalone bootstrap: extract one verified native module into private storage."""
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import signal
import sys
import sysconfig
import tempfile
import zipfile


def main():
    if (sys.implementation.name != 'cpython' or sys.version_info < (3, 13)
            or sysconfig.get_config_var('Py_GIL_DISABLED')):
        raise RuntimeError('VideoRebuild standalone requires GIL-enabled CPython 3.13 or newer')
    with zipfile.ZipFile(Path(sys.argv[0]).resolve()) as archive:
        metadata = json.loads(archive.read('_videorebuild_native.json'))
        name = metadata['path']
        if (not name.startswith('videorebuild/_native.') or not name.endswith('.so')
                or '/' in name.removeprefix('videorebuild/')):
            raise RuntimeError('Invalid standalone native module path')
        tag, machine = metadata['platform'], platform.machine().lower()
        expected_os = 'darwin' if tag.startswith('macosx_') else 'linux' if 'linux' in tag else None
        expected_machine = 'arm64' if tag.endswith('_arm64') else 'aarch64' if tag.endswith('_aarch64') else 'x86_64' if tag.endswith('_x86_64') else None
        if (expected_os != sys.platform or expected_machine != machine
                or metadata['python'] != 'cp313' or metadata['abi'] != 'abi3'):
            raise RuntimeError(f'VideoRebuild standalone platform {tag} does not match this interpreter')
        if archive.getinfo(name).file_size > 64 * 1024 * 1024:
            raise RuntimeError('Standalone native module exceeds extraction bound')
        native = archive.read(name)
    if hashlib.sha256(native).hexdigest() != metadata['sha256']:
        raise RuntimeError('Standalone native module digest differs')
    # SIGINT already raises KeyboardInterrupt; unwind SIGTERM as well so the
    # private extraction directory is removed during ordinary cancellation.
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        with tempfile.TemporaryDirectory(prefix='videorebuild-native-') as directory:
            path = Path(directory) / Path(name).name
            with path.open('xb') as stream:
                stream.write(native)
            del native
            import videorebuild
            spec = importlib.util.spec_from_file_location('videorebuild._native', path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            from videorebuild.cli import main as cli_main
            return cli_main()
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())

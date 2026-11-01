#!/usr/bin/env python3
"""Check built release identity without importing or executing archive contents."""
import argparse
import ast
from email import message_from_bytes
import hashlib
from pathlib import Path
import re
import tarfile
import tomllib
import zipfile


STABLE_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z')
REVISION = re.compile(r'[0-9a-f]{40}\Z')


def source_version(root):
    """Read the one literal version source, without importing the application."""
    config = tomllib.loads((root / 'pyproject.toml').read_text())
    project = config['project']
    if ('version' in project or project.get('dynamic', []).count('version') != 1
            or config['tool']['setuptools']['dynamic']['version']
            != {'attr': 'videorebuild.__version__'}):
        raise ValueError('setuptools must derive version only from videorebuild.__version__')
    tree = ast.parse((root / 'src/videorebuild/__init__.py').read_text())
    values = [ast.literal_eval(node.value) for node in tree.body
              if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == '__version__'
                      for target in node.targets)]
    if len(values) != 1 or not isinstance(values[0], str) or not STABLE_VERSION.fullmatch(values[0]):
        raise ValueError('canonical version must be one literal stable X.Y.Z string')
    return values[0]


def check_metadata(data, version, label):
    metadata = message_from_bytes(data)
    if metadata.get_all('Name') != ['videorebuild'] or metadata.get_all('Version') != [version]:
        raise ValueError(f'{label}: distribution name/version differs from source')


def zip_contents(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f'{path.name}: duplicate archive entries')
        return {name: archive.read(name) for name in names if not name.endswith('/')}


def check_release(root, dist, version, revision=None, write_evidence=False):
    """Require all three distributions to contain the exact current package source."""
    if not STABLE_VERSION.fullmatch(version) or source_version(root) != version:
        raise ValueError('expected stable version differs from canonical source')
    if revision is not None and not REVISION.fullmatch(revision):
        raise ValueError('revision must be a full lowercase 40-character commit SHA')
    if write_evidence and revision is None:
        raise ValueError('writing release evidence requires an exact revision')
    names = [f'videorebuild-{version}-py3-none-any.whl',
             f'videorebuild-{version}.tar.gz', 'videorebuild.pyz']
    allowed = set(names) | {'SHA256SUMS.txt', 'source-revision.txt'}
    actual = {path.name for path in dist.iterdir()}
    if actual - allowed or not set(names).issubset(actual):
        raise ValueError('release directory must contain exactly one wheel, sdist and zipapp')
    source = {str(path.relative_to(root / 'src')): path.read_bytes()
              for path in sorted((root / 'src/videorebuild').rglob('*.py'))}
    wheel = zip_contents(dist / names[0])
    check_metadata(wheel[f'videorebuild-{version}.dist-info/METADATA'], version, 'wheel')
    zipapp = zip_contents(dist / names[2])
    with tarfile.open(dist / names[1], 'r:gz') as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        member_names = [member.name for member in members]
        if len(member_names) != len(set(member_names)):
            raise ValueError('sdist: duplicate archive entries')
        sdist = {member.name: archive.extractfile(member).read() for member in members}
    prefix = f'videorebuild-{version}/'
    check_metadata(sdist[prefix + 'PKG-INFO'], version, 'sdist')
    for label, content, base in (('wheel', wheel, ''), ('zipapp', zipapp, ''),
                                 ('sdist', sdist, prefix + 'src/')):
        package = {name.removeprefix(base): data for name, data in content.items()
                   if name.startswith(base + 'videorebuild/') and name.endswith('.py')}
        if package != source:
            raise ValueError(f'{label}: package source differs from the checked-out revision')
    if write_evidence:
        (dist / 'source-revision.txt').write_text(revision + '\n')
        checksum_names = names + ['source-revision.txt']
        (dist / 'SHA256SUMS.txt').write_text(''.join(
            f'{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}\n'
            for name in sorted(checksum_names)))
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--dist', type=Path, default=Path('dist'))
    parser.add_argument('--version', required=True)
    parser.add_argument('--revision')
    parser.add_argument('--write-evidence', action='store_true')
    args = parser.parse_args()
    try:
        names = check_release(args.root, args.dist, args.version, args.revision, args.write_evidence)
    except (ValueError, KeyError, OSError, SyntaxError, tarfile.TarError, zipfile.BadZipFile) as error:
        parser.exit(1, f'Release check failed: {error}\n')
    print(f'Verified videorebuild {args.version}: ' + ', '.join(names))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

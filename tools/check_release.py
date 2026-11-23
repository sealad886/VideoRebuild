#!/usr/bin/env python3
"""Check built release identity without importing or executing archive contents."""
import argparse
import ast
import configparser
from email import message_from_bytes
import hashlib
import json
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


def native_module(content, label):
    modules = {name: data for name, data in content.items()
               if name.startswith('videorebuild/_native.') and name.endswith('.so')}
    if len(modules) != 1 or '/' in next(iter(modules)).removeprefix('videorebuild/'):
        raise ValueError(f'{label}: exactly one native extension required')
    return modules


def check_release(root, dist, version, revision=None, write_evidence=False):
    """Check source archive and each platform's wheel/standalone pair."""
    if not STABLE_VERSION.fullmatch(version) or source_version(root) != version:
        raise ValueError('expected stable version differs from canonical source')
    if revision is not None and not REVISION.fullmatch(revision):
        raise ValueError('revision must be a full lowercase 40-character commit SHA')
    if write_evidence and revision is None:
        raise ValueError('writing release evidence requires an exact revision')
    wheels = sorted(path.name for path in dist.glob(f'videorebuild-{version}-cp313-abi3-*.whl'))
    names = [f'videorebuild-{version}.tar.gz']
    names += wheels + [name.removesuffix('.whl') + '.pyz' for name in wheels]
    allowed = set(names) | {'SHA256SUMS.txt', 'source-revision.txt'}
    actual = {path.name for path in dist.iterdir()}
    if not wheels or actual - allowed or not set(names).issubset(actual):
        raise ValueError('release directory must contain one sdist and matching native wheel/zipapp pairs')
    source = {str(path.relative_to(root / 'src')): path.read_bytes()
              for path in sorted((root / 'src/videorebuild').rglob('*.py'))}
    with tarfile.open(dist / names[0], 'r:gz') as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        member_names = [member.name for member in members]
        if len(member_names) != len(set(member_names)):
            raise ValueError('sdist: duplicate archive entries')
        sdist = {member.name: archive.extractfile(member).read() for member in members}
    prefix = f'videorebuild-{version}/'
    check_metadata(sdist[prefix + 'PKG-INFO'], version, 'sdist')
    artifacts = [('sdist', sdist, prefix + 'src/')]
    for name in wheels:
        wheel = zip_contents(dist / name)
        check_metadata(wheel[f'videorebuild-{version}.dist-info/METADATA'], version, name)
        wheel_tag = name.removesuffix('.whl').split('-', 2)[2]
        metadata = message_from_bytes(wheel[f'videorebuild-{version}.dist-info/WHEEL'])
        expected_tags = {'cp313-abi3-' + platform for platform in wheel_tag.removeprefix('cp313-abi3-').split('.')}
        if metadata.get('Root-Is-Purelib') != 'false' or set(metadata.get_all('Tag', [])) != expected_tags:
            raise ValueError(f'{name}: native wheel tag differs from filename')
        zipapp_name = name.removesuffix('.whl') + '.pyz'
        zipapp = zip_contents(dist / zipapp_name)
        licenses = {name.split('.dist-info/licenses/', 1)[1]: data for name, data in wheel.items()
                    if '.dist-info/licenses/' in name}
        expected_licenses = {name: (root / name).read_bytes()
                             for name in ('LICENSE', 'native/THIRD_PARTY_NOTICES.md')}
        standalone_licenses = {name.removeprefix('_licenses/'): data for name, data in zipapp.items()
                               if name.startswith('_licenses/')}
        if licenses != expected_licenses or standalone_licenses != licenses:
            raise ValueError('native license notices differ between source, wheel and standalone')
        native = native_module(wheel, name)
        if native_module(zipapp, zipapp_name) != native:
            raise ValueError(f'{zipapp_name}: native module differs from wheel')
        native_name, native_bytes = next(iter(native.items()))
        expected = {'path': native_name, 'sha256': hashlib.sha256(native_bytes).hexdigest(),
                    'platform': wheel_tag.removeprefix('cp313-abi3-'), 'python': 'cp313', 'abi': 'abi3'}
        if json.loads(zipapp['_videorebuild_native.json']) != expected:
            raise ValueError(f'{zipapp_name}: native identity differs from wheel')
        if zipapp['__main__.py'] != (root / 'tools/zipapp_main.py').read_bytes():
            raise ValueError(f'{zipapp_name}: bootstrap differs from checked-out revision')
        artifacts.extend([(name, wheel, ''), (zipapp_name, zipapp, '')])
    for label, content, base in artifacts:
        package = {name.removeprefix(base): data for name, data in content.items()
                   if name.startswith(base + 'videorebuild/') and name.endswith('.py')}
        if package != source:
            raise ValueError(f'{label}: package source differs from the checked-out revision')
    native_sources = [root / 'native/Cargo.toml', root / 'native/Cargo.lock']
    native_sources.extend(sorted((root / 'native').rglob('*.rs')))
    native_sources = [path for path in native_sources if 'target' not in path.parts]
    for path in native_sources:
        if sdist.get(prefix + str(path.relative_to(root))) != path.read_bytes():
            raise ValueError('sdist: native build source differs from checked-out revision')
    for name in ('LICENSE', 'native/THIRD_PARTY_NOTICES.md'):
        if sdist.get(prefix + name) != (root / name).read_bytes():
            raise ValueError('sdist: native license notices differ from checked-out revision')
    source_config, archive_config = configparser.ConfigParser(), configparser.ConfigParser()
    source_config.read_string((root / 'setup.cfg').read_text())
    archive_config.read_string(sdist[prefix + 'setup.cfg'].decode())
    source_options = {section: dict(source_config[section]) for section in source_config.sections()}
    archive_options = {section: dict(archive_config[section]) for section in archive_config.sections()}
    # setuptools adds these deterministic sdist metadata options itself.
    if 'egg_info' not in source_options and archive_options.get('egg_info') == {'tag_build': '', 'tag_date': '0'}:
        del archive_options['egg_info']
    if archive_options != source_options:
        raise ValueError('sdist: native build configuration differs from checked-out revision')
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

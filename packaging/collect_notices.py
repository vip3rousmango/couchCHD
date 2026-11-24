"""Collect license texts from the actual build environment, not an assumed dependency list."""
from importlib import metadata
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import quote


def collect(out: Path) -> None:
    out.mkdir(parents=True)
    with (out / 'SOURCES.txt').open('w', encoding='utf-8') as index:
        index.write('Unmodified source and build recipes for binary components\n'
                    'Qt: https://download.qt.io/archive/qt/6.7/6.7.3/single/qt-everywhere-src-6.7.3.tar.xz\n'
                    'Qt for Python / Shiboken: https://code.qt.io/cgit/pyside/pyside-setup.git/tag/?h=v6.7.3\n'
                    'pygame-ce and wheel codec recipes/hashes: https://github.com/pygame-community/pygame-ce/tree/2.5.5/buildconfig/manylinux-build\n'
                    'AppImage runtime and its dependency recipes (x86 only): https://github.com/AppImage/type2-runtime/tree/dd6cebedcbddde9c82f89b011e8e1d40b6e43868\n'
                    'MAME and its static SDL: see converter/PROVENANCE.txt and release source archives.\n'
                    'CouchCHD: see the source archive and build scripts next to the release download.\n\n')
        for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
            name = dist.metadata['Name']
            if name.lower() == 'couchchd':
                continue
            print(f'{name} {dist.version}: https://pypi.org/project/{name}/{dist.version}/#files', file=index)
            for file in dist.files or ():
                if '..' in file.parts or file.is_absolute():
                    continue
                if not any(p.lower().startswith(('license', 'licence', 'copying', 'copyright', 'notice'))
                           or p.lower().endswith(('.license', '.spdx')) for p in file.parts):
                    continue
                src = Path(file.locate())
                if src.is_file():
                    dest = out / 'python' / name / file
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)

        # Include a superset: both statically linked build libraries and shared
        # libraries collected by PyInstaller, including Debian's packaging patches.
        query = ['dpkg-query', '-W', '-f=${db:Status-Abbrev}\t${Package}\t${source:Package}\t${source:Version}\\n']
        for line in subprocess.check_output(query, text=True).splitlines():
            status, name, source, version = line.split('\t')
            if not status.startswith('ii'):
                continue
            print(f'{name}: https://snapshot.debian.org/package/{quote(source)}/{quote(version)}/', file=index)
            src = Path('/usr/share/doc') / name / 'copyright'
            if src.is_file():
                dest = out / 'debian' / name
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest / 'copyright')
    shutil.copytree('/usr/share/common-licenses', out / 'common-licenses', symlinks=False)


if __name__ == '__main__':
    collect(Path(sys.argv[1]))

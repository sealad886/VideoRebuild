"""Filesystem listings for terminal selection; browsing never creates paths."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .paths import is_ignored_path


@dataclass(frozen=True)
class Entry:
    path: Path
    name: str
    directory: bool
    regular_file: bool = False


class Browser:
    def __init__(self, initial: str = ''):
        target = Path(initial).expanduser().absolute() if initial else Path.cwd()
        target = target.parent.resolve() / target.name
        current = target
        while not current.is_dir() and current != current.parent:
            current = current.parent
        self.current = current.resolve()
        self.entries: list[Entry] = []
        self.error = ''
        self.selected = 0
        self.refresh()
        self.selected = next((i for i, entry in enumerate(self.entries)
                              if entry.path == target), 0)

    def listing(self, path: Path) -> list[Entry]:
        entries = []
        with os.scandir(path) as listing:
            for item in listing:
                if is_ignored_path(item.name):
                    continue
                try:
                    directory, regular = item.is_dir(), item.is_file()
                except OSError:
                    directory, regular = False, False
                entries.append(Entry(path / item.name, item.name, directory, regular))
        entries.sort(key=lambda item: (not item.directory, item.name.casefold(), item.name))
        return [Entry(path.parent, '..', True), *entries]

    def refresh(self) -> None:
        try:
            self.entries = self.listing(self.current)
            self.error = ''
        except OSError as error:
            self.entries = [Entry(self.current.parent, '..', True)]
            self.error = f'Cannot list this directory: {error}. Go up or choose another path.'
        self.selected = min(self.selected, len(self.entries) - 1)

    def enter(self, path: Path) -> bool:
        try:
            target = path.resolve(strict=True)
            entries = self.listing(target)
        except (OSError, RuntimeError) as error:
            self.error = f'Cannot open this directory: {error}. Choose another entry or go up.'
            return False
        previous = self.current
        self.current, self.entries, self.error = target, entries, ''
        self.selected = next((i for i, entry in enumerate(entries)
                              if entry.path == previous), 0)
        return True

    def jump(self, value: str) -> None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.current / path
        path = path.parent.resolve() / path.name
        if is_ignored_path(path):
            self.error = '.DS_Store files are ignored. Choose another path.'
            return
        if path.is_dir():
            self.enter(path)
        elif path.is_file() and self.enter(path.parent):
            self.selected = next((i for i, entry in enumerate(self.entries)
                                  if entry.path == path), 0)
        else:
            self.error = 'Path is not an existing directory or regular file. Choose another path.'

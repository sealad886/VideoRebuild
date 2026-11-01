"""Source names excluded from indexing, recovery and filesystem selection."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath


def is_ignored_path(value: str | Path) -> bool:
    return PurePosixPath(str(value).replace('\\', '/')).name.casefold() == '.ds_store'


def is_ignored_source(row) -> bool:
    row = dict(row)
    if is_ignored_path(row.get('path', '')):
        return True
    if row.get('kind') != 'logical':
        return False
    binding = row.get('binding') or {}
    if isinstance(binding, str):
        binding = json.loads(binding)
    return is_ignored_path(binding.get('name', ''))

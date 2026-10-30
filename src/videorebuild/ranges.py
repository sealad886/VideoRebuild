"""Bounded logical byte ranges and acquisition/filesystem mapping evidence."""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from pathlib import Path
import os
import re
import stat
from typing import Iterable, Iterator, Mapping, Sequence
from xml.parsers import expat

from .bmff import FormatError

MAX_OFFSET = (1 << 63) - 1
_DFXML_NS = 'http://www.forensicswiki.org/wiki/Category:Digital_Forensics_XML'


class UnavailableRange(FormatError):
    """Requested bytes are unmapped or not reported as rescued."""


@dataclass(frozen=True)
class Extent:
    logical_offset: int
    source_id: int
    source_offset: int
    length: int


def _integer(value: int, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_OFFSET:
        raise FormatError(f'invalid_{name}')
    return value


def validate_extents(size: int, extents: Iterable[Extent],
                     source_sizes: Mapping[int, int]) -> tuple[Extent, ...]:
    """Validate and sort mappings; unmapped logical intervals remain holes."""
    _integer(size, 'logical_size')
    values = tuple(extents)
    for extent in values:
        if not isinstance(extent, Extent):
            raise FormatError('invalid_extent')
        _integer(extent.logical_offset, 'logical_offset')
        _integer(extent.source_id, 'source_id')
        _integer(extent.source_offset, 'source_offset')
        _integer(extent.length, 'extent_length', positive=True)
    ordered = tuple(sorted(values, key=lambda e: e.logical_offset))
    end = 0
    for extent in ordered:
        if extent.logical_offset < end:
            raise FormatError('overlapping_logical_extents')
        end = extent.logical_offset + extent.length
        if end > size:
            raise FormatError('extent_beyond_logical_end')
        if extent.source_id not in source_sizes:
            raise FormatError('unknown_extent_source')
        source_size = _integer(source_sizes[extent.source_id], 'source_size')
        if extent.source_offset + extent.length > source_size:
            raise FormatError('extent_beyond_source_end')
    previous_source = None
    physical_end = 0
    for extent in sorted(ordered, key=lambda e: (e.source_id, e.source_offset)):
        if extent.source_id == previous_source and extent.source_offset < physical_end:
            raise FormatError('overlapping_source_extents')
        previous_source = extent.source_id
        physical_end = extent.source_offset + extent.length
    return ordered


def resolve_extents(extents: Sequence[Extent], offset: int,
                    length: int) -> Iterator[Extent]:
    """Resolve a range in validated, sorted mappings in O(log n + hits)."""
    _integer(offset, 'read_offset')
    _integer(length, 'read_length')
    if offset + length > MAX_OFFSET:
        raise FormatError('read_range_overflow')
    end = offset + length
    index = max(0, bisect_right(extents, offset, key=lambda e: e.logical_offset) - 1)
    while offset < end:
        if index >= len(extents):
            raise UnavailableRange(f'unmapped_range_at_{offset}')
        extent = extents[index]
        extent_end = extent.logical_offset + extent.length
        if extent_end <= offset:
            index += 1
            continue
        if extent.logical_offset > offset:
            raise UnavailableRange(f'unmapped_range_at_{offset}')
        count = min(extent_end, end) - offset
        yield Extent(offset, extent.source_id,
                     extent.source_offset + offset - extent.logical_offset, count)
        offset += count
        index += 1


@dataclass(frozen=True)
class RescueSpan:
    offset: int
    length: int
    status: str


def _map_number(token: str) -> int:
    if not re.fullmatch(r'(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)', token):
        raise FormatError('invalid_ddrescue_number')
    base = 16 if token.lower().startswith('0x') else (8 if token.startswith('0') else 10)
    value = int(token, base)
    return _integer(value, 'ddrescue_number')


class RescueMap:
    """An immutable map of reported acquisition status in image coordinates."""
    def __init__(self, blocks: Sequence[RescueSpan], *, image_offset: int = 0):
        self._blocks = tuple(blocks)
        self.image_offset = image_offset

    @classmethod
    def from_file(cls, path: Path, *, image_offset: int = 0,
                  max_bytes: int = 256 * 1024 * 1024,
                  max_blocks: int = 1_000_000) -> RescueMap:
        if type(image_offset) is not int or not -MAX_OFFSET <= image_offset <= MAX_OFFSET:
            raise FormatError('invalid_image_offset')
        _integer(max_bytes, 'map_byte_limit', positive=True)
        _integer(max_blocks, 'map_block_limit', positive=True)
        blocks = []
        previous_end = None
        header = False
        total = 0
        if not stat.S_ISREG(Path(path).stat().st_mode):
            raise FormatError('ddrescue_map_must_be_regular_file')
        with Path(path).open('rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise FormatError('ddrescue_map_must_be_regular_file')
            while True:
                raw = source.readline(4097)
                if not raw:
                    break
                total += len(raw)
                if total > max_bytes or len(raw) > 4096:
                    raise FormatError('ddrescue_map_byte_limit')
                try:
                    line = raw.decode('ascii')
                except UnicodeDecodeError as error:
                    raise FormatError('ddrescue_map_must_be_ascii') from error
                line = re.split(r'(?<!\S)#', line, maxsplit=1)[0].strip()
                if not line:
                    continue
                fields = line.split()
                if not header:
                    if len(fields) != 3 or fields[1] not in ('?', '*', '%', '/', '-', 'F', 'G', '+'):
                        raise FormatError('invalid_ddrescue_status_line')
                    _map_number(fields[0])
                    if not re.fullmatch(r'[0-9]+', fields[2]) or not 0 < int(fields[2]) <= MAX_OFFSET:
                        raise FormatError('invalid_ddrescue_pass')
                    header = True
                    continue
                if len(fields) != 3 or fields[2] not in ('?', '*', '/', '-', '+'):
                    raise FormatError('invalid_ddrescue_block_line')
                start, count = _map_number(fields[0]), _map_number(fields[1])
                if count == 0 or start + count > MAX_OFFSET:
                    raise FormatError('invalid_ddrescue_block_range')
                if previous_end is not None and start != previous_end:
                    raise FormatError('noncontiguous_ddrescue_blocks')
                previous_end = start + count
                translated = start + image_offset
                if translated < -MAX_OFFSET or translated + count > MAX_OFFSET:
                    raise FormatError('ddrescue_image_range_overflow')
                blocks.append(RescueSpan(translated, count, fields[2]))
                if len(blocks) > max_blocks:
                    raise FormatError('ddrescue_map_block_limit')
        if not header or not blocks:
            raise FormatError('empty_ddrescue_map')
        return cls(blocks, image_offset=image_offset)

    def spans(self, offset: int, length: int) -> Iterator[RescueSpan]:
        _integer(offset, 'read_offset')
        _integer(length, 'read_length')
        if offset + length > MAX_OFFSET:
            raise FormatError('read_range_overflow')
        end = offset + length
        index = max(0, bisect_right(self._blocks, offset, key=lambda b: b.offset) - 1)
        while offset < end:
            if index >= len(self._blocks):
                yield RescueSpan(offset, end - offset, 'unknown')
                return
            block = self._blocks[index]
            block_end = block.offset + block.length
            if block_end <= offset:
                index += 1
                continue
            if block.offset > offset:
                count = min(block.offset, end) - offset
                yield RescueSpan(offset, count, 'unknown')
            else:
                count = min(block_end, end) - offset
                yield RescueSpan(offset, count, block.status)
                index += 1
            offset += count

    def require_rescued(self, offset: int, length: int) -> None:
        for span in self.spans(offset, length):
            if span.status != '+':
                raise UnavailableRange(f'ddrescue_range_not_rescued_{span.status}_at_{span.offset}')


@dataclass(frozen=True)
class DFXMLRecord:
    name: str
    size: int
    extents: tuple[tuple[int, int, int], ...]
    reason: str | None
    evidence_kind: str


def _xml_number(text: str) -> int:
    if not re.fullmatch(r'[0-9]+', text.strip()):
        raise FormatError('invalid_dfxml_number')
    significant = text.strip().lstrip('0') or '0'
    if len(significant) > 19:
        raise FormatError('invalid_dfxml_number')
    return _integer(int(significant), 'dfxml_number')


class _DFXMLParser:
    def __init__(self, image_size: int, evidence_kind: str, max_objects: int,
                 max_runs: int, max_runs_per_file: int, max_depth: int):
        self.image_size, self.evidence_kind = image_size, evidence_kind
        self.max_objects, self.max_runs = max_objects, max_runs
        self.max_runs_per_file, self.max_depth = max_runs_per_file, max_depth
        self.stack: list[str] = []
        self.ready: deque[DFXMLRecord] = deque()
        self.objects = self.runs = self.file_runs = 0
        self.record = None
        self.capture = None
        self.text: list[str] = []
        self.text_size = 0
        self.parser = expat.ParserCreate(namespace_separator='}')
        self.parser.StartElementHandler = self.start
        self.parser.EndElementHandler = self.end
        self.parser.CharacterDataHandler = self.characters
        self.parser.StartDoctypeDeclHandler = self.forbidden
        self.parser.EntityDeclHandler = self.forbidden
        self.parser.ExternalEntityRefHandler = self.forbidden

    @staticmethod
    def forbidden(*args):
        raise FormatError('dfxml_dtd_or_entity_forbidden')

    @staticmethod
    def local(name: str) -> str:
        if '}' not in name:
            return name
        namespace, local = name.rsplit('}', 1)
        return local if namespace == _DFXML_NS else ''

    def problem(self, reason: str):
        if self.record is not None:
            old = self.record['reason']
            if old is None or old == 'unmapped_ranges':
                self.record['reason'] = reason

    def start(self, tag: str, attributes: dict[str, str]):
        name = self.local(tag)
        self.stack.append(name)
        if len(self.stack) > self.max_depth:
            raise FormatError('dfxml_depth_limit')
        if len(self.stack) == 1 and name != 'dfxml':
            raise FormatError('invalid_dfxml_root')
        if name == 'fileobject':
            if self.record is not None or len(self.stack) < 2 or self.stack[-2] not in ('dfxml', 'volume'):
                raise FormatError('invalid_dfxml_fileobject_nesting')
            self.objects += 1
            if self.objects > self.max_objects:
                raise FormatError('dfxml_object_limit')
            self.record = {'depth': len(self.stack), 'name': '', 'size': None,
                           'extents': [], 'holes': [], 'reason': None, 'fields': set()}
            self.file_runs = 0
        if self.record is not None:
            depth = self.record['depth']
            if len(self.stack) == depth + 1 and name in ('filename', 'filesize', 'compressed', 'name_type'):
                if name in self.record['fields']:
                    self.problem('duplicate_dfxml_field')
                self.record['fields'].add(name)
                self.capture, self.text, self.text_size = name, [], 0
            if name == 'byte_run' and self.stack[-3:] == ['fileobject', 'byte_runs', 'byte_run']:
                self.add_run(attributes)
        elif self.stack[-2:] == ['source', 'image_size']:
            self.capture, self.text, self.text_size = 'image_size', [], 0

    def characters(self, text: str):
        if self.capture is not None:
            self.text_size += len(text)
            if self.text_size > 16_384:
                raise FormatError('dfxml_text_limit')
            self.text.append(text)

    def add_run(self, attributes: dict[str, str]):
        self.runs += 1
        self.file_runs += 1
        if self.runs > self.max_runs or self.file_runs > self.max_runs_per_file:
            raise FormatError('dfxml_run_limit')
        if 'fill' in attributes:
            if attributes['fill'] != '0':
                self.problem('unsupported_dfxml_fill')
                return
            try:
                logical = _xml_number(attributes.get('file_offset', attributes.get('offset', '')))
                length = _xml_number(attributes.get('len', ''))
                if length == 0 or logical + length > MAX_OFFSET or 'img_offset' in attributes:
                    raise FormatError('invalid_sparse_range')
                if 'file_offset' in attributes and 'offset' in attributes:
                    if logical != _xml_number(attributes['offset']):
                        raise FormatError('conflicting_dfxml_offsets')
                self.record['holes'].append((logical, length))
                self.problem('unmapped_ranges')
            except FormatError:
                self.problem('invalid_extent_mapping')
            return
        if 'uncompressed_len' in attributes:
            self.problem('unsupported_compressed_extent')
            return
        if 'type' in attributes or 'unknown_flags' in attributes:
            self.problem('unsupported_extent_type')
            return
        try:
            logical = _xml_number(attributes.get('file_offset', attributes.get('offset', '')))
            if 'file_offset' in attributes and 'offset' in attributes:
                if logical != _xml_number(attributes['offset']):
                    raise FormatError('conflicting_dfxml_offsets')
            image = _xml_number(attributes.get('img_offset', ''))
            length = _xml_number(attributes.get('len', ''))
            if length == 0 or logical + length > MAX_OFFSET or image + length > MAX_OFFSET:
                raise FormatError('invalid_extent_range')
            self.record['extents'].append((logical, image, length))
        except FormatError:
            self.problem('invalid_extent_mapping')

    def finish(self):
        record = self.record
        size = record['size']
        if size is None:
            size = -1
            self.problem('missing_filesize')
        previous_end = 0
        claimed = [(logical, length) for logical, _, length in record['extents']] + record['holes']
        for logical, length in sorted(claimed):
            if size >= 0 and logical >= size:
                self.problem('extent_beyond_logical_end')
            if logical < previous_end:
                self.problem('overlapping_logical_extents')
            previous_end = max(previous_end, logical + length)
        covered = 0
        extents = []
        for logical, image, length in sorted(record['extents']):
            if logical >= max(size, 0):
                self.problem('extent_beyond_logical_end')
                continue
            length = min(length, size - logical)
            if logical < covered:
                self.problem('overlapping_logical_extents')
                continue
            if image + length > self.image_size:
                self.problem('extent_beyond_image_end')
                continue
            if logical > covered:
                self.problem('unmapped_ranges')
            extents.append((logical, image, length))
            covered = logical + length
        if size >= 0 and covered != size:
            self.problem('unmapped_ranges')
        physical_end = 0
        for _, image, length in sorted(extents, key=lambda e: e[1]):
            if image < physical_end:
                self.problem('overlapping_source_extents')
            physical_end = max(physical_end, image + length)
        self.ready.append(DFXMLRecord(record['name'], size, tuple(extents),
                                     record['reason'], self.evidence_kind))
        self.record = None

    def end(self, tag: str):
        name = self.local(tag)
        if self.capture == name:
            text = ''.join(self.text)
            if name == 'image_size':
                if _xml_number(text) != self.image_size:
                    raise FormatError('dfxml_image_size_mismatch')
            elif name == 'filename':
                self.record['name'] = text
            elif name == 'name_type':
                if text.strip() != 'r':
                    self.problem('not_regular_file')
            elif name == 'compressed':
                if text.strip() != '0':
                    self.problem('unsupported_compressed_extent')
            elif name == 'filesize':
                try:
                    self.record['size'] = _xml_number(text)
                except FormatError:
                    self.problem('invalid_filesize')
            self.capture, self.text, self.text_size = None, [], 0
        if name == 'fileobject':
            self.finish()
        self.stack.pop()


def iter_dfxml(path: Path, image_size: int, *, evidence_kind: str = 'filesystem',
               max_bytes: int = 512 * 1024 * 1024, max_objects: int = 1_000_000,
               max_runs: int = 1_000_000, max_runs_per_file: int = 100_000,
               max_depth: int = 64) -> Iterator[DFXMLRecord]:
    """Stream DFXML records; callers must roll back imports on a later error.

    A record's extents contain (logical_offset, image_offset, length). Sparse
    or absent runs are reported as unmapped ranges, never manufactured zeros.
    Other unsupported or malformed mappings have a blocking reason. Only a
    record with complete raw coverage has no reason.
    """
    _integer(image_size, 'image_size')
    for value in (max_bytes, max_objects, max_runs, max_runs_per_file, max_depth):
        _integer(value, 'dfxml_limit', positive=True)
    reader = _DFXMLParser(image_size, evidence_kind, max_objects, max_runs,
                          max_runs_per_file, max_depth)
    total = 0
    if not stat.S_ISREG(Path(path).stat().st_mode):
        raise FormatError('dfxml_must_be_regular_file')
    try:
        with Path(path).open('rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise FormatError('dfxml_must_be_regular_file')
            while True:
                chunk = source.read(min(65_536, max_bytes - total + 1))
                total += len(chunk)
                if total > max_bytes:
                    raise FormatError('dfxml_byte_limit')
                reader.parser.Parse(chunk, not chunk)
                while reader.ready:
                    yield reader.ready.popleft()
                if not chunk:
                    break
    except expat.ExpatError as error:
        raise FormatError(f'malformed_dfxml_at_line_{error.lineno}') from error

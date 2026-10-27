"""Pure, bounded Stage 5 source processors and high-precision content screen."""

from __future__ import annotations

import hashlib
import json
import re
import time
from bisect import bisect_right
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import replace
from functools import lru_cache
from uuid import UUID, uuid4

from buildlens.domain.processing import Chunk, ChunkSpan, ProcessingDiagnostic, ProcessorContract
from buildlens.domain.source import PROCESSING_VERSION, SCREENING_VERSION, line_offsets
from buildlens.domain.source import ContentRejectedError as ContentRejectedError
from buildlens.domain.source import screen_content as screen_content
from buildlens.domain.structural import Symbol

MAX_CHUNK_CHARS = 4096
PROCESSOR_VERSION = PROCESSING_VERSION


@lru_cache(maxsize=1)
def _coordinates(source: str) -> list[int]:
    return line_offsets(source)


_chunk_budget: ContextVar[tuple[int, float] | None] = ContextVar("chunk_budget", default=None)
_chunk_count: ContextVar[int] = ContextVar("chunk_count", default=0)


def check_budget() -> None:
    budget = _chunk_budget.get()
    if budget:
        if time.monotonic() >= budget[1]:
            raise ContentRejectedError("ARTIFACT_TIMEOUT")
        if _chunk_count.get() >= budget[0]:
            raise ContentRejectedError("CHUNK_LIMIT")


def decoded_span(source: str, start: int, end: int) -> ChunkSpan:
    offsets = _coordinates(source)
    first, last = bisect_right(offsets, start) - 1, bisect_right(offsets, end) - 1
    return ChunkSpan(start, end, first + 1, last + 1, start - offsets[first], end - offsets[last])


def contract_for(name: str, decoding: str = "utf-8") -> ProcessorContract:
    return ProcessorContract(
        name,
        PROCESSOR_VERSION,
        "recursive-zero-overlap.v2",
        decoding,
        screening_version=SCREENING_VERSION,
    )


def _split_range(
    source: str, start: int, end: int, maximum: int = MAX_CHUNK_CHARS
) -> list[tuple[int, int]]:
    if end - start <= maximum:
        return [(start, end)]
    result: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        ceiling = min(cursor + maximum, end)
        if ceiling == end:
            result.append((cursor, end))
            break
        window = source[cursor:ceiling]
        candidates = [
            list(re.finditer(pattern, window))
            for pattern in (r"(?:\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)", r"\r\n|\r|\n", r"\s+")
        ]
        boundary = next(
            (
                matches[-1].end()
                for matches in candidates
                if matches and matches[-1].end() >= len(window) // 2
            ),
            len(window),
        )
        cut = cursor + boundary
        if cut < end and source[cut - 1 : cut + 1] == "\r\n":
            cut -= 1
        result.append((cursor, cut))
        cursor = cut
    return result


def _chunks(
    source: str,
    artifact_processing_id: UUID,
    artifact_anchor: str,
    kind: str,
    units: Iterable[tuple[int, int, str, tuple[str, ...], str | None]],
    contract: ProcessorContract,
    symbol_id: UUID | None = None,
    symbol_identity_id: UUID | None = None,
) -> tuple[Chunk, ...]:
    values: list[Chunk] = []
    for _unit_no, (start, end, anchor, headings, json_path) in enumerate(units):
        for part, (left, right) in enumerate(_split_range(source, start, end)):
            if not source[left:right].strip():
                continue
            check_budget()
            _chunk_count.set(_chunk_count.get() + 1)
            content = source[left:right]
            digest = hashlib.sha256(content.encode()).hexdigest()
            stable = hashlib.sha256(
                f"chunk.v2|{artifact_anchor}|{contract.digest_material}|{anchor}|{part}|{digest}".encode()
            ).hexdigest()
            values.append(
                Chunk(
                    uuid4(),
                    artifact_processing_id,
                    stable,
                    len(values),
                    anchor,
                    part,
                    kind,
                    content,
                    digest,
                    decoded_span(source, left, right),
                    symbol_id,
                    symbol_identity_id,
                    headings,
                    json_path,
                )
            )
    return tuple(values)


def generic_chunks(
    source: str,
    processing_id: UUID,
    artifact_anchor: str,
    contract: ProcessorContract | None = None,
) -> tuple[Chunk, ...]:
    active = contract or contract_for("generic")
    if not source.strip():
        return ()
    return _chunks(
        source,
        processing_id,
        artifact_anchor,
        "text",
        [(0, len(source), "text:0", (), None)],
        active,
    )


def markdown_chunks(
    source: str, processing_id: UUID, artifact_anchor: str
) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...]]:
    contract = contract_for("markdown")
    lines = source.splitlines(keepends=True)
    offsets = []
    point = 0
    for line in lines:
        offsets.append(point)
        point += len(line)
    headings: list[tuple[int, int, str]] = []
    fence: tuple[str, int] | None = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        marker = re.match(r"^(?:`{3,}|~{3,})", stripped)
        if marker:
            token = marker.group(0)
            if fence is None and len(line) - len(stripped) <= 3:
                fence = (token[0], len(token))
            elif (
                fence
                and len(line) - len(stripped) <= 3
                and token[0] == fence[0]
                and len(token) >= fence[1]
                and not stripped[len(token) :].strip()
            ):
                fence = None
            continue
        if fence or line.startswith("    "):
            continue
        atx = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*(?:\n|$)", line)
        setext = i + 1 < len(lines) and re.match(r"^\s{0,3}(=+|-+)\s*(?:\n|$)", lines[i + 1])
        if atx:
            headings.append((i, len(atx.group(1)), atx.group(2).strip()))
        elif setext:
            headings.append((i, 1 if setext.group(1).startswith("=") else 2, line.strip()))
    diagnostics: tuple[ProcessingDiagnostic, ...] = (
        (
            ProcessingDiagnostic(
                "UNCLOSED_FENCE",
                "An unclosed Markdown fence was treated as literal text.",
                "warning",
            ),
        )
        if fence
        else ()
    )
    units: list[tuple[int, int, str, tuple[str, ...], str | None]] = []
    stack: list[tuple[int, str, int]] = []
    seen: dict[tuple[str, ...], int] = {}
    if headings and headings[0][0] > 0:
        units.append((0, offsets[headings[0][0]], "markdown:preamble", (), None))
    elif not headings:
        units.append((0, len(source), "markdown:preamble", (), None))
    for index, (line_no, level, title) in enumerate(headings):
        stack = [item for item in stack if item[0] < level]
        stack.append((level, title, 0))
        path = tuple(x[1] for x in stack)
        occurrence = seen.get(path, 0)
        seen[path] = occurrence + 1
        end = offsets[headings[index + 1][0]] if index + 1 < len(headings) else len(source)
        # direct section stops before first child, preventing parent duplication
        for _later, (child_line, child_level, _) in enumerate(headings[index + 1 :], index + 1):
            if child_level > level:
                end = offsets[child_line]
                break
            if child_level <= level:
                break
        start = offsets[line_no]
        units.append(
            (
                start,
                end,
                f"heading:{json.dumps([path, occurrence], ensure_ascii=False)}",
                path,
                None,
            )
        )
    return _chunks(source, processing_id, artifact_anchor, "markdown", units, contract), diagnostics


def json_chunks(
    source: str, processing_id: UUID, artifact_anchor: str
) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...]]:
    contract = contract_for("json")

    def bad_constant(_: str) -> None:
        raise ValueError("non-standard numeric constant")

    try:
        json.loads(source, parse_constant=bad_constant)
    except (ValueError, json.JSONDecodeError):
        return (), (ProcessingDiagnostic("INVALID_JSON", "Artifact is not valid JSON.", "error"),)
    # Decoder raw_decode keeps exact original spellings and duplicate members can be scanned safely.
    decoder = json.JSONDecoder(parse_constant=bad_constant)
    left = len(source) - len(source.lstrip())
    if left >= len(source) or source[left] not in "[{":
        return _chunks(
            source,
            processing_id,
            artifact_anchor,
            "json",
            [(0, len(source), "json:$", (), "$")],
            contract,
        ), ()
    opener = source[left]
    closer = "}" if opener == "{" else "]"
    cursor = left + 1
    units = []
    occurrence: dict[str, int] = {}
    index = 0
    while True:
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] == closer:
            break
        begin = cursor
        path: str
        if opener == "{":
            key, key_end = decoder.raw_decode(source, cursor)
            cursor = key_end
            while source[cursor].isspace():
                cursor += 1
            if source[cursor] != ":":
                return (), (
                    ProcessingDiagnostic("INVALID_JSON", "Artifact is not valid JSON.", "error"),
                )
            cursor += 1
            while source[cursor].isspace():
                cursor += 1
            _, cursor = decoder.raw_decode(source, cursor)
            n = occurrence.get(str(key), 0)
            occurrence[str(key)] = n + 1
            path = json.dumps([key, n], ensure_ascii=False, separators=(",", ":"))
        else:
            _, cursor = decoder.raw_decode(source, cursor)
            path = f"$[{index}]"
            index += 1
        units.append((begin, cursor, f"json:{path}", (), path))
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor < len(source) and source[cursor] == ",":
            cursor += 1
            continue
        break
    return _chunks(
        source,
        processing_id,
        artifact_anchor,
        "json",
        units or [(0, len(source), "json:$", (), "$")],
        contract,
    ), ()


def python_chunks(
    source: str, processing_id: UUID, artifact_anchor: str, symbols: Iterable[Symbol]
) -> tuple[Chunk, ...]:
    """Slice verified Stage 4 declaration spans without reparsing or duplicating children."""
    contract = contract_for("python", "python-tokenize")
    line_starts = _coordinates(source)
    # Innermost declarations claim their body first, so enclosing regions can
    # retain only their own residual source.
    records = sorted(
        symbols,
        key=lambda value: (
            (value.declaration_span.end_line or 1)
            - (value.full_start_line or value.declaration_span.start_line or 1),
            value.full_start_line or 1,
        ),
    )
    regions: list[tuple[int, int, str, UUID | None, UUID | None]] = []
    occupied: list[tuple[int, int]] = []
    for item in records:
        first = item.full_start_line or item.declaration_span.start_line
        last = item.declaration_span.end_line
        if first is None or last is None or first < 1 or last < first or first > len(line_starts):
            continue
        start = line_starts[first - 1]
        end = line_starts[last] if last < len(line_starts) else len(source)
        # A parent contributes only the text not claimed by a nested declaration.
        children = [(a, b) for a, b in occupied if start <= a and b <= end]
        cursor = start
        for child_start, child_end in sorted(children):
            if cursor < child_start:
                regions.append(
                    (
                        cursor,
                        child_start,
                        f"symbol:{item.stable_identity_id or item.id}:residual",
                        item.id,
                        item.stable_identity_id,
                    )
                )
            cursor = max(cursor, child_end)
        if cursor < end:
            regions.append(
                (
                    cursor,
                    end,
                    f"symbol:{item.stable_identity_id or item.id}",
                    item.id,
                    item.stable_identity_id,
                )
            )
        occupied.append((start, end))
    intervals = sorted(occupied)
    cursor = 0
    for start, end in intervals:
        if cursor < start:
            regions.append((cursor, start, "module", None, None))
        cursor = max(cursor, end)
    if cursor < len(source):
        regions.append((cursor, len(source), "module", None, None))
    values: list[Chunk] = []
    ordered = sorted(regions)
    occurrence: dict[str, int] = {}
    for region_no, (start, end, anchor, symbol_id, identity) in enumerate(ordered):
        if anchor == "module" or anchor.endswith(":residual"):
            left = next((str(x[4] or x[3]) for x in reversed(ordered[:region_no]) if x[3]), "BOF")
            right = next((str(x[4] or x[3]) for x in ordered[region_no + 1 :] if x[3]), "EOF")
            anchor = f"{anchor}:{left}:{right}"
        number = occurrence.get(anchor, 0)
        occurrence[anchor] = number + 1
        anchor = f"{anchor}:unit:{number}"
        if not source[start:end].strip():
            continue
        values.extend(
            _chunks(
                source,
                processing_id,
                artifact_anchor,
                "python",
                [(start, end, anchor, (), None)],
                contract,
                symbol_id,
                identity,
            )
        )
    return tuple(replace(value, ordinal=index) for index, value in enumerate(values))


def toml_chunks(
    source: str, processing_id: UUID, artifact_anchor: str
) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...]]:
    """Validate TOML, then identify top-level assignments/tables outside multiline values."""
    import tomllib

    try:
        tomllib.loads(source)
    except tomllib.TOMLDecodeError:
        return (), (ProcessingDiagnostic("INVALID_TOML", "Artifact is not valid TOML.", "error"),)
    boundaries = [0]
    quote: str | None = None
    nesting = 0
    offset = 0
    for line in source.splitlines(keepends=True):
        stripped = line.lstrip()
        if quote is None and nesting == 0 and stripped and not stripped.startswith("#") and offset:
            boundaries.append(offset)
        i = 0
        while i < len(line):
            char = line[i]
            if quote:
                if line.startswith(quote, i):
                    i += len(quote)
                    quote = None
                    continue
                if char == "\\" and quote.startswith('"'):
                    i += 2
                    continue
            elif char == "#":
                break
            elif char in "\"'":
                quote = char * 3 if line.startswith(char * 3, i) else char
                i += len(quote)
                continue
            elif char in "[{":
                nesting += 1
            elif char in "]}":
                nesting -= 1
            i += 1
        offset += len(line)
    boundaries.append(len(source))
    units: list[tuple[int, int, str, tuple[str, ...], str | None]] = []
    table = "root"
    occurrences: dict[str, int] = {}
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        excerpt = source[a:b]
        if not excerpt.strip():
            continue
        meaningful = next(
            (
                line.strip()
                for line in excerpt.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ),
            "preamble",
        )
        quote_key: str | None = None
        escaped = False
        cut = len(meaningful)
        for i, char in enumerate(meaningful):
            if escaped:
                escaped = False
            elif quote_key:
                if char == "\\" and quote_key == '"':
                    escaped = True
                elif char == quote_key:
                    quote_key = None
            elif char in "\"'":
                quote_key = char
            elif char == "=":
                cut = i
                break
        logical = meaningful[:cut].strip()
        if logical.startswith("["):
            table = logical
        anchor = json.dumps([table, logical], ensure_ascii=False)
        occurrence = occurrences.get(anchor, 0)
        occurrences[anchor] = occurrence + 1
        units.append((a, b, f"toml:{anchor}:{occurrence}", (), None))
    return _chunks(source, processing_id, artifact_anchor, "toml", units, contract_for("toml")), ()


def yaml_chunks(
    source: str, processing_id: UUID, artifact_anchor: str
) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...]]:
    import yaml
    from yaml.nodes import MappingNode, SequenceNode

    try:
        # compose resolves no Python constructors; cap alias use before composition.
        if (
            sum(
                isinstance(event, yaml.events.AliasEvent)
                for event in yaml.parse(source, Loader=yaml.SafeLoader)
            )
            > 64
        ):
            raise ContentRejectedError("YAML_ALIAS_LIMIT")
        documents = list(yaml.compose_all(source, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return (), (ProcessingDiagnostic("INVALID_YAML", "Artifact is not valid YAML.", "error"),)
    units: list[tuple[int, int, str, tuple[str, ...], str | None]] = []
    for document_no, node in enumerate(documents):
        if isinstance(node, MappingNode):
            spans = [
                (key.start_mark.index, max(key.end_mark.index, value.end_mark.index))
                for key, value in node.value
            ]
        elif isinstance(node, SequenceNode):
            spans = [(value.start_mark.index, value.end_mark.index) for value in node.value]
        elif node is not None:
            spans = [(node.start_mark.index, node.end_mark.index)]
        else:
            spans = []
        # Aliases may refer backwards. Fall back to exact text, never duplicate ranges.
        if any(a < 0 or b < a or (i and a < spans[i - 1][1]) for i, (a, b) in enumerate(spans)):
            return generic_chunks(source, processing_id, artifact_anchor, contract_for("yaml")), (
                ProcessingDiagnostic(
                    "YAML_BOUNDARY_FALLBACK", "Complex YAML uses bounded text units.", "warning"
                ),
            )
        units.extend((a, b, f"yaml:{document_no}:{i}", (), None) for i, (a, b) in enumerate(spans))
    return _chunks(source, processing_id, artifact_anchor, "yaml", units, contract_for("yaml")), ()


def process_source(
    source: str,
    suffix: str,
    processing_id: UUID,
    anchor: str,
    symbols: tuple[Symbol, ...],
    max_chunks: int,
    timeout: float,
) -> tuple[tuple[Chunk, ...], tuple[ProcessingDiagnostic, ...], ProcessorContract]:
    budget = _chunk_budget.set((max_chunks, time.monotonic() + timeout))
    count = _chunk_count.set(0)
    try:
        if not source.strip():
            return (), (), contract_for("python" if suffix == "py" else "generic")
        if suffix == "py":
            return (
                python_chunks(source, processing_id, anchor, symbols),
                (),
                contract_for("python", "python-tokenize"),
            )
        processors = {
            "md": markdown_chunks,
            "json": json_chunks,
            "toml": toml_chunks,
            "yaml": yaml_chunks,
            "yml": yaml_chunks,
        }
        processor = processors.get(suffix)
        if processor:
            chunks, diagnostics = processor(source, processing_id, anchor)
            return (
                chunks,
                diagnostics,
                contract_for("yaml" if suffix == "yml" else {"md": "markdown"}.get(suffix, suffix)),
            )
        return generic_chunks(source, processing_id, anchor), (), contract_for("generic")
    finally:
        _chunk_budget.reset(budget)
        _chunk_count.reset(count)
        _coordinates.cache_clear()

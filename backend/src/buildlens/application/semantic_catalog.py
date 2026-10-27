"""Stage 13 deterministic semantic catalog (Milestone B).

``build_catalog`` is a pure function of the persisted structural observations, extractor
passes, chunk metadata and inventory of one snapshot. It never reads repository files, so it
works on legacy snapshots whose source is no longer available. ``SemanticCatalogService``
adds authority, the operation lock and publication with captured pointers.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from threading import Lock
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from buildlens.application.fact_renderer import rendered_fact
from buildlens.application.processing import ProcessingService
from buildlens.application.recognizers import ArtifactView, Match, recognize
from buildlens.domain.extractors import PYTHON_FACTS
from buildlens.domain.processing import ProcessingRun
from buildlens.domain.semantic import (
    NEIGHBOR_FACT_LIMIT,
    PACKAGE_BYTE_CEILING,
    PACKAGE_VERSION,
    REFERENCE_FACT_LIMIT,
    RELATION_ROW_LIMIT,
    RENDERER_VERSION,
    CatalogBuild,
    CatalogOutcome,
    ContextCoverage,
    EntityDetail,
    EntityKind,
    EvidenceChunk,
    FactKind,
    OmittedRegion,
    RelationKind,
    Resolution,
    Row,
    SemanticCatalogRun,
    SemanticEntity,
    SemanticFact,
    SemanticRelation,
    StoredCoverage,
    catalog_contract,
    mixed_script,
    package_digest_of,
)
from buildlens.domain.source import digest
from buildlens.domain.structural import StructuralAnalysis


class SemanticCatalogBusyError(Exception):
    pass


class SemanticCatalogConflictError(Exception):
    pass


class SemanticCatalogRequiredError(Exception):
    pass


class SemanticCatalogStaleError(Exception):
    pass


class SemanticNotFoundError(Exception):
    pass


class SemanticPrerequisiteError(Exception):
    pass


# --------------------------------------------------------------------------- input model


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    """One artifact's persisted rows, normalised by the store (the renderer's row shapes)."""

    analysis: Row
    artifact_identity_id: str | None
    symbols: tuple[Row, ...] = ()
    imports: tuple[Row, ...] = ()
    extractions: tuple[Row, ...] = ()
    symbol_facts: tuple[Row, ...] = ()
    references: tuple[Row, ...] = ()
    exports: tuple[Row, ...] = ()
    processing: Row | None = None
    chunks: tuple[Row, ...] = ()
    dependency_current: bool = False


@dataclass(frozen=True, slots=True)
class CatalogInput:
    producer_digest: str
    structural_outcome: str
    processing_outcome: str
    inventory: tuple[Row, ...]
    artifacts: tuple[ArtifactInput, ...]
    extractor_digests: dict[str, str]


# --------------------------------------------------------------------------- import resolution

_TS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx")
_PY_ROOT_MARKERS = {"pyproject.toml", "setup.py", "setup.cfg"}


@dataclass(frozen=True, slots=True)
class Resolved:
    status: Resolution
    locator: str | None = None
    submodule: bool = False


class ImportResolver:
    """``import-resolution.v1`` against the snapshot inventory only (no filesystem)."""

    def __init__(self, locators: Iterable[str]) -> None:
        self.locators = set(locators)
        roots = {""}
        for locator in self.locators:
            directory, name = posixpath.split(locator)
            if name in _PY_ROOT_MARKERS:
                roots.add(directory)
        roots |= {posixpath.join(root, "src") if root else "src" for root in set(roots)}
        self.roots = sorted(roots)

    def _python_module(self, base: str, dotted: str) -> list[str]:
        path = posixpath.join(base, *dotted.split(".")) if dotted else base
        return [
            candidate
            for candidate in (f"{path}.py", posixpath.join(path, "__init__.py"))
            if candidate in self.locators
        ]

    def _python_known(self, head: str) -> bool:
        """The top-level name is a repository module or *regular* package. A directory without
        ``__init__.py`` (for example an Alembic ``alembic/`` folder) does not shadow an installed
        package, so imports of that name stay ``external``."""
        for root in self.roots:
            prefix = posixpath.join(root, head) if root else head
            if f"{prefix}.py" in self.locators or f"{prefix}/__init__.py" in self.locators:
                return True
        return False

    def python(self, locator: str, row: Row) -> Resolved:
        level = int(row.get("level") or 0)
        form = row["form"]
        module = str(row.get("from_module") or "") if form == "from" else str(row["name"])
        name = str(row["name"]) if form == "from" else ""
        if level:
            base = posixpath.dirname(locator)
            for _ in range(level - 1):
                if not base:
                    return Resolved("not_found")
                base = posixpath.dirname(base)
            if name and name != "*":
                found = self._python_module(base, f"{module}.{name}" if module else name)
                if len(found) == 1:
                    return Resolved("resolved", found[0], True)
            found = self._python_module(base, module)
            if not module and not found:
                found = [p for p in [posixpath.join(base, "__init__.py")] if p in self.locators]
            return Resolved("resolved", found[0]) if len(found) == 1 else Resolved("not_found")
        if name and name != "*":
            sub = sorted(
                {
                    item
                    for root in self.roots
                    for item in self._python_module(root, f"{module}.{name}")
                }
            )
            if len(sub) == 1:
                return Resolved("resolved", sub[0], True)
            if len(sub) > 1:
                return Resolved("ambiguous")
        found = sorted({item for root in self.roots for item in self._python_module(root, module)})
        if len(found) == 1:
            return Resolved("resolved", found[0])
        if len(found) > 1:
            return Resolved("ambiguous")
        return Resolved("not_found" if self._python_known(module.split(".")[0]) else "external")

    def specifier(self, locator: str, specifier: str | None) -> Resolved:
        if not specifier:
            return Resolved("not_attempted")
        if specifier.startswith(("@/", "~/", "#")) or specifier.startswith("/"):
            return Resolved("not_attempted")
        if not specifier.startswith((".", "..")):
            return Resolved("external")
        base = posixpath.normpath(posixpath.join(posixpath.dirname(locator), specifier))
        if base == ".." or base.startswith("../"):
            return Resolved("not_found")
        candidates = [base, *(base + ext for ext in _TS_EXTENSIONS)]
        stem, extension = posixpath.splitext(base)
        if extension in {".js", ".jsx", ".mjs", ".cjs"}:
            candidates += [stem + ".ts", stem + ".tsx"]
        candidates += [posixpath.join(base, "index" + ext) for ext in _TS_EXTENSIONS]
        found = next((item for item in candidates if item in self.locators), None)
        return Resolved("resolved", found) if found else Resolved("not_found")


# --------------------------------------------------------------------------- builder


@dataclass(frozen=True, slots=True)
class _Region:
    by_lines: bool
    start: int
    end: int

    def contains(self, chunk: Row) -> bool:
        if self.by_lines:
            return bool(
                chunk["start_line"] >= self.start
                and (
                    chunk["end_line"] <= self.end
                    or (chunk["end_line"] == self.end + 1 and chunk["end_column"] == 0)
                )
            )
        return bool(self.start <= chunk["start_offset"] and chunk["end_offset"] <= self.end)

    def starts(self, chunk: Row) -> bool:
        if self.by_lines:
            return bool(chunk["start_line"] == self.start and chunk["start_column"] == 0)
        return bool(chunk["start_offset"] == self.start)


@dataclass(slots=True)
class _Entity:
    key: str
    kind: EntityKind
    file: _File
    portable: str
    name: str
    ordinal: int
    symbol: Row | None = None
    subject: str | None = None
    match: Match | None = None
    limitations: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    neighbors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _File:
    source: ArtifactInput
    locator: str
    language: str | None
    key: str
    python: bool
    usable: bool = False
    partial: bool = False
    unavailable: str | None = None
    declaration: Row | None = None
    facts_extraction: Row | None = None
    facts_usable: bool = False
    limitations: list[str] = field(default_factory=list)
    symbols: list[Row] = field(default_factory=list)
    by_id: dict[str, Row] = field(default_factory=dict)
    children: dict[str | None, list[Row]] = field(default_factory=dict)
    keys: dict[str, str] = field(default_factory=dict)
    facts_by_symbol: dict[str, Row] = field(default_factory=dict)
    references: list[Row] = field(default_factory=list)
    exports: list[Row] = field(default_factory=list)
    extraction_digest: dict[str, str] = field(default_factory=dict)
    bad_chunks: set[str] = field(default_factory=set)
    bindings: dict[str, tuple[Row, str | None]] = field(default_factory=dict)
    resolutions: dict[str, Resolved] = field(default_factory=dict)
    roles: dict[tuple[str, str], str] = field(default_factory=dict)

    def top(self, name: str) -> Row | None:
        return next((row for row in self.children.get(None, []) if row["name"] == name), None)


_RelationKey = tuple[str, str, str | None, str | None]


@dataclass(slots=True)
class _PendingRelation:
    kind: RelationKind
    source: str
    target: str | None
    text: str | None
    resolution: Resolution | None
    rows: list[tuple[str, Row, _File]]
    target_row: tuple[str, Row, _File] | None
    count: int = 1


class _Builder:
    def __init__(self, source: CatalogInput) -> None:
        self.source = source
        self.facts: dict[str, SemanticFact] = {}
        self.entities: list[_Entity] = []
        self.by_key: dict[str, _Entity] = {}
        self.relations: dict[_RelationKey, _PendingRelation] = {}
        self.files: list[_File] = []
        self.by_locator: dict[str, _File] = {}
        self.inventory = {
            str(row["locator"]): row for row in source.inventory if row.get("locator")
        }
        self.resolver = ImportResolver(self.inventory)
        self.skipped = 0
        self.matches: dict[str, int] = defaultdict(int)
        self.relation_facts: dict[str, list[SemanticRelation]] = defaultdict(list)

    # ----------------------------------------------------------------- facts and relations

    def _ref(self, file: _File, table: str, row: Row) -> Row:
        if table == "artifacts":
            return {"table": table, "id": row["id"], "contract": None, "locator": row["locator"]}
        if table == "artifact_analyses":
            contract: str | None = self.source.producer_digest
        elif table in {"symbols", "import_references"}:
            contract = (
                self.source.producer_digest
                if file.python
                else str((file.declaration or {}).get("contract_digest"))
            )
        elif table == "artifact_extractions":
            contract = str(row["contract_digest"])
        else:
            contract = file.extraction_digest[str(row["extraction_id"])]
        return {"table": table, "id": row["id"], "contract": contract, "locator": file.locator}

    def _fact(
        self,
        kind: FactKind,
        subject: str,
        rows: Sequence[tuple[str, Row, _File]],
        derived: Row,
        attach: bool = True,
    ) -> str:
        refs = [self._ref(file, table, row) for table, row, file in rows]
        material = {"subject": subject, **derived}
        text, value = rendered_fact(kind, refs, [row for _, row, _ in rows], material)
        if value not in self.facts:
            self.facts[value] = SemanticFact(
                value,
                len(self.facts),
                subject,
                kind,
                tuple(refs),
                material,
                RENDERER_VERSION,
                text,
            )
        entity = self.by_key.get(subject) if attach else None
        if entity is not None and value not in entity.facts:
            entity.facts.append(value)
        return value

    def _relate(
        self,
        kind: RelationKind,
        source: str,
        source_row: tuple[str, Row, _File],
        target: str | None,
        text: str | None,
        resolution: Resolution | None,
        evidence: Sequence[tuple[str, Row, _File]] = (),
        target_row: tuple[str, Row, _File] | None = None,
    ) -> None:
        key = (kind, source, target, None if target else text)
        pending = self.relations.get(key)
        if pending is None:
            self.relations[key] = _PendingRelation(
                kind, source, target, text, resolution, [source_row, *evidence], target_row
            )
            return
        pending.count += 1
        if len(pending.rows) < RELATION_ROW_LIMIT:
            pending.rows.extend(evidence[: RELATION_ROW_LIMIT - len(pending.rows)])

    def _emit_relations(self) -> list[SemanticRelation]:
        values: list[SemanticRelation] = []
        for pending in self.relations.values():
            rows = list(pending.rows)
            layout: Row = {"source": 0, "evidence": list(range(1, len(rows)))}
            if pending.target_row is not None:
                layout["target"] = len(rows)
                rows.append(pending.target_row)
            derived: Row = {
                "relation": pending.kind,
                "resolution": pending.resolution,
                "target_text": pending.text,
                "layout": layout,
                "count": pending.count,
            }
            fact = self._fact("relation", pending.source, rows, derived, attach=False)
            values.append(
                SemanticRelation(
                    len(values),
                    pending.kind,
                    pending.source,
                    pending.target,
                    pending.text if pending.target is None else None,
                    pending.resolution,
                    fact,
                )
            )
        return values

    # ----------------------------------------------------------------- entities

    def _entity(self, file: _File, kind: EntityKind, key: str, portable: str, name: str) -> _Entity:
        entity = _Entity(key, kind, file, portable, name, len(self.entities))
        self.entities.append(entity)
        self.by_key[key] = entity
        return entity

    def _limit(self, entity: _Entity, code: str, row: tuple[str, Row, _File]) -> None:
        if code in entity.limitations:
            return
        entity.limitations.append(code)
        self._fact("limitation", entity.key, [row], {"code": code})

    # ----------------------------------------------------------------- pass 1: per file

    def _prepare(self, item: ArtifactInput) -> _File:
        analysis = item.analysis
        language = analysis.get("language")
        python = language in {None, "Python"}
        identity = item.artifact_identity_id or f"artifact-{analysis['artifact_id']}"
        file = _File(item, str(analysis["locator"]), language, f"file:{identity}", python)
        file.extraction_digest = {
            str(row["id"]): str(row["contract_digest"]) for row in item.extractions
        }
        if analysis["outcome"] == "failed":
            file.limitations.append("STRUCTURE_FAILED")
            file.unavailable = "STRUCTURE_FAILED"
            return file
        if python:
            file.facts_extraction = next(
                (row for row in item.extractions if row["extractor"] == PYTHON_FACTS), None
            )
            current = self.source.extractor_digests.get(PYTHON_FACTS)
            facts = file.facts_extraction
            if facts is None:
                file.limitations.append("FACTS_NOT_EXTRACTED")
            elif facts["outcome"] not in {"extracted", "partial"}:
                file.limitations.append("FACTS_EXTRACTION_FAILED")
            elif facts["contract_digest"] != current:
                file.limitations.append("FACTS_CONTRACT_CHANGED")
            else:
                file.facts_usable = True
            file.usable = True
        else:
            declaration = next(
                (
                    row
                    for row in item.extractions
                    if row["id"] == analysis.get("declaration_extraction_id")
                ),
                None,
            )
            file.declaration = declaration
            if declaration is None or declaration[
                "contract_digest"
            ] != self.source.extractor_digests.get(str(declaration["extractor"])):
                file.limitations.append("EXTRACTOR_CONTRACT_CHANGED")
                file.unavailable = "EXTRACTOR_CONTRACT_CHANGED"
                return file
            file.usable = True
            file.facts_usable = True
            file.facts_extraction = declaration
            if declaration["outcome"] == "partial":
                file.partial = True
                file.limitations.append("PARTIAL_PARSE_ARTIFACT")
        facts_id = (
            file.facts_extraction["id"] if file.facts_usable and file.facts_extraction else None
        )
        artifact_facts = (file.facts_extraction or {}).get("artifact_facts") or {}
        if facts_id and int(artifact_facts.get("truncated_references") or 0):
            file.limitations.append("REFERENCES_TRUNCATED")
        file.symbols = sorted(item.symbols, key=lambda row: int(row["ordinal"]))
        file.by_id = {str(row["id"]): row for row in file.symbols}
        for row in file.symbols:
            file.children.setdefault(row.get("parent_id"), []).append(row)
        if facts_id:
            file.facts_by_symbol = {
                str(row["symbol_id"]): row
                for row in item.symbol_facts
                if row["extraction_id"] == facts_id
            }
            file.references = [row for row in item.references if row["extraction_id"] == facts_id]
            file.exports = [row for row in item.exports if row["extraction_id"] == facts_id]
        processing = item.processing
        if processing is None:
            file.unavailable = "NOT_PROCESSED"
        elif processing["outcome"] not in {"processed", "empty"}:
            file.unavailable = f"PROCESSING_{str(processing['outcome']).upper()}"
        elif not item.dependency_current:
            file.unavailable = "STRUCTURAL_DEPENDENCY_CHANGED"
        for chunk in item.chunks:
            data = str(chunk["content"])
            if hashlib.sha256(data.encode("utf-8")).hexdigest() != chunk["content_hash"] or len(
                data
            ) != int(chunk["end_offset"]) - int(chunk["start_offset"]):
                file.bad_chunks.add(str(chunk["id"]))
        return file

    def _file_limitation_row(self, file: _File, code: str) -> tuple[str, Row, _File]:
        if code in {"PARTIAL_PARSE_ARTIFACT", "EXTRACTOR_CONTRACT_CHANGED"} and file.declaration:
            return ("artifact_extractions", file.declaration, file)
        if code.startswith("FACTS_") and file.facts_extraction is not None:
            return ("artifact_extractions", file.facts_extraction, file)
        if code == "REFERENCES_TRUNCATED" and file.facts_extraction is not None:
            return ("artifact_extractions", file.facts_extraction, file)
        return ("artifact_analyses", file.source.analysis, file)

    def _symbols(self, file: _File, entity: _Entity) -> None:
        counts: dict[tuple[str, str], int] = defaultdict(int)
        portables: dict[str, str] = {}
        for row in file.symbols:
            slot = (str(row["qualified_name"]), str(row["kind"]))
            portables[str(row["id"])] = f"symbol:{file.locator}#{slot[0]}:{slot[1]}:{counts[slot]}"
            counts[slot] += 1
            file.keys[str(row["id"])] = f"symbol:{row['stable_identity_id']}"
        analysis_row = ("artifact_analyses", file.source.analysis, file)
        for row in file.symbols:
            key = file.keys[str(row["id"])]
            symbol_entity = self._entity(
                file, "symbol", key, portables[str(row["id"])], row["qualified_name"]
            )
            symbol_entity.symbol = row
            symbol_row = ("symbols", row, file)
            facts_row = file.facts_by_symbol.get(str(row["id"]))
            signature_rows = [symbol_row] + (
                [("symbol_facts", facts_row, file)] if facts_row else []
            )
            self._fact("signature", key, signature_rows, {"language": file.language})
            for code in file.limitations:
                self._limit(symbol_entity, code, self._file_limitation_row(file, code))
            facts = (facts_row or {}).get("facts") or {}
            if facts.get("dynamic_features") and facts_row:
                self._limit(symbol_entity, "DYNAMIC_FEATURES", ("symbol_facts", facts_row, file))
            if facts.get("ambient") and facts_row:
                self._limit(symbol_entity, "AMBIENT_DECLARATION", ("symbol_facts", facts_row, file))
            if mixed_script(str(row["name"])):
                self._limit(symbol_entity, "MIXED_SCRIPT_IDENTIFIER", symbol_row)
            parent = row.get("parent_id")
            if parent is None:
                self._relate("contains", entity.key, analysis_row, key, None, None, (), symbol_row)
            else:
                parent_row = file.by_id[str(parent)]
                self._relate(
                    "contains", file.keys[str(parent)], ("symbols", parent_row, file), key,
                    None, None, (), symbol_row,
                )  # fmt: skip
        owned: dict[str, list[Row]] = defaultdict(list)
        for ref in file.references:
            if ref.get("owner_symbol_id"):
                owned[str(ref["owner_symbol_id"])].append(ref)
        for row in file.symbols:
            refs = owned.get(str(row["id"]), [])
            key = file.keys[str(row["id"])]
            bases = [ref for ref in refs if ref["kind"] == "base"]
            if bases:
                self._fact(
                    "base",
                    key,
                    [("symbols", row, file), *(("symbol_references", ref, file) for ref in bases)],
                    {},
                )
            seen: set[tuple[str, str]] = set()
            listed: list[Row] = []
            for ref in refs:
                if ref["kind"] in {"base", "decorator"}:
                    continue
                pair = (str(ref["kind"]), str(ref["target"]))
                if pair not in seen:
                    seen.add(pair)
                    listed.append(ref)
            if listed:
                self._fact(
                    "reference",
                    key,
                    [
                        ("symbols", row, file),
                        *(
                            ("symbol_references", ref, file)
                            for ref in listed[:REFERENCE_FACT_LIMIT]
                        ),
                    ],
                    {"omitted": max(0, len(listed) - REFERENCE_FACT_LIMIT)},
                )
            self._same_file_references(file, row, refs)

    def _same_file_references(self, file: _File, row: Row, refs: list[Row]) -> None:
        key = file.keys[str(row["id"])]
        parent = file.by_id.get(str(row.get("parent_id") or ""))
        for ref in refs:
            if ref["kind"] != "call":
                continue
            target = str(ref["target"])
            head, _, rest = target.partition(".")
            found: Row | None = None
            if not rest:
                found = file.top(head)
            elif head in {"self", "this", "cls"} and parent is not None and "." not in rest:
                found = next(
                    (
                        child
                        for child in file.children.get(str(parent["id"]), [])
                        if child["name"] == rest
                    ),
                    None,
                )
            if found is None or found["id"] == row["id"]:
                continue
            self._relate(
                "references", key, ("symbols", row, file), file.keys[str(found["id"])], None,
                "same_file", [("symbol_references", ref, file)], ("symbols", found, file),
            )  # fmt: skip

    def _file_facts(self, file: _File, entity: _Entity) -> None:
        for code in file.limitations:
            self._limit(entity, code, self._file_limitation_row(file, code))
        if not file.usable:
            return
        for row in file.source.imports:
            self._fact(
                "import",
                entity.key,
                [("import_references", row, file)],
                {"language": file.language},
            )
        for row in file.exports:
            self._fact("export", entity.key, [("export_references", row, file)], {})

    def _roles(self, file: _File, entity: _Entity) -> None:
        view = ArtifactView(
            file.source.analysis,
            tuple(file.symbols),
            tuple(file.source.imports),
            file.facts_by_symbol,
            tuple(file.references),
            file.facts_usable,
        )
        for match in recognize(view):
            self.matches[f"{match.recognizer}:{match.strength}"] += 1
            if match.subject_table == "symbols":
                subject_key = file.keys[str(match.subject["id"])]
                subject = self.by_key[subject_key]
                identity = str(match.subject["stable_identity_id"])
            else:
                subject_key, subject, identity = entity.key, entity, entity.key.split(":", 1)[1]
            if match.role == "api_endpoint":
                key = f"api_endpoint:{identity}:{match.recognizer}:{match.ordinal}"
                portable = f"api_endpoint:{subject.portable}:{match.recognizer}:{match.ordinal}"
            elif match.role == "db_table" and match.recognizer == "alembic-table.v1":
                artifact = entity.key.split(":", 1)[1]
                key = f"db_table:{artifact}:{match.ordinal}"
                portable = f"db_table:{file.locator}:{match.ordinal}"
            else:
                key = f"{match.role}:{identity}"
                portable = f"{match.role}:{subject.portable}"
            if key in self.by_key:
                continue
            role = self._entity(file, match.role, key, portable, match.name)
            role.symbol = subject.symbol
            role.subject = subject_key
            role.match = match
            file.roles[(match.role, subject_key)] = key
            for code in subject.limitations:
                self._limit(role, code, self._file_limitation_row(file, code))
            rows = [(item.table, item.row, file) for item in match.evidence]
            self._fact(
                "recognizer_match",
                key,
                [(match.subject_table, match.subject, file), *rows],
                {
                    "recognizer": match.recognizer,
                    "strength": match.strength,
                    "met": list(match.met),
                    "unmet": list(match.unmet),
                    "attributes": dict(match.attributes),
                },
            )
            role.facts.extend(item for item in subject.facts if item not in role.facts)
            relation: RelationKind | None = (
                "defines_route"
                if match.role == "api_endpoint"
                else "maps_table"
                if match.role == "db_table"
                else None
            )
            if relation is not None:
                self._relate(
                    relation, subject_key, (match.subject_table, match.subject, file), key,
                    f"{match.role} {match.name}", None, rows[:2],
                )  # fmt: skip

    def _bindings(self, file: _File) -> None:
        for row in file.source.imports:
            form, name, alias = row["form"], str(row["name"]), row.get("alias")
            if form == "import":
                local = alias or name.split(".")[0]
                file.bindings.setdefault(str(local), (row, None))
            elif form == "from":
                if name != "*":
                    file.bindings.setdefault(str(alias or name), (row, name))
            elif form in {"es_import", "es_import_type"} and name:
                local = alias or name
                file.bindings.setdefault(str(local), (row, None if name == "*" else name))

    # ----------------------------------------------------------------- pass 2: cross-file

    def _resolve(self, file: _File, row: Row) -> Resolved:
        value = file.resolutions.get(str(row["id"]))
        if value is None:
            if file.python:
                value = self.resolver.python(file.locator, row)
            else:
                value = self.resolver.specifier(file.locator, row.get("from_module"))
            file.resolutions[str(row["id"])] = value
        return value

    def _target_file(self, resolved: Resolved) -> _File | None:
        return (
            self.by_locator.get(resolved.locator or "") if resolved.status == "resolved" else None
        )

    def _exported(self, target: _File, name: str) -> Row | None:
        if not target.usable:
            return None
        if name == "default":
            local = next(
                (row.get("local_name") for row in target.exports if row["kind"] == "default"), None
            )
            found = next(
                (row for row in target.children.get(None, []) if row.get("exported") == "default"),
                None,
            )
            return found or (target.top(str(local)) if local else None)
        return target.top(name)

    def _imports(self, file: _File, entity: _Entity) -> None:
        analysis_row = ("artifact_analyses", file.source.analysis, file)
        aliases = False
        for row in file.source.imports:
            resolved = self._resolve(file, row)
            module = row.get("from_module") if row["form"] != "import" else row["name"]
            if file.python and int(row.get("level") or 0):
                module = "." * int(row["level"]) + str(module or "")
            target = self._target_file(resolved)
            if resolved.status == "not_attempted" and module:
                aliases = True
            if target is not None:
                inventory = self.inventory.get(target.locator)
                self._relate(
                    "imports", entity.key, analysis_row, target.key, None, "resolved",
                    [("import_references", row, file)],
                    ("artifacts", inventory, target) if inventory else None,
                )  # fmt: skip
            elif resolved.status == "resolved" and resolved.locator in self.inventory:
                self._relate(
                    "imports", entity.key, analysis_row, None, resolved.locator, "resolved",
                    [("import_references", row, file)],
                    ("artifacts", self.inventory[resolved.locator], file),
                )  # fmt: skip
            else:
                self._relate(
                    "imports", entity.key, analysis_row, None, str(module or row["name"]),
                    resolved.status, [("import_references", row, file)],
                )  # fmt: skip
        if aliases:
            self._limit(entity, "UNRESOLVED_ALIAS_IMPORTS", analysis_row)
        for row in file.exports:
            kind = row["kind"]
            export_row = ("export_references", row, file)
            if kind in {"reexport", "reexport_all"}:
                resolved = self.resolver.specifier(file.locator, row.get("source_module"))
                target = self._target_file(resolved)
                if target is not None:
                    self._relate(
                        "exports", entity.key, analysis_row, target.key, None, "resolved",
                        [export_row],
                    )  # fmt: skip
                else:
                    self._relate(
                        "exports", entity.key, analysis_row, None, str(row.get("source_module")),
                        resolved.status, [export_row],
                    )  # fmt: skip
                continue
            owner = file.by_id.get(str(row.get("owner_symbol_id") or ""))
            name = row.get("local_name") or row["name"]
            found = owner or (file.top(str(name)) if name else None)
            if found is not None:
                self._relate(
                    "exports", entity.key, analysis_row, file.keys[str(found["id"])], None,
                    "same_file", [export_row], ("symbols", found, file),
                )  # fmt: skip
            else:
                self._relate(
                    "exports", entity.key, analysis_row, None, str(row["name"]), "unresolved",
                    [export_row],
                )  # fmt: skip

    def _bind(
        self, file: _File, target: str
    ) -> tuple[_File | None, Row | None, Resolution, Row | None]:
        """One-hop name binding: a same-file declaration or an imported declaration."""
        head, _, rest = target.partition(".")
        local = file.top(head)
        if local is not None:
            return file, local, "same_file", None
        binding = file.bindings.get(head)
        if binding is None:
            return None, None, "unresolved", None
        row, exported = binding
        resolved = self._resolve(file, row)
        destination = self._target_file(resolved)
        if destination is None:
            return (
                None,
                None,
                resolved.status if resolved.status != "resolved" else "unresolved",
                row,
            )
        if exported is None or resolved.submodule:
            member = rest.split(".")[0] if rest else None
            if row["form"] == "import" and not row.get("alias"):
                dotted = str(row["name"])
                member = (
                    target[len(dotted) + 1 :].split(".")[0]
                    if target.startswith(dotted + ".")
                    else None
                )
        else:
            member = exported
        found = self._exported(destination, member) if member else None
        return (destination, found, "imported", row) if found else (None, None, "unresolved", row)

    def _bound_relations(self, file: _File) -> None:
        for ref in file.references:
            kind = ref["kind"]
            relations: dict[str, RelationKind] = {
                "jsx_element": "renders",
                "hook_call": "uses_hook",
                "base": "inherits",
            }
            relation = relations.get(kind)
            owner = file.by_id.get(str(ref.get("owner_symbol_id") or ""))
            if relation is None or owner is None:
                continue
            target = str(ref["target"])
            if kind == "jsx_element" and not target[:1].isupper() and "." not in target:
                continue
            destination, found, status, import_row = self._bind(file, target)
            source_key = file.keys[str(owner["id"])]
            evidence = [("symbol_references", ref, file)]
            if import_row is not None:
                evidence.append(("import_references", import_row, file))
            if destination is not None and found is not None:
                found_key = destination.keys[str(found["id"])]
                role = (
                    "component"
                    if relation == "renders"
                    else "hook"
                    if relation == "uses_hook"
                    else None
                )
                target_key = (
                    destination.roles.get((role, found_key), found_key) if role else found_key
                )
                if target_key == source_key:
                    continue
                self._relate(
                    relation, source_key, ("symbols", owner, file), target_key, None, status,
                    evidence, ("symbols", found, destination),
                )  # fmt: skip
            else:
                self._relate(
                    relation, source_key, ("symbols", owner, file), None, target, status, evidence
                )

    def _neighbors(self, file: _File) -> dict[str, list[str]]:
        """Signature facts of resolved import targets, per local binding name."""
        values: dict[str, list[str]] = {}
        for name, (row, exported) in sorted(file.bindings.items()):
            resolved = self._resolve(file, row)
            destination = self._target_file(resolved)
            if destination is None:
                continue
            if exported is not None and not resolved.submodule:
                found = self._exported(destination, exported)
                rows = [found] if found else []
            else:
                rows = [
                    item
                    for item in destination.children.get(None, [])
                    if destination.python or item.get("exported")
                ]
            facts: list[str] = []
            for item in rows:
                entity = self.by_key.get(destination.keys.get(str(item["id"]), ""))
                facts.extend(
                    value
                    for value in (entity.facts if entity else [])
                    if self.facts[value].fact_kind == "signature"
                )
            if facts:
                values[name] = facts
        return values

    # ----------------------------------------------------------------- coverage and packages

    def _regions(self, row: Row, file: _File) -> list[_Region]:
        if file.python or row.get("start_offset") is None:
            first = int(row.get("full_start_line") or row["start_line"])
            return [_Region(True, first, int(row["end_line"]))]
        spans = [(int(row["start_offset"]), int(row["end_offset"]))]
        spans += [(int(a), int(b)) for a, b in row.get("additional_spans") or []]
        return [_Region(False, a, b) for a, b in spans if b > a]

    def _descendants(self, file: _File, row: Row) -> list[Row]:
        values, stack = [], [str(row["id"])]
        while stack:
            for child in file.children.get(stack.pop(), []):
                values.append(child)
                stack.append(str(child["id"]))
        return values

    def _coverage(
        self,
        file: _File,
        identities: set[str] | None,
        regions: list[_Region] | None,
        required: set[str],
    ) -> tuple[StoredCoverage, str | None, list[Row], Row]:
        """Stored-source coverage; ``required`` identities (the entity and every declaration
        inside it) must each own at least one verified chunk."""
        if file.unavailable:
            return "unavailable", file.unavailable, [], {}
        chunks = sorted(
            (
                chunk
                for chunk in file.source.chunks
                if identities is None or str(chunk.get("symbol_identity_id")) in identities
            ),
            key=lambda chunk: (int(chunk["start_offset"]), int(chunk["end_offset"])),
        )
        gaps = {"count": 0, "characters": 0}
        for left, right in zip(chunks, chunks[1:], strict=False):
            if int(right["start_offset"]) < int(left["end_offset"]):
                return "coordinate_mismatch", "CHUNK_OVERLAP", chunks, gaps
            if int(right["start_offset"]) > int(left["end_offset"]):
                gaps["count"] += 1
                gaps["characters"] += int(right["start_offset"]) - int(left["end_offset"])
        if any(str(chunk["id"]) in file.bad_chunks for chunk in chunks):
            return "coordinate_mismatch", "CHUNK_INTEGRITY", chunks, gaps
        owners = {str(chunk.get("symbol_identity_id")) for chunk in chunks}
        if regions is not None:
            if not chunks:
                return "incomplete", "NO_STORED_CHUNKS", chunks, gaps
            if any(not any(region.contains(chunk) for region in regions) for chunk in chunks):
                return "coordinate_mismatch", "OUTSIDE_EXPECTED_REGION", chunks, gaps
            for region in regions:
                inside = [chunk for chunk in chunks if region.contains(chunk)]
                if not inside:
                    return "incomplete", "REGION_WITHOUT_CHUNK", chunks, gaps
                if not region.starts(inside[0]):
                    return "coordinate_mismatch", "REGION_START_MISMATCH", chunks, gaps
        if required - owners:
            return "incomplete", "DECLARATION_WITHOUT_CHUNK", chunks, gaps
        return "complete", None, chunks, gaps

    def _bytes(self, chunks: Sequence[Row], facts: Iterable[str]) -> int:
        return sum(len(str(chunk["content"]).encode()) for chunk in chunks) + sum(
            len(self.facts[value].rendered_text.encode()) for value in facts
        )

    def _finish(
        self, entity: _Entity, neighbors: dict[str, dict[str, list[str]]]
    ) -> tuple[SemanticEntity, Row]:
        file = entity.file
        subject = self.by_key.get(entity.subject or "") if entity.subject else None
        base = subject if subject is not None and subject.kind in {"symbol", "file"} else entity
        row = base.symbol if base.kind == "symbol" else None
        if row is not None:
            descendants = self._descendants(file, row)
            identities = {str(row["stable_identity_id"])} | {
                str(item["stable_identity_id"]) for item in descendants
            }
            stored, reason, chunks, gaps = self._coverage(
                file, identities, self._regions(row, file), identities
            )
            children = file.children.get(str(row["id"]), [])
            own = [
                chunk
                for chunk in chunks
                if str(chunk.get("symbol_identity_id")) == str(row["stable_identity_id"])
            ]
            container = bool(children)
        else:
            descendants = list(file.symbols)
            stored, reason, chunks, gaps = self._coverage(
                file, None, None, {str(item["stable_identity_id"]) for item in file.symbols}
            )
            children = file.children.get(None, [])
            own = [chunk for chunk in chunks if chunk.get("symbol_identity_id") is None]
            container = True
        names = self._referenced_names(file, [row, *descendants] if row is not None else [])
        neighbor_facts: list[str] = []
        lookup = neighbors.get(file.key, {})
        for name in sorted(lookup) if row is None else names:
            for value in lookup.get(name, []):
                if value not in neighbor_facts and len(neighbor_facts) < NEIGHBOR_FACT_LIMIT:
                    neighbor_facts.append(value)
        own_facts = list(dict.fromkeys(entity.facts))
        relation_facts = [item.fact_digest for item in self.relation_facts.get(base.key, [])]
        own_facts += [value for value in relation_facts if value not in own_facts]
        mode: ContextCoverage
        context_reason: str | None = None
        omitted: list[OmittedRegion] = []
        package_chunks: list[Row] = []
        child_facts: list[str] = []
        if stored != "complete":
            mode, context_reason = "not_eligible", reason or f"STORED_SOURCE_{stored.upper()}"
        elif base.kind == "file" and file.partial:
            mode, context_reason = "not_eligible", "PARTIAL_PARSE_ARTIFACT"
        else:
            full_bytes = self._bytes(chunks, own_facts + neighbor_facts)
            if base.kind == "symbol" and full_bytes <= PACKAGE_BYTE_CEILING:
                mode, package_chunks = "full", chunks
            elif container:
                mode, package_chunks = "outline", own
                for child in children:
                    child_key = file.keys[str(child["id"])]
                    child_entity = self.by_key[child_key]
                    child_facts += [
                        value
                        for value in child_entity.facts
                        if self.facts[value].fact_kind == "signature"
                    ]
                    omitted.append(
                        OmittedRegion(
                            child_key,
                            child.get("full_start_line") or child["start_line"],
                            child["end_line"],
                        )
                    )
                if (
                    self._bytes(own, own_facts + child_facts + neighbor_facts)
                    > PACKAGE_BYTE_CEILING
                ):
                    mode, context_reason, package_chunks, omitted, child_facts = (
                        "not_eligible", "CONTEXT_BUDGET_EXCEEDED", [], [], [],
                    )  # fmt: skip
            else:
                mode, context_reason = "not_eligible", "CONTEXT_BUDGET_EXCEEDED"
        package_facts = list(dict.fromkeys(own_facts + child_facts))
        package_digest = (
            package_digest_of(
                mode,
                [
                    (str(chunk["stable_key"]), str(chunk["content_hash"]))
                    for chunk in package_chunks
                ],
                package_facts,
                neighbor_facts,
                [(item.entity_key, item.start_line, item.end_line) for item in omitted],
            )
            if mode != "not_eligible"
            else None
        )
        package_bytes = (
            self._bytes(package_chunks, package_facts + neighbor_facts) if package_digest else 0
        )
        portable = {
            "package": [
                [
                    [chunk["content_hash"], chunk["start_offset"], chunk["end_offset"]]
                    for chunk in package_chunks
                ],
                sorted(self._portable_fact(item) for item in package_facts),
                [self._portable_fact(item) for item in neighbor_facts],
                [
                    [self.by_key[item.entity_key].portable, item.start_line, item.end_line]
                    for item in omitted
                ],
            ]
            if package_digest
            else None
        }
        content_digest = (
            digest(
                {
                    "version": PACKAGE_VERSION,
                    "entity": entity.portable,
                    "kind": entity.kind,
                    "mode": mode,
                    "package": portable["package"],
                }
            )
            if package_digest
            else None
        )
        symbol = entity.symbol
        result = SemanticEntity(
            entity.key,
            entity.kind,
            file.language,
            file.locator,
            _uuid(file.source.artifact_identity_id),
            _uuid(symbol["stable_identity_id"]) if symbol is not None else None,
            entity.subject,
            entity.name,
            symbol["qualified_name"] if symbol is not None else None,
            symbol["kind"] if symbol is not None else None,
            int(symbol.get("full_start_line") or symbol["start_line"])
            if symbol is not None
            else None,
            int(symbol["end_line"]) if symbol is not None else None,
            entity.ordinal,
            entity.portable,
            stored,
            reason,
            mode,
            context_reason,
            package_digest,
            package_bytes,
            tuple(entity.limitations),
            tuple(
                EvidenceChunk(
                    UUID(str(chunk["id"])),
                    str(chunk["stable_key"]),
                    str(chunk["content_hash"]),
                    int(chunk["start_offset"]),
                    int(chunk["end_offset"]),
                    int(chunk["start_line"]),
                    int(chunk["end_line"]),
                    _uuid(chunk.get("symbol_identity_id")),
                )
                for chunk in chunks
            ),
            gaps,
            tuple(str(chunk["stable_key"]) for chunk in package_chunks),
            tuple(package_facts),
            tuple(neighbor_facts),
            tuple(omitted),
            content_digest,
        )
        return result, portable

    def _referenced_names(self, file: _File, rows: list[Row]) -> list[str]:
        ids = {str(row["id"]) for row in rows}
        names: list[str] = []
        for ref in file.references:
            if str(ref.get("owner_symbol_id")) in ids:
                head = str(ref["target"]).split(".")[0]
                if head in file.bindings and head not in names:
                    names.append(head)
        return names

    def _portable_fact(self, value: str) -> str:
        fact = self.facts[value]
        subject = self.by_key.get(fact.subject_entity_key)
        return digest([fact.fact_kind, subject.portable if subject else None, fact.rendered_text])

    # ----------------------------------------------------------------- run

    def run(self) -> CatalogBuild:
        for item in sorted(self.source.artifacts, key=lambda value: str(value.analysis["locator"])):
            if item.analysis["outcome"] == "skipped":
                self.skipped += 1
                continue
            file = self._prepare(item)
            self.files.append(file)
            self.by_locator[file.locator] = file
        for file in self.files:
            entity = self._entity(file, "file", file.key, f"file:{file.locator}", file.locator)
            self._file_facts(file, entity)
            if file.usable:
                self._symbols(file, entity)
                self._roles(file, entity)
                self._bindings(file)
        for file in self.files:
            if file.usable:
                self._imports(file, self.by_key[file.key])
                self._bound_relations(file)
        relations = self._emit_relations()
        for relation in relations:
            if relation.kind != "contains":
                self.relation_facts[relation.source_entity_key].append(relation)
        neighbors = {file.key: self._neighbors(file) for file in self.files if file.usable}
        finished = [self._finish(entity, neighbors) for entity in self.entities]
        entities = tuple(value for value, _ in finished)
        facts = tuple(sorted(self.facts.values(), key=lambda value: value.ordinal))
        return CatalogBuild(
            entities,
            facts,
            tuple(relations),
            self._catalog_digest(entities, facts, relations),
            self._portable_digest(finished, facts, relations),
            self._summary(entities, facts, relations),
            self._outcome(entities),
        )

    def _catalog_digest(
        self,
        entities: Sequence[SemanticEntity],
        facts: Sequence[SemanticFact],
        relations: Sequence[SemanticRelation],
    ) -> str:
        return digest(
            {
                "entities": _sorted(
                    [
                        item.entity_key,
                        item.kind,
                        item.stored_coverage,
                        item.coverage_reason,
                        item.context_coverage,
                        item.context_reason,
                        list(item.limitations),
                        item.package_digest,
                    ]
                    for item in entities
                ),
                "facts": sorted(item.fact_digest for item in facts),
                "relations": _sorted(
                    [
                        item.kind,
                        item.source_entity_key,
                        item.target_entity_key,
                        item.target_text,
                        item.resolution_status,
                        item.fact_digest,
                    ]
                    for item in relations
                ),
            }
        )

    def _portable_digest(
        self,
        finished: Sequence[tuple[SemanticEntity, Row]],
        facts: Sequence[SemanticFact],
        relations: Sequence[SemanticRelation],
    ) -> str:
        def portable(key: str | None) -> str | None:
            return self.by_key[key].portable if key in self.by_key else key

        return digest(
            {
                "entities": _sorted(
                    [
                        item.portable_key,
                        item.kind,
                        item.language,
                        item.stored_coverage,
                        item.coverage_reason,
                        item.context_coverage,
                        item.context_reason,
                        list(item.limitations),
                        extra["package"],
                    ]
                    for item, extra in finished
                ),
                "facts": _sorted(
                    [portable(item.subject_entity_key), item.fact_kind, item.rendered_text]
                    for item in facts
                ),
                "relations": _sorted(
                    [
                        item.kind,
                        portable(item.source_entity_key),
                        portable(item.target_entity_key),
                        item.target_text,
                        item.resolution_status,
                    ]
                    for item in relations
                ),
            }
        )

    def _summary(
        self,
        entities: Sequence[SemanticEntity],
        facts: Sequence[SemanticFact],
        relations: Sequence[SemanticRelation],
    ) -> dict[str, object]:
        def count(values: Iterable[str | None]) -> dict[str, int]:
            result: dict[str, int] = defaultdict(int)
            for value in values:
                result[str(value)] += 1
            return dict(sorted(result.items()))

        return {
            "artifacts": len(self.files),
            "skipped_artifacts": self.skipped,
            "entities": len(entities),
            "entity_kinds": count(item.kind for item in entities),
            "stored_coverage": count(item.stored_coverage for item in entities),
            "context_coverage": count(item.context_coverage for item in entities),
            "limitations": count(code for item in entities for code in item.limitations),
            "facts": len(facts),
            "fact_kinds": count(item.fact_kind for item in facts),
            "relations": len(relations),
            "relation_kinds": count(item.kind for item in relations),
            "import_resolution": count(
                item.resolution_status for item in relations if item.kind == "imports"
            ),
            "recognizer_matches": dict(sorted(self.matches.items())),
        }

    def _outcome(self, entities: Sequence[SemanticEntity]) -> CatalogOutcome:
        if (
            self.source.structural_outcome != "complete"
            or self.source.processing_outcome != "complete"
            or any(item.stored_coverage == "coordinate_mismatch" for item in entities)
        ):
            return "partial"
        return "complete"


def _sorted(values: Iterable[Any]) -> list[Any]:
    """Total order over mixed JSON values (``None`` and strings never compare directly)."""
    return sorted(values, key=lambda value: json.dumps(value, sort_keys=True))


def _uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except ValueError:
        return None


def build_catalog(source: CatalogInput) -> CatalogBuild:
    return _Builder(source).run()


def input_authority_digest(
    snapshot_id: UUID,
    structural: StructuralAnalysis,
    processing: ProcessingRun,
    contract: dict[str, object],
) -> str:
    return digest(
        {
            "snapshot": str(snapshot_id),
            "structural": [str(structural.id), structural.revision],
            "processing": [str(processing.id), processing.revision, processing.processing_contract],
            "catalog": contract,
        }
    )


# --------------------------------------------------------------------------- service


@dataclass(frozen=True, slots=True)
class EntityFilters:
    kind: str | None = None
    language: str | None = None
    locator_prefix: str | None = None
    stored_coverage: str | None = None
    context_coverage: str | None = None
    query: str | None = None
    keys: tuple[str, ...] | None = None


class SemanticCatalogStore(Protocol):
    def load_input(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        structural: StructuralAnalysis,
        processing: ProcessingRun,
    ) -> CatalogInput: ...
    def latest(self, project_id: UUID, snapshot_id: UUID) -> SemanticCatalogRun | None: ...
    def active(self, project_id: UUID, snapshot_id: UUID) -> SemanticCatalogRun | None: ...
    def run(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID
    ) -> SemanticCatalogRun | None: ...
    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[SemanticCatalogRun], int]: ...
    def publish(self, run: SemanticCatalogRun, build: CatalogBuild) -> SemanticCatalogRun: ...
    def save_failure(self, run: SemanticCatalogRun) -> SemanticCatalogRun: ...
    def entity_page(
        self, run_id: UUID, filters: EntityFilters, limit: int, offset: int
    ) -> tuple[list[SemanticEntity], int]: ...
    def entity(self, run_id: UUID, entity_key: str) -> SemanticEntity | None: ...
    def facts(self, run_id: UUID, digests: Sequence[str]) -> list[SemanticFact]: ...
    def subject_facts(self, run_id: UUID, entity_key: str) -> list[SemanticFact]: ...
    def relations(
        self, run_id: UUID, entity_key: str, limit: int
    ) -> tuple[list[SemanticRelation], list[SemanticRelation]]: ...
    def verify_facts(self, facts: Sequence[SemanticFact]) -> dict[str, str]: ...
    def labels(self, run_id: UUID, entity_keys: Sequence[str]) -> dict[str, str]: ...
    def names(self, run_id: UUID, entity_keys: Sequence[str]) -> dict[str, tuple[str, ...]]: ...
    def fact_rows(self, facts: Sequence[SemanticFact]) -> dict[str, list[Row]]: ...
    def artifact_ranges(
        self, structural_analysis_id: UUID, artifact_identity_id: UUID | None
    ) -> dict[str, tuple[tuple[int, int], ...]] | None: ...
    def evidence(
        self, run: SemanticCatalogRun, chunks: Sequence[EvidenceChunk]
    ) -> list[EvidenceChunk]: ...


class StructuralAuthority(Protocol):
    def latest_analysis(self, project_id: UUID, snapshot_id: UUID) -> StructuralAnalysis | None: ...


@dataclass(frozen=True, slots=True)
class CatalogStatus:
    latest_attempt: SemanticCatalogRun | None
    active_run: SemanticCatalogRun | None
    freshness: Literal["fresh", "stale", "none"]
    freshness_reasons: tuple[str, ...]
    contract: dict[str, object]


class SemanticCatalogService:
    def __init__(
        self,
        store: SemanticCatalogStore,
        structure: StructuralAuthority,
        processing: ProcessingService,
        lock: Lock | None = None,
    ) -> None:
        self._store, self._structure, self._processing = store, structure, processing
        self._lock = lock or Lock()

    def require_snapshot(self, project_id: UUID, snapshot_id: UUID) -> None:
        self._processing.require_snapshot(project_id, snapshot_id)

    def freshness(
        self, project_id: UUID, snapshot_id: UUID, run: SemanticCatalogRun
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        if run.quarantined:
            reasons.append("CATALOG_QUARANTINED")
        if run.outcome == "failed":
            reasons.append("CATALOG_FAILED")
        if run.catalog_contract != catalog_contract():
            reasons.append("CATALOG_CONTRACT_CHANGED")
        structural = self._structure.latest_analysis(project_id, snapshot_id)
        if structural is None or structural.id != run.structural_analysis_id:
            reasons.append("STRUCTURAL_AUTHORITY_CHANGED")
        status = self._processing.status(project_id, snapshot_id)
        active = status.active_run
        if (
            active is None
            or active.id != run.processing_run_id
            or active.revision != run.processing_revision
            or active.processing_contract != run.processing_contract
        ):
            reasons.append("PROCESSING_AUTHORITY_CHANGED")
        else:
            reasons.extend(status.freshness_reasons)
        return tuple(dict.fromkeys(reasons))

    def status(self, project_id: UUID, snapshot_id: UUID) -> CatalogStatus:
        self.require_snapshot(project_id, snapshot_id)
        latest = self._store.latest(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        reasons = self.freshness(project_id, snapshot_id, active) if active else ()
        state: Literal["fresh", "stale", "none"] = (
            "none" if active is None else "stale" if reasons else "fresh"
        )
        return CatalogStatus(latest, active, state, reasons, catalog_contract())

    def build(self, project_id: UUID, snapshot_id: UUID) -> SemanticCatalogRun:
        self.require_snapshot(project_id, snapshot_id)
        if not self._lock.acquire(False):
            raise SemanticCatalogBusyError("Another catalog operation is running.")
        try:
            started = datetime.now(UTC)
            structural = self._structure.latest_analysis(project_id, snapshot_id)
            if structural is None:
                raise SemanticPrerequisiteError("Current structural evidence is required.")
            processing = self._processing.current_authority(project_id, snapshot_id)
            contract = catalog_contract()
            authority = input_authority_digest(snapshot_id, structural, processing, contract)
            active = self._store.active(project_id, snapshot_id)
            latest = self._store.latest(project_id, snapshot_id)
            if (
                active is not None
                and active.input_authority_digest == authority
                and not self.freshness(project_id, snapshot_id, active)
            ):
                return active
            candidate = SemanticCatalogRun(
                uuid4(),
                project_id,
                snapshot_id,
                0,
                started,
                started,
                "failed",
                structural.id,
                structural.revision,
                processing.id,
                processing.revision,
                dict(processing.processing_contract),
                contract,
                authority,
                None,
                None,
                {},
                (),
                captured_active_catalog_run_id=active.id if active else None,
                captured_latest_revision=latest.revision if latest else 0,
            )
            try:
                source = self._store.load_input(project_id, snapshot_id, structural, processing)
                build = build_catalog(source)
                run = replace(
                    candidate,
                    completed_at=datetime.now(UTC),
                    outcome=build.outcome,
                    catalog_digest=build.catalog_digest,
                    portable_digest=build.portable_digest,
                    coverage=build.coverage,
                    diagnostics=build.diagnostics,
                )
                return self._store.publish(run, build)
            except SemanticCatalogConflictError:
                self._store.save_failure(
                    replace(
                        candidate,
                        completed_at=datetime.now(UTC),
                        diagnostics=("CATALOG_AUTHORITY_CHANGED",),
                    )
                )
                raise
        finally:
            self._lock.release()

    def run(self, project_id: UUID, snapshot_id: UUID, run_id: UUID) -> SemanticCatalogRun:
        self.require_snapshot(project_id, snapshot_id)
        run = self._store.run(project_id, snapshot_id, run_id)
        if run is None:
            raise SemanticNotFoundError("Catalog run was not found.")
        return run

    def history(
        self, project_id: UUID, snapshot_id: UUID, limit: int, offset: int
    ) -> tuple[list[SemanticCatalogRun], int]:
        self.require_snapshot(project_id, snapshot_id)
        return self._store.history(project_id, snapshot_id, limit, offset)

    def select(
        self, project_id: UUID, snapshot_id: UUID, run_id: UUID | None
    ) -> tuple[SemanticCatalogRun, tuple[str, ...]]:
        """Current reads need a fresh active catalog (F1); explicit history is labelled."""
        if run_id is not None:
            run = self.run(project_id, snapshot_id, run_id)
            if run.outcome == "failed":
                raise SemanticNotFoundError("Failed catalog runs have no entities.")
            return run, self.freshness(project_id, snapshot_id, run)
        self.require_snapshot(project_id, snapshot_id)
        active = self._store.active(project_id, snapshot_id)
        if active is None:
            raise SemanticCatalogRequiredError("Build the semantic catalog first.")
        reasons = self.freshness(project_id, snapshot_id, active)
        if reasons:
            raise SemanticCatalogStaleError("The semantic catalog is stale; rebuild it explicitly.")
        return active, ()

    def entity_page(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        run_id: UUID | None,
        filters: EntityFilters,
        limit: int,
        offset: int,
    ) -> tuple[SemanticCatalogRun, tuple[str, ...], list[SemanticEntity], int]:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        values, total = self._store.entity_page(run.id, filters, limit, offset)
        return run, reasons, values, total

    def entity(
        self, project_id: UUID, snapshot_id: UUID, entity_key: str, run_id: UUID | None = None
    ) -> EntityDetail:
        run, reasons = self.select(project_id, snapshot_id, run_id)
        entity = self._store.entity(run.id, entity_key)
        if entity is None:
            raise SemanticNotFoundError("Entity was not found in the selected catalog.")
        own = self._store.subject_facts(run.id, entity_key)
        listed = {fact.fact_digest for fact in own}
        extra = [item for item in entity.package_facts if item not in listed]
        package = self._store.facts(run.id, [*extra, *entity.neighbor_facts])
        by_digest = {fact.fact_digest: fact for fact in package}
        verification = self._store.verify_facts([*own, *package])
        own = [replace(fact, verification=verification.get(fact.fact_digest)) for fact in own]
        own += [
            replace(by_digest[item], verification=verification.get(item))
            for item in extra
            if item in by_digest
        ]
        neighbors = [
            replace(by_digest[item], verification=verification.get(item))
            for item in entity.neighbor_facts
            if item in by_digest
        ]
        evidence = self._store.evidence(run, entity.evidence)
        errors = [
            f"{value}:{key[:12]}"
            for key, value in sorted(verification.items())
            if value != "verified"
        ]
        errors += [
            f"CHUNK_INTEGRITY:{chunk.stable_key[:12]}"
            for chunk in evidence
            if chunk.verified is False
        ]
        outgoing, incoming = self._store.relations(run.id, entity_key, 500)
        named = {
            key
            for item in [*outgoing, *incoming]
            for key in (item.source_entity_key, item.target_entity_key)
            if key
        }
        return EntityDetail(
            run,
            replace(entity, evidence=tuple(evidence)),
            tuple(own),
            tuple(neighbors),
            tuple(outgoing),
            tuple(incoming),
            tuple(errors),
            reasons,
            self._store.labels(run.id, sorted(named)),
        )

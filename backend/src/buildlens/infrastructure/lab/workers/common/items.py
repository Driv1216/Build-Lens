"""Framework-agnostic helpers shared by the LangChain and LlamaIndex workers."""

from __future__ import annotations

import json
import re
from typing import Any

from buildlens.domain.lab import LabError, sha256_hex


class DocumentSet:
    """Documents received from the parent Gateway for one index build."""

    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.by_ref: dict[str, dict[str, Any]] = {}
        self.reuse: dict[str, list[float]] = {}
        self.bytes = 0

    def add(self, batch: list[dict[str, Any]]) -> None:
        for raw in batch:
            ref = str(raw["ref"])
            if ref in self.by_ref:
                raise LabError("LAB_DOCUMENT_DUPLICATE")
            text = raw["text"]
            if not isinstance(text, str):
                raise LabError("LAB_TRANSFER_INVALID")
            document = {"ref": ref, "text": text, "metadata": dict(raw.get("metadata") or {})}
            self.documents.append(document)
            self.by_ref[ref] = document
            self.bytes += len(text.encode())
            vector = raw.get("vector")
            if vector is not None:
                self.reuse[sha256_hex(text)] = [float(v) for v in vector]


def locate(source: str, text: str, hint: int | None) -> tuple[int | None, str]:
    """Anchor `text` inside `source`.

    Returns (start, method). `indexed`: the splitter's own offset is exact. `search`: the
    text occurs exactly once. Otherwise unanchored (`ambiguous`, `rewritten`).
    """
    if not text:
        return None, "empty"
    if hint is not None and 0 <= hint and source[hint : hint + len(text)] == text:
        return hint, "indexed"
    first = source.find(text)
    if first < 0:
        return None, "rewritten"
    if source.find(text, first + 1) >= 0:
        return None, "ambiguous"
    return first, "search"


def item(
    *,
    ref: str,
    rank: int,
    text: str,
    metadata: dict[str, Any],
    source_text: str | None,
    start_hint: int | None,
    score: float | None,
    score_kind: str,
    detail: dict[str, Any] | None = None,
    span_override: tuple[int, int] | None = None,
    constituents: list[tuple[int, int, str]] | None = None,
) -> dict[str, Any]:
    """Build a LabItem dictionary; native-chunk metadata carries its own exact span.

    A composite (e.g. a LlamaIndex sentence window, whose sentences are re-joined with
    separators) reports its constituent source spans with the hash of each member's text.
    The parent verifies every constituent against the source and the delivered text and
    credits only those; the bounding span is a hint and is never trusted on its own.
    """
    chunk_id = metadata.get("chunk_id")
    extra = dict(detail or {})
    start: int | None
    end: int | None
    if span_override is not None:
        start, end = span_override
        method = "composite"
        if source_text is None or source_text[start:end] != text:
            extra["composite_text_differs"] = True
        extra["constituents"] = [
            {"start_offset": s, "end_offset": e, "text_sha256": sha256_hex(member)}
            for s, e, member in constituents or []
        ]
    elif source_text is None:
        # Pass-through BuildLens chunk: the authoritative span travels in its metadata.
        start, end = metadata.get("start_offset"), metadata.get("end_offset")
        method = "native" if chunk_id and start is not None else "no_source"
        if method == "no_source":
            start = end = None
    else:
        start, method = locate(source_text, text, start_hint)
        end = start + len(text) if start is not None else None
    extra["anchor_method"] = method
    anchored = start is not None and end is not None
    return {
        "ref": ref,
        "rank": rank,
        "text_sha256": sha256_hex(text),
        "text_bytes": len(text.encode()),
        "locator": metadata.get("locator"),
        "artifact_fingerprint": metadata.get("artifact_fingerprint"),
        "span": {"start_offset": start, "end_offset": end} if anchored else None,
        "anchored": anchored,
        "chunk_id": chunk_id,
        "score": None if score is None else float(score),
        "score_kind": score_kind,
        "detail": extra,
    }


_EVIDENCE = re.compile(r"\[(E\d{1,3})\]")


def offline_answer(prompt: str, max_statements: int = 3) -> dict[str, Any]:
    """Deterministic extractive stand-in for a model. Mechanics only; never a quality claim."""
    ids: list[str] = []
    for match in _EVIDENCE.findall(prompt):
        if match not in ids:
            ids.append(match)
    if not ids:
        return {"status": "insufficient_evidence", "statements": []}
    return {
        "status": "answered",
        "statements": [
            {
                "text": f"Evidence {key} was retrieved for this question (offline model).",
                "support": "inference",
                "evidence_ids": [key],
            }
            for key in ids[:max_statements]
        ],
    }


def evidence_block(texts: list[tuple[str, str, dict[str, Any]]]) -> str:
    """Render evidence the same way for both frameworks: [E#] locator then content."""
    parts = []
    for key, text, metadata in texts:
        parts.append(f"[{key}] {metadata.get('locator') or metadata.get('ref') or ''}\n{text}")
    return "\n\n".join(parts)


CITED_ANSWER_SCHEMA: dict[str, Any] = {
    "title": "CitedAnswer",
    "description": "A grounded answer whose statements cite evidence identifiers.",
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answered", "insufficient_evidence"]},
        "statements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "support": {"type": "string", "enum": ["direct", "inference"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "support", "evidence_ids"],
            },
        },
    },
    "required": ["status", "statements"],
}


def validate_citations(answer: dict[str, Any], evidence_ids: list[str]) -> dict[str, Any]:
    """BuildLens-side validation, independent of the framework's own parsing."""
    problems = []
    if answer.get("status") not in {"answered", "insufficient_evidence"}:
        problems.append("status")
    for statement in answer.get("statements") or []:
        for key in statement.get("evidence_ids") or []:
            if key not in evidence_ids:
                problems.append(f"unknown_citation:{key}")
    if answer.get("status") == "answered" and not answer.get("statements"):
        problems.append("answered_without_statements")
    return {"valid": not problems, "problems": problems[:20]}


def bounded_json(value: Any, limit: int = 4000) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"

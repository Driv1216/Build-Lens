"""Deterministic per-claim validation of semantic generation output (§4.1, checks 1–9).

A claim that passes every check is ``cited``: it quotes real evidence and makes no assertion
that contradicts a parsed fact. That is not a claim of semantic truth. Rejected claims are
dropped and only their reason codes are kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from buildlens.application.semantic_prompt import Package, PackageChunk, PackageFact
from buildlens.domain.semantic_generation import (
    ASSERTION_KINDS,
    CLAIM_CATEGORIES,
    AssertionOutcome,
    Citation,
    Claim,
    InterpretationStatus,
)
from buildlens.domain.source import ContentRejectedError, screen_content

MAX_OUTPUT_BYTES = 64_000
CLAIM_TEXT_BYTES = 400
UNKNOWN_TEXT_BYTES = 200
QUOTE_MIN, QUOTE_MAX = 8, 200
_BACKTICK = re.compile(r"`([^`]{1,200})`")
_TOKEN = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_WORD = re.compile(r"[A-Za-z_][\w]*")
_VERDICT = re.compile(
    r"\b(secure|insecure|correct|correctly|incorrect|bug-free|guaranteed?|guarantees|"
    r"thread-safe|threadsafe|safe|safely|unsafe|robust|optimal|optimally|efficient|"
    r"efficiently|best practices?|production-ready|flawless|perfect|perfectly|reliable|"
    r"reliably|vulnerable|well-designed)\b",
    re.IGNORECASE,
)
_QUANTIFIER = re.compile(
    r"\b(always|never|all|every|none|any|only|entirely|everything|nothing)\b", re.IGNORECASE
)
_BIDI = re.compile("[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_BOOLEAN_ASSERTIONS = {"is_async", "awaits", "returns_value", "defines_route", "maps_table"}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    status: InterpretationStatus
    claims: tuple[Claim, ...]
    unknowns: tuple[str, ...]
    rejection_codes: tuple[str, ...]


class _Rejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def identifiers(text: str) -> set[str]:
    """Identifier-like spans a claim makes: back-ticked spans plus tokens with ``_``, ``.``
    or an interior capital (check 4)."""
    values: set[str] = set()
    for span in _BACKTICK.findall(text):
        cleaned = span.strip().removesuffix("()").strip()
        if cleaned:
            values.add(cleaned)
    plain = _BACKTICK.sub(" ", text)
    for token in _TOKEN.findall(plain):
        segments = token.split(".")
        if all(len(segment) <= 1 for segment in segments):
            continue  # e.g. "e.g", "i.e"
        if "_" in token or "." in token or re.search(r"[a-z0-9][A-Z]", token):
            values.add(token.rstrip("."))
    return values


def _grounded(name: str, texts: list[str]) -> bool:
    return any(name in text for text in texts)


def _classify(package: Package, item: PackageChunk, start: int, end: int) -> str:
    if not package.ranges_available:
        return "unclassified"
    absolute = item.chunk.start_offset + start
    text = item.text[start:end]
    documentation = package.ranges.get("documentation", ())
    strings = package.ranges.get("string", ())

    def covered(offset: int, ranges: tuple[tuple[int, int], ...]) -> bool:
        return any(left <= offset < right for left, right in ranges)

    in_doc = in_string = False
    for index, character in enumerate(text):
        if character.isspace():
            continue
        offset = absolute + index
        if covered(offset, documentation):
            in_doc = True
        elif covered(offset, strings):
            in_string = True
        else:
            return "code"
    return "documentation" if in_doc else "string" if in_string else "code"


def static_claim_problem(text: str) -> str | None:
    """Re-validation of a stored claim's text: the checks that need no package."""
    if not text.strip() or len(text.encode("utf-8")) > CLAIM_TEXT_BYTES:
        return "CLAIM_STRUCTURE"
    if _BIDI.search(text):
        return "BIDI_CONTROL"
    if _VERDICT.search(_BACKTICK.sub(" ", text)):
        return "LEXICON_VERDICT"
    try:
        screen_content(text)
    except ContentRejectedError:
        return "SECRET_SCREENED"
    return None


def _assertion_outcome(
    package: Package, assertion: dict[str, Any] | None
) -> AssertionOutcome | None:
    if assertion is None:
        return None
    kind, target = assertion["kind"], assertion.get("target")
    value = getattr(package.view, kind)
    if value is None:
        return "not_checkable"
    if kind in _BOOLEAN_ASSERTIONS:
        return "matches_fact" if value else "conflicts_with_fact"
    complete = package.view.references_complete or kind == "inherits"
    names: set[str] = set(value)
    if target is None:
        if names:
            return "matches_fact"
        return "conflicts_with_fact" if complete else "not_checkable"
    wanted = str(target).strip().strip("`").removesuffix("()")
    candidates = {wanted, wanted.split(".")[-1]}
    if candidates & names:
        return "matches_fact"
    return "conflicts_with_fact" if complete else "not_checkable"


def _claim(package: Package, ordinal: int, raw: Any) -> Claim:
    # Check 1: structure and size.
    if (
        not isinstance(raw, dict)
        or not {"category", "text", "evidence"} <= set(raw)
        or set(raw) - {"category", "text", "evidence", "assertion"}
    ):
        raise _Rejected("CLAIM_STRUCTURE")
    category, text, evidence = raw["category"], raw["text"], raw["evidence"]
    assertion = raw.get("assertion")
    if (
        category not in CLAIM_CATEGORIES
        or not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > CLAIM_TEXT_BYTES
        or not isinstance(evidence, list)
        or not 1 <= len(evidence) <= 4
    ):
        raise _Rejected("CLAIM_STRUCTURE")
    if assertion is not None and (
        not isinstance(assertion, dict)
        or set(assertion) - {"kind", "target"}
        or assertion.get("kind") not in ASSERTION_KINDS
        or not isinstance(assertion.get("target"), (str, type(None)))
    ):
        raise _Rejected("CLAIM_STRUCTURE")
    # Check 9: screening (before anything is stored); bidi controls could disguise text.
    if _BIDI.search(text):
        raise _Rejected("BIDI_CONTROL")
    try:
        screen_content(text)
    except ContentRejectedError as error:
        raise _Rejected("SECRET_SCREENED") from error
    # Check 8: lexicon.
    prose = _BACKTICK.sub(" ", text)
    if _VERDICT.search(prose):
        raise _Rejected("LEXICON_VERDICT")
    chunks = {item.id: item for item in package.chunks}
    facts = {item.id: item for item in package.facts}
    citations: list[Citation] = []
    cited_texts: list[str] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"id", "quote"}:
            raise _Rejected("CLAIM_STRUCTURE")
        evidence_id, quote = item["id"], item["quote"]
        if not isinstance(evidence_id, str) or not isinstance(quote, str):
            raise _Rejected("CLAIM_STRUCTURE")
        # Check 2: evidence ids must be in the supplied set.
        if evidence_id not in package.evidence_ids or evidence_id in seen:
            raise _Rejected("EVIDENCE_UNKNOWN")
        seen.add(evidence_id)
        # Check 3: exact, unique quote.
        if not QUOTE_MIN <= len(quote) <= QUOTE_MAX:
            raise _Rejected("QUOTE_LENGTH")
        source: PackageChunk | PackageFact = chunks.get(evidence_id) or facts[evidence_id]
        haystack = source.text
        count = haystack.count(quote)
        if count == 0:
            raise _Rejected("QUOTE_NOT_FOUND")
        if count > 1:
            raise _Rejected("QUOTE_AMBIGUOUS")
        start = haystack.index(quote)
        end = start + len(quote)
        digest = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        cited_texts.append(haystack)
        if isinstance(source, PackageChunk):
            basis = _classify(package, source, start, end)
            first = source.chunk.start_line + haystack[:start].count("\n")
            citations.append(
                Citation(
                    evidence_id,
                    "chunk",
                    digest,
                    start,
                    end,
                    basis,  # type: ignore[arg-type]
                    stable_key=source.chunk.stable_key,
                    content_hash=source.chunk.content_hash,
                    locator=source.locator,
                    start_line=first,
                    end_line=first + quote.count("\n"),
                )
            )
        else:
            citations.append(
                Citation(
                    evidence_id,
                    "fact",
                    digest,
                    start,
                    end,
                    "facts",
                    fact_digest=source.fact.fact_digest,
                    locator=str(source.fact.source_refs[0].get("locator")),
                )
            )
    # Check 4: identifier grounding.
    grounding = cited_texts + list(package.own_fact_texts)
    names = identifiers(text)
    if any(not _grounded(name, grounding) for name in names):
        raise _Rejected("UNGROUNDED_IDENTIFIER")
    # Check 5: basis.
    kinds = {citation.basis for citation in citations}
    has_code = "code" in kinds
    if category in {"behavior", "side_effect"} and not has_code:
        raise _Rejected("BEHAVIOR_WITHOUT_CODE_EVIDENCE")
    basis = "code" if has_code else "facts" if kinds == {"facts"} else "documentation"
    label = (
        "cited"
        if basis == "code"
        else "from_declarations_only"
        if basis == "facts"
        else "per_documentation"
    )
    # Check 7: assertion versus parsed facts.
    outcome = _assertion_outcome(package, assertion)
    # Check 6: omitted implementation (behaviour about code that was not sent).
    if category in {"behavior", "side_effect"} and package.foreign_names:
        mentioned = set(names) | {word for word in _WORD.findall(prose) if len(word) >= 4}
        mentioned |= {name.split(".")[-1] for name in names}
        if mentioned & package.foreign_names and not (
            assertion is not None and assertion["kind"] == "calls" and outcome == "matches_fact"
        ):
            raise _Rejected("OMITTED_IMPLEMENTATION_REFERENCED")
    return Claim(
        ordinal,
        category,
        text.strip(),
        "conflicts_with_fact" if outcome == "conflicts_with_fact" else "cited",
        basis,  # type: ignore[arg-type]
        label,  # type: ignore[arg-type]
        dict(assertion) if assertion is not None else None,
        outcome,
        bool(_QUANTIFIER.search(prose)),
        tuple(citations),
    )


def _unknown(package: Package, raw: Any) -> str:
    if not isinstance(raw, dict) or set(raw) != {"text"} or not isinstance(raw["text"], str):
        raise _Rejected("UNKNOWN_STRUCTURE")
    text = raw["text"].strip()
    if not text or len(text.encode("utf-8")) > UNKNOWN_TEXT_BYTES:
        raise _Rejected("UNKNOWN_STRUCTURE")
    if re.search(r"[.!?]\s+\S", text):
        raise _Rejected("UNKNOWN_NOT_ONE_SENTENCE")
    if _BIDI.search(text):
        raise _Rejected("BIDI_CONTROL")
    try:
        screen_content(text)
    except ContentRejectedError as error:
        raise _Rejected("SECRET_SCREENED") from error
    if _VERDICT.search(_BACKTICK.sub(" ", text)):
        raise _Rejected("LEXICON_VERDICT")
    grounding = [item.text for item in package.facts] + [item.text for item in package.chunks]
    if any(not _grounded(name, grounding) for name in identifiers(text)):
        raise _Rejected("UNGROUNDED_IDENTIFIER")
    return text


def validate(raw: str, package: Package) -> ValidationResult:
    """Validate one model output against the exact package that was sent."""
    if not raw or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return ValidationResult("rejected", (), (), ("OUTPUT_INVALID",))
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError):
        return ValidationResult("rejected", (), (), ("OUTPUT_INVALID",))
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"claims", "unknowns"}
        or not isinstance(parsed["claims"], list)
        or not isinstance(parsed["unknowns"], list)
        or len(parsed["claims"]) > 12
        or len(parsed["unknowns"]) > 6
    ):
        return ValidationResult("rejected", (), (), ("OUTPUT_INVALID",))
    claims: list[Claim] = []
    codes: list[str] = []
    for index, item in enumerate(parsed["claims"]):
        try:
            claims.append(_claim(package, len(claims), item))
        except _Rejected as rejected:
            codes.append(f"claim{index}:{rejected.code}")
    unknowns: list[str] = []
    for index, item in enumerate(parsed["unknowns"]):
        try:
            unknowns.append(_unknown(package, item))
        except _Rejected as rejected:
            codes.append(f"unknown{index}:{rejected.code}")
    if not claims:
        status: InterpretationStatus = (
            "insufficient_evidence" if not parsed["claims"] else "rejected"
        )
    elif any(claim.status == "conflicts_with_fact" for claim in claims):
        status = "accepted_with_conflicts"
    else:
        status = "accepted"
    if status == "rejected":
        return ValidationResult("rejected", (), (), tuple(codes))
    return ValidationResult(status, tuple(claims), tuple(unknowns), tuple(codes))

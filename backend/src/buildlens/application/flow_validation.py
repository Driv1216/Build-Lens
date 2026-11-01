"""Stage 15 Milestone E: ``flow-validator.v1``, deterministic per-claim validation of a model's
explanation of one ``FlowPackage``.

D5 outcome: the Stage 13 validator is **not** shared. Its generic checks are interleaved with
its own package type, so sharing them would mean refactoring Stage 13 and then proving
behavioural parity. This validator stands alone and reuses only Stage 13's unchanged pure
helpers (``identifiers``, ``static_claim_problem``). Generic checks: structure, evidence ids,
exact unique quotes, identifier grounding, verdict lexicon, bidi and secret screening.
Flow-specific checks:

- ``EDGE_NOT_IN_TRACE``: a cited step is not in the package, or the claim names a hop that no
  cited step connects (an invented edge).
- ``LEVEL_OVERSTATED``: execution wording ("executes", "runs", "is called") over a structural
  trace.
- ``TIER_OVERSTATED``: certainty wording, or "calls", over an inferred or unknown step.
- ``ORDER_NOT_IN_TRACE``: order wording across lanes whose order is not determined.
- ``BEHAVIOR_IN_GAP``: behaviour asserted for an unresolved step without saying it is unknown.
- ``COMMIT_OVERSTATED``: "saved"/"persisted", or a commit not phrased as "reaches a commit" on
  a cited path that reaches one.
- ``RENDER_OVERSTATED``: "re-renders" or "the effect runs" after a state update.
- ``STATUS_OVERSTATED``: a status code that no cited outcome states.

A passing claim is consistent with the trace; it is not a claim of runtime truth.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from buildlens.application.semantic_validation import identifiers, static_claim_problem
from buildlens.domain.flow_explanation import CLAIM_LEVELS, ExplanationClaim

MAX_OUTPUT_BYTES = 64_000
CLAIM_TEXT_BYTES = 400
UNKNOWN_TEXT_BYTES = 200
QUOTE_MIN, QUOTE_MAX = 8, 200

_EXECUTION = re.compile(
    r"\b(executes|executed|executing|execute|runs|ran|running|is called|are called|"
    r"gets called|get called|is invoked at runtime|will call|will run|will execute)\b",
    re.IGNORECASE,
)
_CERTAINTY = re.compile(
    r"\b(definitely|certainly|confirmed|guaranteed|for sure|always|proven|undoubtedly)\b",
    re.IGNORECASE,
)
_CALLS = re.compile(r"\b(calls|called)\b", re.IGNORECASE)
_ORDER = re.compile(
    r"\b(before|after|then|first|finally|followed by|precedes|preceded|next|later|earlier)\b",
    re.IGNORECASE,
)
_UNKNOWN_WORDS = re.compile(
    r"\b(unknown|unresolved|not determined|cannot be determined|not known|undetermined)\b",
    re.IGNORECASE,
)
_SAVED = re.compile(
    r"\b(saved|saves|save|persists|persisted|persist|stored|stores|written to|writes to)\b",
    re.IGNORECASE,
)
_COMMIT = re.compile(r"\b(commits|committed|commit)\b", re.IGNORECASE)
_REACHES_COMMIT = re.compile(r"\breach(?:es|ed)? a commit\b", re.IGNORECASE)
_RENDER = re.compile(
    r"\b(re-?renders?|re-?rendered|re-?rendering|will render|renders again|"
    r"effect (?:runs|will run|re-?runs|fires)|triggers? the effect|runs the effect)\b",
    re.IGNORECASE,
)
_STATUS = re.compile(r"\b([1-5]\d\d)\b")
_GENERIC_LABELS = {"arrow", "module", "constructor", "setstate"}


@dataclass(frozen=True, slots=True)
class FlowValidation:
    status: str
    claims: tuple[ExplanationClaim, ...]
    unknowns: tuple[str, ...]
    rejection_codes: tuple[str, ...]


class _Rejected(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _Package:
    """Lookups over one package (built once per validation)."""

    def __init__(self, package: dict[str, Any]) -> None:
        self.raw = package
        self.steps = {s["id"]: s for s in package["steps"]}
        self.hops = {h["id"]: h for h in package["hops"]}
        self.excerpts = {e["id"]: e for e in package["excerpts"]}
        self.lane_order = {lane["id"]: lane["order"] for lane in package["lanes"]}
        self.gap_steps = {g.get("step") for g in package["gaps"]}
        self.effects = {e["step"]: e for e in package["effects"]}
        texts = [str(h.get("label") or "") for h in package["hops"]]
        texts += [e["text"] for e in package["excerpts"]]
        texts += [c for s in package["steps"] for c in s["conditions"]]
        texts += [s["relation"] for s in package["steps"]] + list(self.lane_order)
        texts += [str(t) for e in package["effects"] for t in e.get("tables", [])]
        texts += [str(h.get("locator") or "") for h in package["hops"]]
        self.grounding = texts
        self.labels = {
            hop_id: str(h["label"])
            for hop_id, h in self.hops.items()
            if h.get("label")
            and re.fullmatch(r"[A-Za-z_][\w.]{2,}", str(h["label"]))
            and str(h["label"]).lower() not in _GENERIC_LABELS
        }


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _claim(p: _Package, ordinal: int, raw: Any) -> ExplanationClaim:
    # Generic: structure and size.
    if not isinstance(raw, dict) or set(raw) != {"level", "text", "steps", "evidence"}:
        raise _Rejected("CLAIM_STRUCTURE")
    level, text, steps, evidence = raw["level"], raw["text"], raw["steps"], raw["evidence"]
    if (
        level not in CLAIM_LEVELS
        or not isinstance(text, str)
        or not text.strip()
        or len(text.encode("utf-8")) > CLAIM_TEXT_BYTES
        or not isinstance(steps, list)
        or not 1 <= len(steps) <= 6
        or not all(isinstance(s, str) for s in steps)
        or len(set(steps)) != len(steps)
        or not isinstance(evidence, list)
        or len(evidence) > 3
    ):
        raise _Rejected("CLAIM_STRUCTURE")
    # Generic: bidi, secrets, verdict lexicon (Stage 13 helper, unchanged).
    problem = static_claim_problem(text)
    if problem:
        raise _Rejected(problem)
    # Flow: every cited step exists.
    if any(step not in p.steps for step in steps):
        raise _Rejected("EDGE_NOT_IN_TRACE")
    cited = [p.steps[step] for step in steps]
    endpoints = {s["from"] for s in cited} | {s["to"] for s in cited if s["to"]}
    # Flow: a hop named in the text must be connected by a cited step.
    for hop_id, label in p.labels.items():
        if re.search(rf"(?<![\w.]){re.escape(label)}(?![\w])", text) and hop_id not in endpoints:
            if not any(h == label for h in (p.labels.get(e) for e in endpoints)):
                raise _Rejected("EDGE_NOT_IN_TRACE")
    # Generic: evidence ids and exact, unique quotes.
    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in evidence:
        if not isinstance(item, dict) or set(item) != {"id", "quote"}:
            raise _Rejected("CLAIM_STRUCTURE")
        evidence_id, quote = item["id"], item["quote"]
        if not isinstance(evidence_id, str) or not isinstance(quote, str):
            raise _Rejected("CLAIM_STRUCTURE")
        if evidence_id not in p.excerpts or evidence_id in seen:
            raise _Rejected("EVIDENCE_UNKNOWN")
        seen.add(evidence_id)
        if not QUOTE_MIN <= len(quote) <= QUOTE_MAX:
            raise _Rejected("QUOTE_LENGTH")
        haystack = p.excerpts[evidence_id]["text"]
        count = haystack.count(quote)
        if count == 0:
            raise _Rejected("QUOTE_NOT_FOUND")
        if count > 1:
            raise _Rejected("QUOTE_AMBIGUOUS")
        citations.append(
            {
                "id": evidence_id,
                "quote_digest": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                "start": haystack.index(quote),
                "end": haystack.index(quote) + len(quote),
            }
        )
    # Generic: identifier grounding.
    if any(not any(name in t for t in p.grounding) for name in identifiers(text)):
        raise _Rejected("UNGROUNDED_IDENTIFIER")
    tiers = {s["tier"] for s in cited}
    # Flow: level, tier, order, gaps, commits, renders, statuses.
    if _EXECUTION.search(text):
        raise _Rejected("LEVEL_OVERSTATED")
    if tiers & {"inferred", "unknown"} and (_CERTAINTY.search(text) or _CALLS.search(text)):
        raise _Rejected("TIER_OVERSTATED")
    if _ORDER.search(text):
        lanes = {s["lane"] for s in cited if s["relation"] != "response_boundary"}
        if len(lanes) > 1 and any(
            p.lane_order.get(lane, "ORDER_NOT_DETERMINED") == "ORDER_NOT_DETERMINED"
            for lane in lanes
        ):
            raise _Rejected("ORDER_NOT_IN_TRACE")
    if any(s["relation"] == "unresolved" or s["id"] in p.gap_steps for s in cited):
        if not _UNKNOWN_WORDS.search(text):
            raise _Rejected("BEHAVIOR_IN_GAP")
    if _SAVED.search(text):
        raise _Rejected("COMMIT_OVERSTATED")
    if _COMMIT.search(text):
        reached = any(
            str(p.effects.get(s["id"], {}).get("state", "")).startswith("commit_reached")
            for s in cited
        )
        if not (_REACHES_COMMIT.search(text) and reached):
            raise _Rejected("COMMIT_OVERSTATED")
    if _RENDER.search(text):
        raise _Rejected("RENDER_OVERSTATED")
    stated = {
        str(s["attributes"].get("status"))
        for s in cited
        if s["attributes"].get("status") is not None
    }
    stated |= {
        str(p.hops[s["to"]]["label"]).split(" ", 1)[0]
        for s in cited
        if s["to"] in p.hops and p.hops[s["to"]].get("kind") == "response_outcome"
    }
    if any(code not in stated for code in _STATUS.findall(text)):
        raise _Rejected("STATUS_OVERSTATED")
    body = {"level": level, "text": text.strip(), "steps": list(steps), "citations": citations}
    return ExplanationClaim(
        ordinal,
        level,
        text.strip(),
        tuple(steps),
        tuple(citations),
        hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest(),
    )


def _unknown(p: _Package, raw: Any) -> str:
    if not isinstance(raw, dict) or set(raw) != {"text"} or not isinstance(raw["text"], str):
        raise _Rejected("UNKNOWN_STRUCTURE")
    text = raw["text"].strip()
    if not text or len(text.encode("utf-8")) > UNKNOWN_TEXT_BYTES:
        raise _Rejected("UNKNOWN_STRUCTURE")
    if re.search(r"[.!?]\s+\S", text):
        raise _Rejected("UNKNOWN_NOT_ONE_SENTENCE")
    problem = static_claim_problem(text)
    if problem:
        raise _Rejected(problem)
    if any(not any(name in t for t in p.grounding) for name in identifiers(text)):
        raise _Rejected("UNGROUNDED_IDENTIFIER")
    return text


def validate(raw: str, package: dict[str, Any]) -> FlowValidation:
    """Validate one model output against the exact package that was sent."""
    if not raw or len(raw.encode("utf-8")) > MAX_OUTPUT_BYTES:
        return FlowValidation("rejected", (), (), ("OUTPUT_INVALID",))
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, TypeError):
        return FlowValidation("rejected", (), (), ("OUTPUT_INVALID",))
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"claims", "unknowns"}
        or not isinstance(parsed["claims"], list)
        or not isinstance(parsed["unknowns"], list)
        or len(parsed["claims"]) > 12
        or len(parsed["unknowns"]) > 6
    ):
        return FlowValidation("rejected", (), (), ("OUTPUT_INVALID",))
    p = _Package(package)
    claims: list[ExplanationClaim] = []
    codes: list[str] = []
    for index, item in enumerate(parsed["claims"]):
        try:
            claims.append(_claim(p, len(claims), item))
        except _Rejected as rejected:
            codes.append(f"claim{index}:{rejected.code}")
    unknowns: list[str] = []
    for index, item in enumerate(parsed["unknowns"]):
        try:
            unknowns.append(_unknown(p, item))
        except _Rejected as rejected:
            codes.append(f"unknown{index}:{rejected.code}")
    if not claims:
        status = "insufficient_evidence" if not parsed["claims"] else "rejected"
        return FlowValidation(status, (), tuple(unknowns), tuple(codes))
    status = "accepted_with_rejections" if codes else "accepted"
    return FlowValidation(status, tuple(claims), tuple(unknowns), tuple(codes))

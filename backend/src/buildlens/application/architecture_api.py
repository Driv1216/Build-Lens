"""Stage 14 Milestone B: backend endpoints, client request sites and conservative API matching.

A pure projection over sanitised literal rows, catalog route facts and the module import
bindings the architecture builder already resolved. Nothing is executed or evaluated.

Contracts (recorded in ``architecture_contract()``):

* ``router-binding.v1``: a route decorator's receiver binds to an ``app_or_router`` row of the
  same module and scope; mounts bind their parent and child objects in the same module or
  through one resolved import; full paths follow Starlette/FastAPI and Flask joining rules;
  every non-literal, unbound or invalid component makes the path ``unknown``.
* ``http-sinks.v1``: request sites are only ``fetch``/``window.fetch``/``globalThis.fetch``
  (bare ``fetch`` only when the file declares or imports no ``fetch``) and ``axios`` bound by
  an import from the bare specifier ``axios``. URL-shaped arguments elsewhere are never
  request evidence.
* ``client-wrapper.v1``: a top-level function with exactly one unconditional sink whose URL is
  ``${B}${p}`` (``p`` a parameter) qualifies as a wrapper; callers bind in the same file or
  through one resolved import.
* ``client-folding.v1``: module constants (``X ?? 'literal'`` yields two branches) and
  single-return URL helpers, same file or one import hop, depth <= 2.
* ``api-matching.v3``: destination classes first; base path preserved; a candidate requires a
  non-empty intersection of the two path patterns and a known method; there is no
  unconditional "matched" state. Typed converters (int, float, uuid) accept only their own
  characters (v2 treated them as any segment, so ``/items/abc`` matched ``{id:int}``).
"""

from __future__ import annotations

import ipaddress
import posixpath
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from buildlens.domain.architecture import (
    API_MATCHING_VERSION,
    ArchitectureEdge,
    ArchitectureNode,
    EvidenceLink,
    Row,
)
from buildlens.domain.architecture_literals import LiteralRejected, Part, url_template
from buildlens.domain.source import digest

MOUNT_DEPTH = 4
IMPORT_HOPS = 2
FOLD_DEPTH = 2
FETCH_NORMALISED = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "POST", "PUT"})
_FASTAPI_PARAM = re.compile(r"\{([A-Za-z_][\w]*)(?::([A-Za-z_]+))?\}")
_FLASK_PARAM = re.compile(r"<(?:([A-Za-z_]+):)?([A-Za-z_][\w]*)>")
_CALL = re.compile(r"^([A-Za-z_$][\w$]*)\((.*)\)$")

Evidence = Callable[[Any, Sequence[Row], Row, str], EvidenceLink]
DiagnosticDraft = tuple[str, str, str, tuple[str, ...], Row, tuple[EvidenceLink, ...]]


# --------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class EndpointInput:
    """One catalog ``api_endpoint`` entity with its recognizer attributes."""

    entity_key: str
    portable_key: str
    locator: str
    owner: str | None
    function: str
    recognizer: str
    strength: str
    method: str | None
    path: str | None
    receiver: str | None
    fact_digest: str


@dataclass(frozen=True, slots=True)
class Binding:
    """A local name bound by one resolved import row: a module object or an exported name."""

    target: str
    name: str | None
    specifier: str
    ref: Row


@dataclass(slots=True)
class ApiModule:
    locator: str
    language: str | None
    usable: bool
    literals: tuple[Row, ...]
    contract: str | None
    bindings: dict[str, Binding] = field(default_factory=dict)
    top_level_names: frozenset[str] = frozenset()


def literal_row_digest(row: Row) -> str:
    """Semantic fields of an ``architecture_literals`` row and its owning pass, without ids."""
    return digest(
        {
            "extractor": row.get("extractor"),
            "contract": row.get("contract_digest"),
            "ordinal": row.get("ordinal"),
            "kind": row.get("kind"),
            "subject": row.get("subject"),
            "attributes": row.get("attributes"),
            "start_line": row.get("start_line"),
            "end_line": row.get("end_line"),
            "start_column": row.get("start_column"),
            "end_column": row.get("end_column"),
        }
    )


def extraction_row_digest(row: Row) -> str:
    facts = row.get("artifact_facts") or {}
    return digest(
        {
            "extractor": row.get("extractor"),
            "contract": row.get("contract_digest"),
            "outcome": row.get("outcome"),
            "classification_complete": facts.get("classification_complete"),
        }
    )


def literal_ref(locator: str, row: Row) -> Row:
    return {
        "table": "architecture_literals",
        "id": row["id"],
        "contract": row.get("contract_digest"),
        "locator": locator,
        "row_digest": literal_row_digest(row),
    }


# --------------------------------------------------------------------------- path rules


def fastapi_prefix(value: str) -> bool:
    return value == "" or (value.startswith("/") and not value.endswith("/"))


def flask_join(prefix: str, rule: str) -> str:
    if not prefix:
        return rule
    return "/".join((prefix.rstrip("/"), rule.lstrip("/"))) if rule else prefix


# Pattern elements: ("c", char) | ("one", cls) | ("star", cls). ``cls`` is True (any
# character), False (any but "/") or a typed converter's class ("int", "float", "uuid"); the
# classes over-approximate Starlette's converter expressions and never accept "/".
Element = tuple[str, Any]
_CLASS_CHARS = {
    "int": frozenset("0123456789"),
    "float": frozenset("0123456789."),
    "uuid": frozenset("0123456789abcdefABCDEF-"),
}


def _accepts(cls: Any, char: str) -> bool:
    if cls is True:
        return True
    if cls is False:
        return char != "/"
    return char in _CLASS_CHARS[cls]


def _literal(text: str) -> list[Element]:
    return [("c", char) for char in text]


def backend_pattern(path: str, framework: str) -> tuple[list[Element], set[str]] | None:
    """Compile a full backend path; ``None`` when a parameter syntax is not understood."""
    elements: list[Element] = []
    kinds: set[str] = set()
    position = 0
    expression = _FASTAPI_PARAM if framework == "fastapi" else _FLASK_PARAM
    for match in expression.finditer(path):
        elements.extend(_literal(path[position : match.start()]))
        converter = (match.group(2) if framework == "fastapi" else match.group(1)) or "str"
        if converter in {"str", "string"}:
            elements += [("one", False), ("star", False)]
            kinds.add("segment")
        elif converter == "path":
            # Starlette ``{p:path}`` matches ``.*`` (may be empty); Flask ``<path:p>`` is ``.+``.
            if framework == "flask":
                elements.append(("one", True))
            elements.append(("star", True))
            kinds.add("multi_segment")
        elif converter in {"int", "float", "uuid"}:
            elements += [("one", converter), ("star", converter)]
            kinds.add("typed")
        else:
            return None
        position = match.end()
    remainder = path[position:]
    if "{" in remainder or "<" in remainder:
        return None
    elements.extend(_literal(remainder))
    return elements, kinds


def _bounded(parts: Sequence[Part], index: int) -> bool:
    """Whether placeholder ``index`` fills whole segments (``/`` or the end on both sides)."""
    before = parts[index - 1] if index > 0 else ("s", "/")
    after = parts[index + 1] if index + 1 < len(parts) else ("s", "/")
    return (
        before[0] == "s"
        and before[1].endswith("/")
        and after[0] == "s"
        and after[1].startswith("/")
    )


def client_pattern(parts: Sequence[Part], strict: bool = False) -> list[Element]:
    """Client path pattern. A placeholder may contain ``/`` and may be empty; under
    ``strict`` a segment-bounded placeholder is one non-empty segment (an assumption that
    is recorded as ``PLACEHOLDER_WITHIN_SEGMENT_ASSUMED`` whenever it decides a match)."""
    elements: list[Element] = []
    for index, (kind, value) in enumerate(parts):
        if kind == "s":
            elements.extend(_literal(value))
        elif strict and _bounded(parts, index):
            elements += [("one", False), ("star", False)]
        else:
            elements.append(("star", True))
    return elements


def _overlap(a: Element, b: Element) -> bool:
    """Whether the single-character sets of two elements intersect."""
    if a[0] == "c" and b[0] == "c":
        return bool(a[1] == b[1])
    if a[0] == "c":
        return _accepts(b[1], a[1])
    if b[0] == "c":
        return _accepts(a[1], b[1])
    return True  # every pair of classes shares the digits (or any non-"/" character)


def intersects(first: Sequence[Element], second: Sequence[Element]) -> bool:
    """Non-empty intersection of two glob-like patterns (product-automaton reachability)."""
    start = (0, 0)
    seen = {start}
    stack = [start]
    while stack:
        i, j = stack.pop()
        if i == len(first) and j == len(second):
            return True
        moves: list[tuple[int, int]] = []
        if i < len(first) and first[i][0] == "star":
            moves.append((i + 1, j))
        if j < len(second) and second[j][0] == "star":
            moves.append((i, j + 1))
        if i < len(first) and j < len(second):
            a, b = first[i], second[j]
            symbol = ("one", a[1]) if a[0] == "star" else a
            other = ("one", b[1]) if b[0] == "star" else b
            if _overlap(symbol, other):
                moves.append((i if a[0] == "star" else i + 1, j if b[0] == "star" else j + 1))
        for move in moves:
            if move not in seen:
                seen.add(move)
                stack.append(move)
    return False


def segment_aligned(parts: Sequence[Part]) -> bool:
    """Every client placeholder fills whole path segments (bounded by ``/`` or the end)."""
    return all(_bounded(parts, index) for index, (kind, _) in enumerate(parts) if kind == "e")


def display(parts: Sequence[Part]) -> str:
    return "".join(value if kind == "s" else "{…}" for kind, value in parts)


def host_class(host: str) -> str:
    bare = host.strip("[]").lower()
    if bare in {"localhost"} or bare.endswith(".localhost"):
        return "local_loopback"
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        return "public_host_unbound"
    if address.is_loopback:
        return "local_loopback"
    if address.is_private or address.is_link_local:
        return "private_network_host_unbound"
    return "public_host_unbound"


def normalise_fetch_method(value: str) -> tuple[str, tuple[str, ...]]:
    """Fetch-standard normalisation: only six methods are upper-cased, others stay byte-exact."""
    if value.upper() in FETCH_NORMALISED:
        return value.upper(), ()
    if value != value.upper():
        return value, ("METHOD_NOT_NORMALISED_BY_FETCH",)
    return value, ()


# --------------------------------------------------------------------------- client branches


@dataclass(slots=True)
class Branch:
    parts: list[Part]
    conditions: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    refs: list[Row] = field(default_factory=list)


@dataclass(slots=True)
class _Wrapper:
    module: ApiModule
    name: str
    sink: Row
    base: list[Part]
    path_index: int
    init_index: int | None
    fixed_method: str | None
    method_state: str


@dataclass(slots=True)
class _Site:
    """One request site: a direct sink or a caller of a qualified wrapper."""

    key: str
    module: ApiModule
    row: Row
    sink: str
    url: list[Part] | None
    method: str | None
    method_conditions: tuple[str, ...]
    conditions: set[str]
    refs: list[Row]
    wrapper: str | None = None
    # A wrapper's base folds in the wrapper's module; the caller's URL in the caller's.
    base: list[Part] = field(default_factory=list)
    base_module: ApiModule | None = None


def _template_parts(template: Mapping[str, Any]) -> list[Part]:
    origin = template.get("origin") or {}
    parts: list[Part] = [
        ("s", str(value["s"])) if "s" in value else ("e", str(value["e"]))
        for value in template.get("parts") or []
    ]
    kind = origin.get("kind")
    if kind == "absolute":
        scheme = f"{origin['scheme']}:" if origin.get("scheme") else ""
        port = f":{origin['port']}" if origin.get("port") else ""
        return [("s", f"{scheme}//{origin.get('host') or ''}{port}"), *parts]
    if kind == "absolute_opaque_host":
        scheme = f"{origin['scheme']}:" if origin.get("scheme") else ""
        return [("s", f"{scheme}//"), ("e", "<host>"), ("s", "/"), ("e", "<expr>")]
    return parts


# --------------------------------------------------------------------------- projection


class ApiProjection:
    def __init__(
        self,
        modules: Sequence[ApiModule],
        endpoints: Sequence[EndpointInput],
        evidence: Evidence,
    ) -> None:
        self.modules = {module.locator: module for module in modules}
        self.endpoints = endpoints
        self.evidence = evidence
        self.nodes: dict[str, ArchitectureNode] = {}
        self.edges: list[ArchitectureEdge] = []
        self.diagnostics: list[DiagnosticDraft] = []
        self.counters: Counter[str] = Counter()
        # (locator, owner, name) -> [(router key, row)]
        self.objects: dict[tuple[str, str | None, str], list[tuple[str, Row]]] = defaultdict(list)
        self.routers: dict[str, tuple[ApiModule, Row]] = {}
        # child router -> [(parent router, mount row, binding refs, mount module)]
        self.mounts: dict[str, list[tuple[str, Row, list[Row], ApiModule]]] = defaultdict(list)

    # ----------------------------------------------------------------- helpers

    def _rows(self, module: ApiModule, kind: str) -> list[Row]:
        return [row for row in module.literals if row["kind"] == kind] if module.usable else []

    def _link(
        self, module: ApiModule, rows: Iterable[Row], derived: Row, text: str
    ) -> EvidenceLink:
        refs = [literal_ref(module.locator, row) for row in rows]
        return self.evidence("literal_rows", refs, derived, text)

    def _diagnose(
        self,
        code: str,
        tier: str,
        subjects: Sequence[str],
        attributes: Row,
        links: Iterable[EvidenceLink],
    ) -> None:
        self.diagnostics.append(
            ("api", code, tier, tuple(subjects), attributes, tuple(sorted(set(links), key=_order)))
        )

    def _edge(
        self,
        key: str,
        kind: str,
        source: str,
        target: str | None,
        tier: str,
        attributes: Row,
        conditions: Iterable[str],
        links: Iterable[EvidenceLink],
        text: str | None = None,
    ) -> None:
        self.edges.append(
            ArchitectureEdge(
                0,
                key,
                kind,  # type: ignore[arg-type]
                source,
                target,
                text,
                tier,  # type: ignore[arg-type]
                None,
                None,
                1,
                attributes,
                tuple(sorted(set(conditions))),
                tuple(sorted(set(links), key=_order)),
            )
        )

    def _node(
        self,
        key: str,
        kind: str,
        parent: str | None,
        name: str,
        locator: str | None,
        language: str | None,
        attributes: Row,
        links: Iterable[EvidenceLink],
        entity: str | None = None,
    ) -> None:
        self.nodes[key] = ArchitectureNode(
            key,
            kind,  # type: ignore[arg-type]
            parent,
            name,
            locator,
            language,
            entity,
            None,
            "derived",
            attributes,
            evidence=tuple(sorted(set(links), key=_order)),
        )

    # ----------------------------------------------------------------- backend

    def run(self) -> None:
        self._router_objects()
        self._mount_edges()
        paths = self._endpoints()
        self._duplicates(paths)
        self._clients(paths)

    def _router_objects(self) -> None:
        for module in self.modules.values():
            rows = self._rows(module, "app_or_router")
            names = Counter((row["attributes"].get("owner"), row["subject"]) for row in rows)
            for row in rows:
                attributes = row["attributes"]
                owner = attributes.get("owner")
                qualified = f"{owner}.{row['subject']}" if owner else str(row["subject"])
                key = f"router:{module.locator}#{qualified}"
                if names[(owner, row["subject"])] > 1:
                    key += f"@{row['start_line']}"
                constructor = str(attributes.get("constructor"))
                framework = "fastapi" if constructor in {"FastAPI", "APIRouter"} else "flask"
                link = self._link(
                    module,
                    [row],
                    {"algorithm": "router-binding.v1", "constructor": constructor},
                    f"{module.locator}:{row['start_line']} assigns "
                    f"{qualified} = {constructor}(...)",
                )
                self._node(
                    key, "router", f"module:{module.locator}", qualified, module.locator,
                    module.language,
                    {
                        "constructor": constructor,
                        "framework": framework,
                        "is_app": constructor in {"FastAPI", "Flask"},
                        "prefix": attributes.get("prefix"),
                        "scope": attributes.get("scope"),
                        "line": row["start_line"],
                    },
                    [link],
                )  # fmt: skip
                self._edge(
                    f"defines_router|module:{module.locator}|{key}", "defines_router",
                    f"module:{module.locator}", key, "observed", {"line": row["start_line"]},
                    (), [link],
                )  # fmt: skip
                self.objects[(module.locator, owner, str(row["subject"]))].append((key, row))
                self.routers[key] = (module, row)

    def _resolve(
        self, module: ApiModule, owner: str | None, chain: str | None
    ) -> tuple[str | None, str, list[Row]]:
        """Router key for an object reference, with the import rows that bound it."""
        if not chain:
            return None, "OBJECT_NOT_A_NAME", []
        parts = chain.split(".")
        if len(parts) == 1:
            for scope in dict.fromkeys((owner, None)):
                found = self.objects.get((module.locator, scope, parts[0]))
                if found:
                    if len(found) > 1:
                        return None, "OBJECT_AMBIGUOUS", []
                    return found[0][0], "bound", []
            refs: list[Row] = []
            current = module
            name = parts[0]
            # One import hop, plus one re-export hop (``from a import router`` where ``a``
            # itself imported ``router``); deeper chains stay unknown.
            for _ in range(IMPORT_HOPS):
                binding = current.bindings.get(name)
                if binding is None or binding.name in {None, "*", "default"}:
                    break
                refs.append(binding.ref)
                found = self.objects.get((binding.target, None, str(binding.name)))
                if found:
                    if len(found) > 1:
                        return None, "OBJECT_AMBIGUOUS", []
                    return found[0][0], "bound", refs
                target = self.modules.get(binding.target)
                if target is None:
                    break
                current, name = target, str(binding.name)
            return None, "OBJECT_UNBOUND", []
        if len(parts) == 2:
            binding = module.bindings.get(parts[0])
            if binding is not None and binding.name in {None, "*"}:
                found = self.objects.get((binding.target, None, parts[1]))
                if found and len(found) == 1:
                    return found[0][0], "bound", [binding.ref]
            return None, "OBJECT_UNBOUND", []
        return None, "OBJECT_CHAIN_UNSUPPORTED", []

    def _mount_edges(self) -> None:
        for module in self.modules.values():
            for row in self._rows(module, "router_mount"):
                attributes = row["attributes"]
                owner = attributes.get("owner")
                parent, parent_state, parent_refs = self._resolve(module, owner, row["subject"])
                child, child_state, child_refs = self._resolve(
                    module, owner, attributes.get("router")
                )
                self.counters["mounts"] += 1
                if parent is None and attributes.get("method") == "mount":
                    self.counters["mount_rows_unrelated"] += 1
                    continue
                link = self._link(
                    module,
                    [row],
                    {"algorithm": "router-binding.v1", "method": attributes.get("method")},
                    f"{module.locator}:{row['start_line']} calls "
                    f"{row['subject']}.{attributes.get('method')}({attributes.get('router')})",
                )
                if parent is None or child is None:
                    self.counters["mounts_unbound"] += 1
                    self._diagnose(
                        "MOUNT_TARGET_UNKNOWN", "unknown", [f"module:{module.locator}"],
                        {"line": row["start_line"], "parent": parent_state, "child": child_state},
                        [link],
                    )  # fmt: skip
                    continue
                refs = [*parent_refs, *child_refs]
                links = [link]
                if refs:
                    links.append(
                        self.evidence(
                            "import_statements",
                            refs,
                            {"algorithm": "router-binding.v1", "binding": "one_import_hop"},
                            f"{module.locator} binds the mounted object through an import",
                        )
                    )
                self.mounts[child].append((parent, row, refs, module))
                self._edge(
                    f"mounts_router|{parent}|{child}|{module.locator}:{row['start_line']}",
                    "mounts_router", parent, child, "derived",
                    {
                        "method": attributes.get("method"),
                        "prefix": attributes.get("prefix"),
                        "line": row["start_line"],
                        "locator": module.locator,
                    },
                    (), links,
                )  # fmt: skip

    def _prefixes(
        self, router: str, depth: int, seen: frozenset[str]
    ) -> list[tuple[str | None, list[str], set[str], set[str]]]:
        """(prefix or None, chain, reasons, conditions) for every mount chain to an app."""
        module, row = self.routers[router]
        attributes = row["attributes"]
        constructor = str(attributes.get("constructor"))
        framework = "fastapi" if constructor in {"FastAPI", "APIRouter"} else "flask"
        is_app = constructor in {"FastAPI", "Flask"}
        own = attributes.get("prefix") or {"state": "absent"}
        parents = self.mounts.get(router, [])
        if not parents:
            if is_app:
                return [("", [router], set(), set())]
            # An absence claim: only made when every mount call in the run was bound.
            code = (
                "ROUTER_NOT_MOUNTED"
                if not self.counters["mounts_unbound"]
                else ("MOUNT_NOT_DETERMINABLE")
            )
            return [(None, [router], {code}, set())]
        if depth >= MOUNT_DEPTH:
            return [(None, [router], {"MOUNT_DEPTH_EXCEEDED"}, set())]
        values: list[tuple[str | None, list[str], set[str], set[str]]] = []
        for parent, mount, _, _ in parents:
            if parent in seen:
                values.append((None, [router], {"MOUNT_CYCLE"}, set()))
                continue
            parent_constructor = str(self.routers[parent][1]["attributes"].get("constructor"))
            parent_framework = (
                "fastapi" if parent_constructor in {"FastAPI", "APIRouter"} else "flask"
            )
            mount_attributes = mount["attributes"]
            mounted = mount_attributes.get("prefix") or {"state": "absent"}
            method = mount_attributes.get("method")
            for prefix, chain, reasons, conditions in self._prefixes(
                parent, depth + 1, seen | {router}
            ):
                reasons = set(reasons)
                conditions = set(conditions)
                if parent_framework != framework and method != "mount":
                    reasons.add("FRAMEWORK_MISMATCH")
                segment: str | None
                if framework == "flask":
                    chosen = mounted if mounted.get("state") != "absent" else own
                    segment = _literal_value(chosen)
                    if segment is None:
                        reasons.add("ROUTER_PREFIX_UNKNOWN")
                    elif prefix is not None:
                        segment = flask_join(prefix, segment) if segment else prefix
                    value = segment if prefix is not None else None
                else:
                    outer = _literal_value(mounted)
                    inner = _literal_value(own) if not is_app else ""
                    if outer is None or inner is None:
                        reasons.add("ROUTER_PREFIX_UNKNOWN")
                        value = None
                    elif method == "mount":
                        outer = outer.rstrip("/")
                        if outer and not outer.startswith("/"):
                            reasons.add("PREFIX_INVALID")
                        value = None if prefix is None else prefix + outer + inner
                    else:
                        if not fastapi_prefix(outer) or not fastapi_prefix(inner):
                            reasons.add("PREFIX_INVALID")
                        value = None if prefix is None else prefix + outer + inner
                    if len(chain) > 1:
                        conditions.add("INCLUDE_ORDER_NOT_CHECKED")
                values.append(
                    (value if not reasons else None, [*chain, router], reasons, conditions)
                )
        return values

    def _chain_refs(self, chain: Sequence[str]) -> list[Row]:
        """Literal refs for every router and mount row a full path depends on, each cited
        with its own module's locator."""
        refs: list[Row] = []
        for index, key in enumerate(chain):
            module, row = self.routers[key]
            refs.append(literal_ref(module.locator, row))
            if index:
                parent = chain[index - 1]
                for mounted_by, mount, binding_refs, owner in self.mounts.get(key, []):
                    if mounted_by == parent:
                        refs.append(literal_ref(owner.locator, mount))
                        refs.extend(binding_refs)
        return refs

    def _endpoints(self) -> list[tuple[str, str, set[str], str, set[str]]]:
        """Endpoint nodes; returns (endpoint key, full path, methods, framework, conditions)."""
        matchable: list[tuple[str, str, set[str], str, set[str]]] = []
        for endpoint in sorted(self.endpoints, key=lambda item: item.portable_key):
            module = self.modules.get(endpoint.locator)
            key = "endpoint:" + endpoint.portable_key.removeprefix("api_endpoint:")
            methods = {item for item in (endpoint.method or "").split(",") if item}
            framework = "flask" if endpoint.recognizer.startswith("flask") else "fastapi"
            catalog = EvidenceLink("catalog", endpoint.fact_digest)
            router: str | None = None
            reasons: set[str] = set()
            paths: list[Row] = []
            if module is None or not module.usable:
                reasons.add("LITERALS_NOT_EXTRACTED")
            elif endpoint.receiver is None:
                reasons.add("ROUTER_UNBOUND")
            else:
                router, state, _ = self._resolve(module, endpoint.owner, endpoint.receiver)
                if router is None:
                    reasons.add("ROUTER_UNBOUND" if state != "OBJECT_AMBIGUOUS" else state)
            if endpoint.path is None:
                reasons.add("ROUTE_PATH_UNKNOWN")
            refs: list[Row] = []
            if router is not None and endpoint.path is not None and not reasons:
                for prefix, chain, chain_reasons, conditions in self._prefixes(
                    router, 0, frozenset()
                ):
                    refs.extend(self._chain_refs(chain))
                    full: str | None = None
                    route_reasons = set(chain_reasons)
                    if prefix is not None:
                        if framework == "flask":
                            full = flask_join(prefix, endpoint.path)
                        elif endpoint.path and not endpoint.path.startswith("/"):
                            route_reasons.add("PREFIX_INVALID")
                        else:
                            full = prefix + endpoint.path
                    paths.append(
                        {
                            "path": full if not route_reasons else None,
                            "status": "derived" if full and not route_reasons else "unknown",
                            "reasons": sorted(route_reasons),
                            "conditions": sorted(conditions),
                            "chain": chain,
                        }
                    )
                    if full and not route_reasons and "WEBSOCKET" not in methods:
                        matchable.append((key, full, methods, framework, conditions))
            for code in reasons | {r for item in paths for r in item["reasons"]}:
                self.counters[f"endpoint_reason:{code}"] += 1
            links = [catalog]
            if router is not None:
                links.append(
                    self.evidence(
                        "literal_rows",
                        _unique(refs) or self._chain_refs([router]),
                        {"algorithm": "router-binding.v1", "paths": paths},
                        f"{endpoint.function} is a route of {router.split('#', 1)[-1]}; "
                        + (
                            ", ".join(item["path"] or "unknown path" for item in paths)
                            or "no full path"
                        ),
                    )
                )
            self.counters["endpoints"] += 1
            if any(item["status"] == "derived" for item in paths):
                self.counters["endpoints_with_full_path"] += 1
            self._node(
                key, "api_endpoint", f"module:{endpoint.locator}",
                f"{endpoint.method or '?'} "
                f"{endpoint.path if endpoint.path is not None else '?'}",
                endpoint.locator, "Python",
                {
                    "methods": sorted(methods),
                    "route_path": endpoint.path,
                    "receiver": endpoint.receiver,
                    "function": endpoint.function,
                    "router": router,
                    "recognizer": endpoint.recognizer,
                    "strength": endpoint.strength,
                    "framework": framework,
                    "paths": paths,
                    "reasons": sorted(reasons),
                },
                links, entity=endpoint.entity_key,
            )  # fmt: skip
            if router is not None:
                self._edge(
                    f"defines_endpoint|{router}|{key}", "defines_endpoint", router, key,
                    "inferred", {"function": endpoint.function}, (), links,
                )  # fmt: skip
            if reasons & {"ROUTER_UNBOUND", "OBJECT_AMBIGUOUS"}:
                self._diagnose(
                    "ROUTER_UNBOUND", "unknown", [key], {"receiver": endpoint.receiver}, links
                )
            unknown_prefix = any("ROUTER_PREFIX_UNKNOWN" in item["reasons"] for item in paths)
            if unknown_prefix:
                self._diagnose("ROUTER_PREFIX_UNKNOWN", "unknown", [key], {}, links)
            if any("ROUTER_NOT_MOUNTED" in item["reasons"] for item in paths) and router:
                self._diagnose("ROUTER_NOT_MOUNTED", "unknown", [router], {}, links)
        return matchable

    def _duplicates(self, paths: list[tuple[str, str, set[str], str, set[str]]]) -> None:
        seen: dict[tuple[str, str], set[str]] = defaultdict(set)
        for key, path, methods, _, _ in paths:
            for method in methods or {"?"}:
                seen[(method, path)].add(key)
        for (method, path), keys in sorted(seen.items()):
            if len(keys) > 1:
                links = [link for item in sorted(keys) for link in self.nodes[item].evidence]
                self._diagnose(
                    "DUPLICATE_ROUTE",
                    "derived",
                    sorted(keys),
                    {"method": method, "path": path},
                    links,
                )

    # ----------------------------------------------------------------- clients

    def _sink(self, module: ApiModule, row: Row) -> tuple[bool, set[str], str]:
        callee = str(row["subject"])
        attributes = row["attributes"]
        if attributes.get("sink") == "fetch":
            if callee == "fetch":
                if "fetch" in module.top_level_names or "fetch" in module.bindings:
                    return False, set(), "FETCH_SHADOWED"
                return True, {"GLOBAL_FETCH_ASSUMED"}, "fetch"
            return True, set(), "fetch"
        binding = module.bindings.get("axios")
        if (
            binding is not None
            and binding.specifier == "axios"
            and binding.name in {"default", "*"}
        ):
            return True, set(), "axios"
        return False, set(), "AXIOS_NOT_BOUND"

    def _method(self, sink: str, attributes: Row, init: Row) -> tuple[str | None, tuple[str, ...]]:
        if sink == "axios":
            verb = attributes.get("verb")
            if verb and verb != "request":
                return str(verb).upper(), ()
            method = init.get("method") or {"state": "absent"}
            if init.get("kind") == "absent" or (
                init.get("kind") == "object"
                and method.get("state") == "absent"
                and not init.get("spreads")
                and not init.get("computed")
            ):
                return "GET", ("METHOD_DEFAULT_GET",)
            if method.get("state") == "literal" and not init.get("spreads"):
                return str(method["value"]).upper(), ()
            return None, ()
        if init.get("kind") == "absent":
            return "GET", ("METHOD_DEFAULT_GET",)
        if init.get("kind") != "object" or init.get("spreads") or init.get("computed"):
            return None, ()
        method = init.get("method") or {"state": "absent"}
        if method.get("state") == "absent":
            return "GET", ("METHOD_DEFAULT_GET",)
        if method.get("state") == "literal":
            return normalise_fetch_method(str(method["value"]))
        return None, ()

    def _wrapper(self, module: ApiModule, row: Row, sinks: list[Row]) -> _Wrapper | None:
        attributes = row["attributes"]
        name = attributes.get("function")
        if not name or attributes.get("depth") != 1:
            return None
        if attributes.get("conditional") or attributes.get("loop") or attributes.get("callback"):
            return None
        if sum(1 for other in sinks if other["attributes"].get("function") == name) != 1:
            return None
        url = attributes.get("url") or {}
        if url.get("state") != "template":
            return None
        parts = _template_parts(url)
        parameters = list(attributes.get("parameters") or [])
        if not parts or parts[-1][0] != "e" or parts[-1][1] not in parameters:
            return None
        if len(parts) > 2 or (len(parts) == 2 and parts[0][0] != "e"):
            return None
        init = attributes.get("init") or {"kind": "absent"}
        init_index: int | None = None
        fixed: str | None = None
        state = "fixed"
        if init.get("kind") == "identifier" and init.get("name") in parameters:
            init_index, state = parameters.index(init["name"]), "caller"
        elif init.get("kind") == "object":
            spreads = list(init.get("spreads") or [])
            method = init.get("method") or {"state": "absent"}
            if init.get("computed") or len(spreads) > 1:
                return None
            if spreads:
                if spreads[0] not in parameters:
                    return None
                init_index = parameters.index(spreads[0])
                state = "caller" if method.get("state") == "absent" else "unknown"
            elif method.get("state") == "literal":
                fixed = str(method["value"])
            elif method.get("state") != "absent":
                state = "unknown"
        elif init.get("kind") != "absent":
            return None
        return _Wrapper(
            module, str(name), row, parts[:-1], parameters.index(parts[-1][1]), init_index,
            fixed, state,
        )  # fmt: skip

    def _fold(self, module: ApiModule, parts: list[Part], depth: int = FOLD_DEPTH) -> list[Branch]:
        return self._fold_helper(module, parts, set(), depth)

    def _lookup(
        self, module: ApiModule, name: str, kind: str
    ) -> tuple[ApiModule, Row, list[Row]] | None:
        for row in self._rows(module, kind):
            if row["subject"] == name:
                return module, row, []
        binding = module.bindings.get(name)
        if binding is not None and binding.name not in {None, "*", "default"}:
            target = self.modules.get(binding.target)
            if target is not None:
                for row in self._rows(target, kind):
                    if row["subject"] == binding.name:
                        return target, row, [binding.ref]
        return None

    def _expand(self, module: ApiModule, text: str, depth: int) -> list[Branch]:
        opaque = [Branch([("e", text)])]
        if depth <= 0 or text == "<expr>":
            return opaque
        call = _CALL.match(text)
        if call is None and re.match(r"^[A-Za-z_$][\w$]*$", text):
            found = self._lookup(module, text, "url_constant")
            if found is None:
                return opaque
            owner, row, refs = found
            attributes = row["attributes"]
            value = _template_parts(attributes["value"])
            refs = [*refs, literal_ref(owner.locator, row)]
            folded = self._fold(owner, value, depth - 1)
            for branch in folded:
                branch.refs = [*refs, *branch.refs]
            if attributes.get("operator"):
                for branch in folded:
                    branch.conditions.add("BASE_FROM_FALLBACK_LITERAL")
                primary = Branch([("e", f"<{attributes.get('primary') or 'expr'}>")], refs=refs)
                primary.reasons.add("BASE_UNKNOWN")
                return [primary, *folded]
            return folded
        if call is not None:
            found = self._lookup(module, call.group(1), "url_helper")
            if found is None:
                return opaque
            owner, row, refs = found
            attributes = row["attributes"]
            parameters = list(attributes.get("parameters") or [])
            arguments = [item.strip() for item in call.group(2).split(",")] if call.group(2) else []
            mapping = dict(zip(parameters, arguments, strict=False))
            substituted: list[Part] = [
                ("e", mapping[item]) if kind == "e" and item in mapping else (kind, item)
                for kind, item in _template_parts(attributes["value"])
            ]
            folded = self._fold_helper(owner, substituted, set(mapping.values()), depth - 1)
            for branch in folded:
                branch.refs = [*refs, literal_ref(owner.locator, row), *branch.refs]
            return folded
        return opaque

    def _fold_helper(
        self, module: ApiModule, parts: list[Part], arguments: set[str], depth: int
    ) -> list[Branch]:
        """Fold placeholders into branches; ``arguments`` (a helper's substituted call
        arguments) stay opaque placeholders."""
        branches = [Branch([])]
        for kind, value in parts:
            options = (
                [Branch([(kind, value)])]
                if kind == "s" or value in arguments
                else self._expand(module, value, depth)
            )
            branches = [
                Branch(
                    [*branch.parts, *option.parts],
                    branch.conditions | option.conditions,
                    branch.reasons | option.reasons,
                    [*branch.refs, *option.refs],
                )
                for branch in branches
                for option in options
            ]
        return branches

    def _sites(self) -> list[_Site]:
        sites: list[_Site] = []
        wrappers: dict[tuple[str, str], _Wrapper] = {}
        for module in self.modules.values():
            sinks = self._rows(module, "http_call")
            valid: list[tuple[Row, set[str], str]] = []
            for row in sinks:
                ok, conditions, sink = self._sink(module, row)
                if not ok:
                    self.counters["unrecognized_client"] += 1
                    text = (
                        f"{module.locator}:{row['start_line']} {row['subject']}(...) "
                        "is not a recognized request sink"
                    )
                    self._diagnose(
                        "UNRECOGNIZED_CLIENT", "unknown", [f"module:{module.locator}"],
                        {"line": row["start_line"], "reason": sink},
                        [self._link(module, [row], {"reason": sink}, text)],
                    )  # fmt: skip
                    continue
                valid.append((row, conditions, sink))
            valid_rows = [row for row, _, _ in valid]
            for row, conditions, sink in valid:
                wrapper = self._wrapper(module, row, valid_rows)
                if wrapper is not None:
                    wrappers[(module.locator, wrapper.name)] = wrapper
                    self.counters["wrappers"] += 1
                    continue
                attributes = row["attributes"]
                init = attributes.get("init") or {"kind": "absent"}
                url = attributes.get("url") or {}
                method, method_conditions = self._method(sink, attributes, init)
                sites.append(
                    _Site(
                        f"client:{module.locator}:{row['ordinal']}", module, row, sink,
                        _template_parts(url) if url.get("state") == "template" else None,
                        method, method_conditions, set(conditions),
                        [literal_ref(module.locator, row)],
                    )
                )  # fmt: skip
        for module in self.modules.values():
            for row in self._rows(module, "url_call"):
                attributes = row["attributes"]
                callee = str(row["subject"])
                wrapper, refs, state = self._wrapper_for(module, callee, wrappers)
                if wrapper is None:
                    self.counters[f"url_call:{state}"] += 1
                    if state == "WRAPPER_BINDING_UNKNOWN":
                        text = (
                            f"{module.locator}:{row['start_line']} calls {callee} "
                            "through an import that does not bind a wrapper"
                        )
                        self._diagnose(
                            "WRAPPER_BINDING_UNKNOWN", "unknown", [f"module:{module.locator}"],
                            {"line": row["start_line"], "callee": callee},
                            [self._link(module, [row], {"callee": callee}, text)],
                        )  # fmt: skip
                    continue
                if attributes.get("position") != wrapper.path_index:
                    self.counters["url_call:WRAPPER_ARGUMENT_MISMATCH"] += 1
                    continue
                method, method_conditions = self._wrapper_method(wrapper, attributes)
                _, sink_conditions, sink = self._sink(wrapper.module, wrapper.sink)
                url = attributes.get("url") or {}
                caller = _template_parts(url) if url.get("state") == "template" else None
                sites.append(
                    _Site(
                        f"client:{module.locator}:{row['ordinal']}", module, row, sink, caller,
                        method, method_conditions, set(sink_conditions),
                        [
                            literal_ref(module.locator, row),
                            literal_ref(wrapper.module.locator, wrapper.sink),
                            *refs,
                        ],
                        wrapper=f"{wrapper.module.locator}#{wrapper.name}",
                        base=list(wrapper.base),
                        base_module=wrapper.module,
                    )
                )  # fmt: skip
        return sites

    def _wrapper_for(
        self, module: ApiModule, callee: str, wrappers: Mapping[tuple[str, str], _Wrapper]
    ) -> tuple[_Wrapper | None, list[Row], str]:
        parts = callee.split(".")
        binding = module.bindings.get(parts[0])
        if len(parts) == 1:
            if binding is None:
                found = wrappers.get((module.locator, callee))
                return (found, [], "bound") if found else (None, [], "UNCLASSIFIED_URL_USAGE")
            if binding.name in {None, "*", "default"}:
                return None, [], "UNCLASSIFIED_URL_USAGE"
            found = wrappers.get((binding.target, str(binding.name)))
            if found is not None:
                return found, [binding.ref], "bound"
            if any(name == binding.name for (_, name) in wrappers):
                return None, [], "WRAPPER_BINDING_UNKNOWN"
            return None, [], "UNCLASSIFIED_URL_USAGE"
        if len(parts) == 2 and binding is not None and binding.name in {None, "*"}:
            found = wrappers.get((binding.target, parts[1]))
            if found is not None:
                return found, [binding.ref], "bound"
        return None, [], "UNCLASSIFIED_URL_USAGE"

    def _wrapper_method(
        self, wrapper: _Wrapper, attributes: Row
    ) -> tuple[str | None, tuple[str, ...]]:
        if wrapper.method_state == "unknown":
            return None, ()
        if wrapper.method_state == "fixed":
            if wrapper.fixed_method is None:
                return "GET", ("METHOD_DEFAULT_GET",)
            return normalise_fetch_method(wrapper.fixed_method)
        assert wrapper.init_index is not None
        if int(attributes.get("arity") or 0) <= wrapper.init_index:
            return "GET", ("METHOD_VIA_WRAPPER_INIT",)
        init = (attributes.get("objects") or {}).get(str(wrapper.init_index))
        if not init or init.get("kind") != "object" or init.get("spreads") or init.get("computed"):
            return None, ()
        method = init.get("method") or {"state": "absent"}
        if method.get("state") == "absent":
            return "GET", ("METHOD_VIA_WRAPPER_INIT",)
        if method.get("state") == "literal":
            value, conditions = normalise_fetch_method(str(method["value"]))
            return value, (*conditions, "METHOD_VIA_WRAPPER_INIT")
        return None, ()

    def _clients(self, endpoints: list[tuple[str, str, set[str], str, set[str]]]) -> None:
        compiled = []
        for key, path, methods, framework, conditions in endpoints:
            pattern = backend_pattern(path, framework)
            if pattern is not None:
                compiled.append((key, path, methods, pattern[0], pattern[1], conditions))
        incomplete = self.counters["endpoints"] > self.counters["endpoints_with_full_path"]
        for site in self._sites():
            self.counters["client_calls"] += 1
            branches = self._site_branches(site)
            results: list[Row] = []
            candidates: list[tuple[str, set[str]]] = []
            destinations: list[tuple[str, Row]] = []
            refs = list(site.refs)
            for branch in branches:
                refs.extend(branch.refs)
                result = self._branch(site, branch, compiled, incomplete)
                results.append(result)
                self.counters[f"outcome:{result['outcome']}"] += 1
                if result["outcome"] == "conditional_candidate":
                    candidates.append((result["candidates"][0], set(result["conditions"])))
                if result.get("host"):
                    destinations.append((result["destination"], result))
            row = site.row
            via = f" via {site.wrapper}" if site.wrapper else ""
            link = self.evidence(
                "literal_rows", _unique(refs),
                {"algorithm": "client-folding.v1", "branches": results},
                f"{site.module.locator}:{row['start_line']} issues {site.method or '?'} "
                f"{', '.join(item['display'] for item in results)}{via}",
            )  # fmt: skip
            tier = "observed" if site.wrapper is None else "derived"
            self._node(
                site.key, "client_call", f"module:{site.module.locator}",
                f"{site.method or '?'} {results[-1]['display'] if results else '?'}",
                site.module.locator, site.module.language,
                {
                    "sink": site.sink,
                    "wrapper": site.wrapper,
                    "method": site.method,
                    "method_conditions": list(site.method_conditions),
                    "line": row["start_line"],
                    "branches": results,
                },
                [link],
            )  # fmt: skip
            self._edge(
                f"issues_request|module:{site.module.locator}|{site.key}", "issues_request",
                f"module:{site.module.locator}", site.key, tier, {"sink": site.sink}, (), [link],
            )  # fmt: skip
            merged: dict[str, set[str]] = defaultdict(set)
            for endpoint, conditions in candidates:
                merged[endpoint] |= conditions
            for endpoint, conditions in sorted(merged.items()):
                self._edge(
                    f"calls_endpoint|{site.key}|{endpoint}", "calls_endpoint", site.key, endpoint,
                    "inferred", {"api_matching": API_MATCHING_VERSION}, conditions,
                    [link, *self.nodes[endpoint].evidence],
                )  # fmt: skip
            for destination, result in destinations:
                if destination not in {"private_network_host_unbound", "public_host_unbound"}:
                    continue
                host_key = f"destination:{result['scheme'] or ''}//{result['host']}" + (
                    f":{result['port']}" if result.get("port") else ""
                )
                if host_key not in self.nodes:
                    self._node(
                        host_key, "external_destination", None, str(result["host"]), None, None,
                        {"ownership": "unknown", "class": destination, "host": result["host"],
                         "port": result.get("port"), "scheme": result.get("scheme")},
                        [link],
                    )  # fmt: skip
                self._edge(
                    f"targets_destination|{site.key}|{host_key}", "targets_destination",
                    site.key, host_key, "observed", {"class": destination}, (), [link],
                )  # fmt: skip
            self._client_diagnostics(site.key, results, link)

    def _site_branches(self, site: _Site) -> list[Branch]:
        if site.url is None:
            return [Branch([("e", "<expr>")], reasons={"URL_NOT_RECORDED"})]
        tail = self._fold(site.module, site.url)
        if site.base_module is None:
            return tail
        head = self._fold(site.base_module, site.base) if site.base else [Branch([])]
        return [
            Branch(
                [*first.parts, *second.parts],
                first.conditions | second.conditions,
                first.reasons | second.reasons,
                [*first.refs, *second.refs],
            )
            for first in head
            for second in tail
        ]

    def _branch(
        self,
        site: _Site,
        branch: Branch,
        endpoints: list[tuple[str, str, set[str], list[Element], set[str], set[str]]],
        incomplete: bool,
    ) -> Row:
        conditions = set(branch.conditions) | set(site.conditions) | set(site.method_conditions)
        reasons = set(branch.reasons)
        try:
            classified = url_template(branch.parts)
        except LiteralRejected:
            classified = {"origin": {"kind": "relative_or_unknown"}, "parts": [{"e": "<expr>"}]}
        origin = cast(dict[str, Any], classified["origin"])
        parts: list[Part] = [
            ("s", str(value["s"])) if "s" in value else ("e", str(value["e"]))
            for value in cast(list[dict[str, str]], classified["parts"])
        ]
        result: Row = {
            "display": display(parts) or "/",
            "host": origin.get("host"),
            "port": origin.get("port"),
            "scheme": origin.get("scheme"),
            "candidates": [],
        }
        kind = origin.get("kind")
        if kind == "absolute" and origin.get("host"):
            destination = host_class(str(origin["host"]))
        elif kind == "path_absolute":
            destination = "same_origin_relative"
        elif kind == "absolute_opaque_host":
            destination, reasons = "opaque_origin", reasons | {"HOST_UNKNOWN"}
        elif parts and parts[0][0] == "e":
            destination, reasons = "opaque_origin", reasons | {"BASE_UNKNOWN"}
        else:
            destination, reasons = "opaque_origin", reasons | {"RELATIVE_PATH_NOT_ABSOLUTE"}
        result["destination"] = destination
        if destination == "same_origin_relative":
            conditions.add("SAME_ORIGIN_DEPLOYMENT_ASSUMED")
        if destination == "local_loopback":
            conditions.add("LOOPBACK_ORIGIN_ASSUMED_LOCAL_BACKEND")
        outcome: str
        if destination in {"private_network_host_unbound", "public_host_unbound"}:
            outcome = "external_destination"
        elif destination == "opaque_origin":
            outcome = "opaque_origin"
        elif classified.get("truncated") or not any(
            kind == "s" and value.strip("/") for kind, value in parts
        ):
            outcome, reasons = "opaque_path", reasons | {"NO_LITERAL_PATH_SEGMENT"}
        else:
            loose = [item for item in endpoints if intersects(client_pattern(parts), item[3])]
            strict = [
                item for item in loose if intersects(client_pattern(parts, strict=True), item[3])
            ]
            assumed = bool(strict) and len(strict) < len(loose)
            compatible = strict or loose
            if assumed:
                conditions.add("PLACEHOLDER_WITHIN_SEGMENT_ASSUMED")
                result["alternatives"] = [item[0] for item in loose if item not in strict][:20]
            spanning = not strict and any(kind == "e" for kind, _ in parts)
            if site.method is None:
                outcome = (
                    "ambiguous"
                    if len(compatible) > 1
                    else ("method_unknown" if compatible else "no_candidate")
                )
                result["candidates"] = [item[0] for item in compatible]
            else:
                exact = [item for item in compatible if site.method in item[2]]
                if len(exact) == 1:
                    outcome = "conditional_candidate"
                    _, _, _, _, kinds, endpoint_conditions = exact[0]
                    conditions |= set(endpoint_conditions)
                    if "typed" in kinds:
                        strength = "converter_unverified"
                        conditions.add("CONVERTER_UNVERIFIED")
                    elif "multi_segment" in kinds or spanning or not segment_aligned(parts):
                        strength = "spanning"
                        conditions.add("SEGMENT_SPANNING")
                    elif any(kind == "e" for kind, _ in parts):
                        strength = "segment_aligned"
                    else:
                        strength = "literal_exact"
                    result["strength"] = strength
                    result["candidates"] = [exact[0][0]]
                elif len(exact) > 1:
                    outcome = "ambiguous"
                    result["candidates"] = [item[0] for item in exact]
                elif compatible:
                    outcome = "method_mismatch"
                    result["candidates"] = [item[0] for item in compatible]
                else:
                    outcome = "no_candidate"
                    toggled = _toggle_slash(parts)
                    if toggled is not None and any(
                        intersects(client_pattern(toggled), item[3]) for item in endpoints
                    ):
                        reasons.add("TRAILING_SLASH_DIFFERS")
            if outcome in {"no_candidate", "method_mismatch"} and incomplete:
                reasons.add("BACKEND_PATHS_INCOMPLETE")
        result["outcome"] = outcome
        result["conditions"] = sorted(conditions)
        result["reasons"] = sorted(reasons)
        return result

    def _client_diagnostics(self, key: str, results: list[Row], link: EvidenceLink) -> None:
        for result in results:
            outcome = result["outcome"]
            subjects = [key, *[item for item in result["candidates"] if item in self.nodes]]
            attributes = {"path": result["display"], "reasons": result["reasons"]}
            if outcome == "ambiguous":
                self._diagnose("CLIENT_CALL_AMBIGUOUS", "inferred", subjects, attributes, [link])
            elif outcome == "method_mismatch":
                self._diagnose(
                    "METHOD_MISMATCH_NEAR_MATCH", "inferred", subjects, attributes, [link]
                )
            elif outcome == "no_candidate":
                code = (
                    "TRAILING_SLASH_DIFFERS"
                    if "TRAILING_SLASH_DIFFERS" in result["reasons"]
                    else "CLIENT_CALL_NO_CANDIDATE"
                )
                self._diagnose(code, "unknown", [key], attributes, [link])
            elif outcome == "opaque_origin" and "BASE_UNKNOWN" in result["reasons"]:
                self._diagnose("BASE_UNKNOWN", "unknown", [key], attributes, [link])

    # ----------------------------------------------------------------- coverage

    def coverage(self, states: Mapping[str, int]) -> Row:
        prefix_reasons = {
            key.split(":", 1)[1]: value
            for key, value in self.counters.items()
            if key.startswith("endpoint_reason:")
        }
        return {
            "literal_passes": dict(sorted(states.items())),
            "endpoints": {
                "total": self.counters["endpoints"],
                "with_full_path": self.counters["endpoints_with_full_path"],
                "reasons": dict(sorted(prefix_reasons.items())),
            },
            "routers": len(self.routers),
            "mounts": {
                "total": self.counters["mounts"],
                "unbound": self.counters["mounts_unbound"],
                "unrelated_mount_calls": self.counters["mount_rows_unrelated"],
            },
            "client_calls": {
                "total": self.counters["client_calls"],
                "wrappers": self.counters["wrappers"],
                "by_outcome": {
                    key.split(":", 1)[1]: value
                    for key, value in sorted(self.counters.items())
                    if key.startswith("outcome:")
                },
            },
            "unrecognized_clients": self.counters["unrecognized_client"],
            "url_usage": {
                key.split(":", 1)[1]: value
                for key, value in sorted(self.counters.items())
                if key.startswith("url_call:")
            },
        }


def _literal_value(value: Mapping[str, Any]) -> str | None:
    state = value.get("state")
    if state == "absent":
        return ""
    if state == "literal":
        return str(value["value"])
    return None


def _toggle_slash(parts: list[Part]) -> list[Part] | None:
    if not parts or parts[-1][0] != "s":
        return None
    last = parts[-1][1]
    toggled = last[:-1] if last.endswith("/") and len(last) > 1 else last + "/"
    return [*parts[:-1], ("s", toggled)]


def _unique(rows: Iterable[Row]) -> list[Row]:
    seen: dict[str, Row] = {}
    for row in rows:
        seen.setdefault(digest(row), row)
    return list(seen.values())


def _order(link: EvidenceLink) -> tuple[str, str]:
    return link.source, link.digest


def binding_kind(locator: str, name: str) -> bool:
    """Whether ``from X import name`` resolved to the submodule ``name`` (a module object)."""
    stem = posixpath.splitext(posixpath.basename(locator))[0]
    if stem == "__init__":
        return posixpath.basename(posixpath.dirname(locator)) == name
    return stem == name

"""The only path to provider traffic for semantic generation (§11.2, invariant O1).

A T1 transaction mints one single-use ``GateTicket`` per attempt. A ``StructuredGenerator``
must redeem the ticket against the exact canonical bytes before it may send, so the number of
provider requests can never exceed the number of minted tickets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from buildlens.domain.semantic_generation import OUTBOUND_VERSION, ProviderConfig


class OutboundRefusedError(Exception):
    """A send was attempted without a valid, unused ticket for exactly these bytes."""


ProviderErrorCode = Literal[
    "rate_limited",
    "provider_unavailable",
    "timeout",
    "context_rejected",
    "configuration_invalid",
]


_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _class_name(error: BaseException | None) -> str | None:
    if error is None:
        return None
    name = type(error).__name__
    return name if _CLASS_NAME.fullmatch(name) else "UNNAMED"


class ProviderCallError(Exception):
    """A failed provider call. Diagnostics are an HTTP status and exception class names only:
    never messages, bodies or headers, which may echo request content or credentials."""

    def __init__(
        self,
        code: ProviderErrorCode,
        retry_after: float | None = None,
        *,
        status: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        self.status = status if isinstance(status, int) and 100 <= status <= 599 else None
        self.exception_class = _class_name(error)
        cause = (error.__cause__ or error.__context__) if error is not None else None
        self.cause_class = _class_name(cause)

    @property
    def diagnostics(self) -> tuple[str, ...]:
        values = []
        if self.status is not None:
            values.append(f"HTTP_{self.status}")
        if self.exception_class:
            values.append(f"EXC_{self.exception_class}")
        if self.cause_class:
            values.append(f"CAUSE_{self.cause_class}")
        return tuple(values)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class CanonicalOutbound:
    """The exact provider-facing content BuildLens controls (SDK envelopes are outside it)."""

    provider: str
    model: str
    body: dict[str, Any]
    settings: dict[str, Any]

    @property
    def data(self) -> bytes:
        return canonical_json(
            {
                "version": OUTBOUND_VERSION,
                "provider": self.provider,
                "model": self.model,
                "body": self.body,
                "settings": self.settings,
            }
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(OUTBOUND_VERSION.encode() + b"\0" + self.data).hexdigest()

    @property
    def size(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class ProviderResult:
    raw_text: str
    state: Literal["completed", "incomplete", "refused"]
    returned_model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def response_digest(self) -> str:
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GateTicket:
    attempt_id: UUID
    run_id: UUID
    lease_token: UUID
    request_digest: str
    secret: UUID
    gate: OutboundGate = field(repr=False, compare=False)

    def redeem(self, canonical: CanonicalOutbound) -> None:
        self.gate.redeem(self, canonical)


class OutboundGate:
    """Per-run ticket authority. Tickets are single-use and bound to one request digest."""

    def __init__(self, run_id: UUID, lease_token: UUID) -> None:
        self.run_id, self.lease_token = run_id, lease_token
        self._issued: dict[UUID, bool] = {}
        self._lock = Lock()
        self.minted = 0
        self.redeemed = 0

    def mint(self, attempt_id: UUID, request_digest: str) -> GateTicket:
        ticket = GateTicket(
            attempt_id, self.run_id, self.lease_token, request_digest, uuid4(), self
        )
        with self._lock:
            self._issued[ticket.secret] = False
            self.minted += 1
        return ticket

    def redeem(self, ticket: GateTicket, canonical: CanonicalOutbound) -> None:
        with self._lock:
            if (
                ticket.gate is not self
                or ticket.run_id != self.run_id
                or ticket.lease_token != self.lease_token
                or self._issued.get(ticket.secret) is not False
            ):
                raise OutboundRefusedError("The ticket is unknown, foreign or already used.")
            if canonical.digest != ticket.request_digest:
                raise OutboundRefusedError("The outbound bytes differ from the authorised plan.")
            self._issued[ticket.secret] = True
            self.redeemed += 1

    async def send(
        self,
        generator: StructuredGenerator,
        canonical: CanonicalOutbound,
        ticket: GateTicket,
        timeout_seconds: float,
    ) -> ProviderResult:
        if canonical.digest != ticket.request_digest:
            raise OutboundRefusedError("The outbound bytes differ from the authorised plan.")
        return await generator.send(canonical, ticket, timeout_seconds)


class StructuredGenerator(Protocol):
    """Semantic generation port. Construction, ``config`` and ``render`` make no requests."""

    @property
    def config(self) -> ProviderConfig: ...

    def status(self) -> dict[str, object]: ...

    def render(
        self, instructions: str, data: str, schema: dict[str, Any], max_output_tokens: int
    ) -> CanonicalOutbound: ...

    async def send(
        self, canonical: CanonicalOutbound, ticket: GateTicket, timeout_seconds: float
    ) -> ProviderResult: ...

    async def close(self) -> None: ...

"""One-way passive observation. Production never reads a collector's state.

Collectors must only retain existing values: no IO, hydration, or inference. The
ContextVar follows asyncio.to_thread and isolates concurrent invocations. A broken
collector cannot change an operation's result or exception.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Protocol, TypeVar


class Observer(Protocol):
    def record(self, stage: str, value: object) -> None: ...
    def unavailable(self, stage: str) -> None: ...


_observer: ContextVar[Observer | None] = ContextVar("buildlens_observer", default=None)
T = TypeVar("T")


def emit(stage: str, value: object) -> None:
    observer = _observer.get()
    if observer is not None:
        try:
            observer.record(stage, value)
        except Exception:
            try:
                observer.unavailable(stage)
            except Exception:
                pass


def capture(stage: str, factory: Callable[[], T]) -> T | None:
    try:
        value = factory()
    except Exception:
        emit(stage + "_unavailable", None)
        return None
    emit(stage, value)
    return value


@contextmanager
def observing(observer: Observer) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def generation_settings(policy: object, transmitted: dict[str, object]) -> None:
    """Only explicit arguments from the actual adapter request count as sent."""
    from dataclasses import asdict, is_dataclass

    def project() -> dict[str, object]:
        requested = asdict(policy) if is_dataclass(policy) and not isinstance(policy, type) else {}
        names = (
            "model_id",
            "max_output_tokens",
            "temperature",
            "reasoning_effort",
            "thinking_level",
            "truncation",
            "provider_store",
            "automatic_retries",
            "streaming",
            "tools_enabled",
        )
        return {
            name: {
                "requested": requested.get(name),
                "sent": name in transmitted,
                "transmitted_value": transmitted.get(name),
                "effective": None,
                "effective_provenance": "provider-unconfirmed"
                if name in transmitted
                else "provider-default-unspecified",
            }
            for name in names
        }

    capture("generation_settings", project)


def provider_reported_settings(response: object, provenance: str) -> None:
    """Allowlisted explicit response fields; never infer defaults or retain raw output."""

    def project() -> dict[str, object]:
        # SDK response defaults are not provider observations. Only fields
        # actually present in the decoded response may confirm a setting.
        fields: set[str] = getattr(response, "model_fields_set", set())
        settings: dict[str, object] = {}
        for name in ("temperature", "max_output_tokens", "truncation"):
            if name not in fields:
                continue
            value = getattr(response, name, None)
            if type(value) in (str, int, float, bool):
                settings[name] = {"value": value, "provenance": provenance + "." + name}
        reasoning = getattr(response, "reasoning", None) if "reasoning" in fields else None
        effort = getattr(reasoning, "effort", None)
        if "effort" in getattr(reasoning, "model_fields_set", set()) and isinstance(effort, str):
            settings["reasoning_effort"] = {
                "value": effort,
                "provenance": provenance + ".reasoning.effort",
            }
        for name in ("model", "model_version"):
            value = getattr(response, name, None) if name in fields else None
            if isinstance(value, str):
                settings["model_id"] = {"value": value, "provenance": provenance + "." + name}
        return settings

    capture("provider_reported_settings", project)

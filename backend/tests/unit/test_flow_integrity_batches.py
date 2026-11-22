"""Large graph integrity must check every digest without crossing PostgreSQL's bind cap."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from buildlens.infrastructure.persistence.flow import SqlAlchemyFlowStore


def test_more_than_postgres_parameter_limit_is_batched_and_none_are_dropped() -> None:
    requested = {f"{i:064x}" for i in range(66_000)}
    queried: list[str] = []

    class Session:
        def scalars(self, statement: Any) -> list[Any]:
            params = statement.compile().params
            batch = next(v for v in params.values() if isinstance(v, list))
            assert 0 < len(batch) <= 1000
            queried.extend(batch)
            return []  # Every missing item must fail, including the final batch.

    store = SqlAlchemyFlowStore(cast(Any, None))
    failures = store._verify_evidence(
        cast(Any, Session()), cast(Any, SimpleNamespace(id=uuid4())), requested
    )
    assert len(queried) == len(set(queried)) == len(requested)
    assert set(queried) == requested
    assert len(failures) == len(requested)
    assert all(f.startswith("EVIDENCE_MISSING:") for f in failures)

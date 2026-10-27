"""Stage 13 Milestone D: record configuration failures as their own attempt outcome (additive)."""

import sqlalchemy as sa

from alembic import op

revision = "0013_semantic_config_outcome"
down_revision = "0012_semantic_generation"
branch_labels = None
depends_on = None

_TABLE = "semantic_generation_attempt_outcomes"
_NAME = "ck_semantic_generation_outcomes_code"
_CODES = (
    "accepted", "accepted_with_conflicts", "insufficient_evidence", "rejected", "rate_limited",
    "provider_unavailable", "timeout", "context_rejected", "refused", "token_bound_violated",
    "reused_concurrently", "outcome_unknown", "not_sent",
)  # fmt: skip


def _check(codes: tuple[str, ...]) -> str:
    return "outcome IN (" + ", ".join(f"'{code}'" for code in codes) + ")"


def upgrade() -> None:
    # Widening only: every existing row stays valid and nothing is rewritten.
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _check((*_CODES, "configuration_invalid")))


def downgrade() -> None:
    used = (
        op.get_bind()
        .execute(sa.text(f"SELECT count(*) FROM {_TABLE} WHERE outcome = 'configuration_invalid'"))
        .scalar_one()
    )
    if used:
        raise RuntimeError(
            "configuration_invalid attempt outcomes exist; downgrading would make them invalid. "
            "Restore a verified pre-upgrade backup instead."
        )
    op.drop_constraint(_NAME, _TABLE, type_="check")
    op.create_check_constraint(_NAME, _TABLE, _check(_CODES))

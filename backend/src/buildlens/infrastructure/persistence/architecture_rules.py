"""PostgreSQL store for owner-declared architecture rule sets (append-only versions)."""

from __future__ import annotations

from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from buildlens.application.projects import ProjectNotFoundError
from buildlens.domain.architecture_rules import (
    RULES_VERSION,
    Rule,
    RuleSet,
    rules_digest,
    validate_rules,
)
from buildlens.infrastructure.persistence.models import (
    ArchitectureRuleSetRecord,
    ProjectRecord,
)


def _rule_set(record: ArchitectureRuleSetRecord) -> RuleSet:
    rules = validate_rules(cast(list[Any], record.rules))
    return RuleSet(
        record.id,
        record.project_id,
        record.version,
        rules,
        record.digest,
        record.created_at,
        record.contract,
    )


class SqlAlchemyArchitectureRuleStore:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def add(self, project_id: UUID, rules: tuple[Rule, ...]) -> RuleSet:
        """Append the next version under a project row lock; earlier versions never change."""
        with self._session_factory.begin() as session:
            project = session.scalar(
                select(ProjectRecord).where(ProjectRecord.id == project_id).with_for_update()
            )
            if project is None:
                raise ProjectNotFoundError("Project was not found.")
            version = (
                session.scalar(
                    select(func.max(ArchitectureRuleSetRecord.version)).where(
                        ArchitectureRuleSetRecord.project_id == project_id
                    )
                )
                or 0
            ) + 1
            record = ArchitectureRuleSetRecord(
                id=uuid4(),
                project_id=project_id,
                version=version,
                contract=RULES_VERSION,
                rules=[rule.as_json() for rule in rules],
                digest=rules_digest(rules),
            )
            session.add(record)
            session.flush()
            session.refresh(record)
            return _rule_set(record)

    def get(self, project_id: UUID, version: int | None) -> RuleSet | None:
        with self._session_factory() as session:
            query = select(ArchitectureRuleSetRecord).where(
                ArchitectureRuleSetRecord.project_id == project_id
            )
            if version is not None:
                query = query.where(ArchitectureRuleSetRecord.version == version)
            record = session.scalar(
                query.order_by(ArchitectureRuleSetRecord.version.desc()).limit(1)
            )
            return _rule_set(record) if record else None

    def versions(self, project_id: UUID) -> list[RuleSet]:
        with self._session_factory() as session:
            return [
                _rule_set(record)
                for record in session.scalars(
                    select(ArchitectureRuleSetRecord)
                    .where(ArchitectureRuleSetRecord.project_id == project_id)
                    .order_by(ArchitectureRuleSetRecord.version)
                )
            ]

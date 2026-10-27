"""S12-OPS-05: lab database provisioning is scoped, preflighted and explicit about blast radius.

Runs against the disposable BUILDLENS_TEST_ADMIN_URL server only. Every test restores the
server-level state it touches (databases, roles, the `postgres` CONNECT grant).
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from buildlens.domain.lab import LabError
from buildlens.infrastructure.lab.database import (
    LabProvision,
    provision_lab_database,
    role_privileges,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def server() -> Iterator[dict[str, object]]:
    url = os.getenv("BUILDLENS_TEST_ADMIN_URL")
    if not url:
        if os.getenv("CI"):
            pytest.fail("CI requires BUILDLENS_TEST_ADMIN_URL; integration cannot be skipped")
        pytest.skip("Set BUILDLENS_TEST_ADMIN_URL to enable disposable PostgreSQL tests")
    suffix = uuid4().hex[:10]
    names = {"database": f"lab_t_{suffix}", "role": f"lab_t_{suffix}", "other": f"other_{suffix}"}
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    try:
        yield {"url": url, "admin": admin, **names}
    finally:
        with admin.connect() as connection:
            for database in (names["database"], names["other"]):
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{names["role"]}"'))
            connection.execute(text("GRANT CONNECT ON DATABASE postgres TO PUBLIC"))
        admin.dispose()


def _acls(admin: Engine) -> dict[str, str | None]:
    with admin.connect() as connection:
        rows = connection.execute(
            text("select datname, datacl::text from pg_database order by datname")
        ).all()
    return {name: acl for name, acl in rows}


def _role_exists(admin: Engine, role: str) -> bool:
    with admin.connect() as connection:
        found = connection.execute(text("select 1 from pg_roles where rolname=:r"), {"r": role})
        return found.first() is not None


def _provision(server: dict[str, object], **kwargs: bool) -> LabProvision:
    return provision_lab_database(
        str(server["url"]),
        str(server["database"]),
        str(server["role"]),
        secrets.token_urlsafe(24),
        production_url=None,
        **kwargs,
    )


def test_shared_server_is_refused_before_any_change(server: dict[str, object]) -> None:
    admin = server["admin"]
    assert isinstance(admin, Engine)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{server["other"]}" TEMPLATE template0'))
    before = _acls(admin)
    for dedicated, code in (
        (False, "LAB_DATABASE_ROLE_NOT_CONFINED"),
        (True, "LAB_DATABASE_SERVER_NOT_DEDICATED"),
    ):
        with pytest.raises(LabError) as caught:
            _provision(server, dedicated_server=dedicated)
        assert caught.value.code == code
        assert str(server["other"]) in caught.value.message
        assert "Nothing was changed" in caught.value.message
    assert _acls(admin) == before  # no database ACL anywhere on the server changed
    assert str(server["database"]) not in before
    assert not _role_exists(admin, str(server["role"]))


def test_scoped_provisioning_touches_only_the_lab_database(server: dict[str, object]) -> None:
    admin = server["admin"]
    assert isinstance(admin, Engine)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{server["other"]}" TEMPLATE template0'))
        # The operator has confined PUBLIC on every other database themselves.
        others = connection.execute(
            text("select datname from pg_database where not datistemplate")
        ).scalars()
        for name in list(others):
            connection.execute(text(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC'))
    before = _acls(admin)
    provisioned = _provision(server)
    assert provisioned.server_wide_revocations == ()
    after = _acls(admin)
    changed = {name for name in after if after[name] != before.get(name)}
    assert changed == {str(server["database"])}
    url = provisioned.url
    privileges = role_privileges(url)
    assert privileges["connectable_databases"] == [server["database"]]
    assert privileges["public_schema_create"] is False and privileges["lab_schema_create"]
    assert not any(privileges[k] for k in ("superuser", "createdb", "createrole"))
    escape = make_url(url).set(database=str(server["other"]))
    with pytest.raises(Exception) as denied:
        create_engine(escape).connect().close()
    assert "permission denied" in str(denied.value).lower()
    assert str(make_url(url).password) not in repr(provisioned)


def test_dedicated_server_mode_reports_its_only_server_wide_change(
    server: dict[str, object],
) -> None:
    admin = server["admin"]
    assert isinstance(admin, Engine)
    with admin.connect() as connection:
        others = set(
            connection.execute(
                text("select datname from pg_database where not datistemplate")
            ).scalars()
        )
        connection.execute(text("GRANT CONNECT ON DATABASE postgres TO PUBLIC"))
    if others != {"postgres"}:
        pytest.skip(f"test server is not otherwise empty: {sorted(others)}")
    with pytest.raises(LabError) as caught:
        _provision(server)  # default mode never makes the server-wide change
    assert caught.value.code == "LAB_DATABASE_ROLE_NOT_CONFINED"
    provisioned = _provision(server, dedicated_server=True)
    assert provisioned.server_wide_revocations == ("postgres",)
    privileges = role_privileges(provisioned.url)
    assert privileges["connectable_databases"] == [server["database"]]


@pytest.mark.parametrize("grant", ["direct", "membership"])
def test_existing_role_grants_are_refused_before_any_change(
    server: dict[str, object], grant: str
) -> None:
    """Defect: PUBLIC was preflighted, but a pre-existing role's own CONNECT grant (direct or
    via role membership) was found only after the lab database and role had been changed."""
    admin = server["admin"]
    assert isinstance(admin, Engine)
    role, other = str(server["role"]), str(server["other"])
    group = role + "_grp"
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{other}" TEMPLATE template0'))
            for name in list(
                connection.execute(
                    text("select datname from pg_database where not datistemplate")
                ).scalars()
            ):
                connection.execute(text(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC'))
            connection.execute(text(f'CREATE ROLE "{role}" LOGIN CONNECTION LIMIT 3'))
            if grant == "direct":
                connection.execute(text(f'GRANT CONNECT ON DATABASE "{other}" TO "{role}"'))
            else:
                connection.execute(text(f'CREATE ROLE "{group}" NOLOGIN'))
                connection.execute(text(f'GRANT CONNECT ON DATABASE "{other}" TO "{group}"'))
                connection.execute(text(f'GRANT "{group}" TO "{role}"'))
        before = _acls(admin)
        with admin.connect() as connection:
            limit_before = connection.execute(
                text("select rolconnlimit from pg_roles where rolname=:r"), {"r": role}
            ).scalar()
        with pytest.raises(LabError) as caught:
            _provision(server)
        assert caught.value.code == "LAB_DATABASE_ROLE_NOT_CONFINED"
        assert other in caught.value.message and "Nothing was changed" in caught.value.message
        assert _acls(admin) == before
        assert str(server["database"]) not in _acls(admin)  # lab database never created
        with admin.connect() as connection:
            limit_after = connection.execute(
                text("select rolconnlimit from pg_roles where rolname=:r"), {"r": role}
            ).scalar()
        assert limit_after == limit_before == 3  # role not altered
    finally:
        with admin.connect() as connection:
            for database in (other, str(server["database"])):
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{role}"'))
            connection.execute(text(f'DROP ROLE IF EXISTS "{group}"'))

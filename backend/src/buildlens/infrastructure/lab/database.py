"""Separate laboratory PostgreSQL database with a least-privilege role.

Production evidence never lives here and the production URL is refused. The lab role:
NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS, a connection limit, a
statement/lock timeout, CONNECT only on the lab database, and CREATE/USAGE only on its own
`lab` schema. Inside the lab database PUBLIC loses CONNECT and CREATE on `public`.

Blast radius. By default provisioning changes only the lab role and the lab database. A
role can still CONNECT to any OTHER database whose CONNECT privilege is granted to PUBLIC
(PostgreSQL's default), so before changing anything the server is checked: if such a
database exists, provisioning refuses (LAB_DATABASE_ROLE_NOT_CONFINED) and names it. The
operator either revokes PUBLIC CONNECT there themselves, or passes `dedicated_server=True`
for a disposable lab-only server; that mode first verifies the server holds no databases
besides `postgres` and the lab database (LAB_DATABASE_SERVER_NOT_DEDICATED otherwise), and
only then revokes PUBLIC CONNECT on `postgres` — the one cluster-wide change, reported back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from buildlens.domain.lab import LabError

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")
_PASSWORD = re.compile(r"^[A-Za-z0-9_-]{24,64}$")  # token_urlsafe output only; safe to inline
_SERVER_DATABASES = frozenset({"postgres"})  # allowed beside the lab DB on a dedicated server
# Non-template databases on which PUBLIC holds CONNECT (a NULL ACL means the default grant).
_PUBLIC_CONNECT = text(
    "select datname from pg_database where not datistemplate and datname <> :d and "
    "(datacl is null or exists (select 1 from aclexplode(datacl) a "
    "where a.grantee = 0 and a.privilege_type = 'CONNECT')) order by datname"
)


@dataclass(frozen=True, slots=True)
class LabProvision:
    url: str
    server_wide_revocations: tuple[str, ...]  # databases whose PUBLIC CONNECT was revoked

    def __repr__(self) -> str:  # never print the credential
        return f"LabProvision(server_wide_revocations={self.server_wide_revocations!r})"


def provision_lab_database(
    admin_url: str,
    database: str,
    role: str,
    password: str,
    *,
    production_url: str | None,
    dedicated_server: bool = False,
) -> LabProvision:
    """Errors are re-raised without SQL text or parameters (they could contain secrets)."""
    try:
        return _provision(
            admin_url,
            database,
            role,
            password,
            production_url=production_url,
            dedicated_server=dedicated_server,
        )
    except SQLAlchemyError as error:
        raise LabError("LAB_DATABASE_PROVISION_FAILED", type(error).__name__) from None


def _provision(
    admin_url: str,
    database: str,
    role: str,
    password: str,
    *,
    production_url: str | None,
    dedicated_server: bool,
) -> LabProvision:
    if not _IDENT.match(database) or not _IDENT.match(role):
        raise LabError("LAB_DATABASE_IDENTIFIER_INVALID")
    if not _PASSWORD.match(password):
        raise LabError("LAB_DATABASE_PASSWORD_INVALID")
    admin = make_url(admin_url)
    if production_url is not None:
        production = make_url(production_url)
        if (admin.host, admin.port) == (production.host, production.port):
            raise LabError(
                "LAB_DATABASE_NOT_SEPARATE", "Refusing to provision on the production server"
            )
    engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    revoked: list[str] = []
    try:
        with engine.connect() as connection:
            # Preflight: nothing is changed until the role is known to be confinable.
            exposed = list(connection.execute(_PUBLIC_CONNECT, {"d": database}).scalars())
            if exposed and not dedicated_server:
                raise LabError(
                    "LAB_DATABASE_ROLE_NOT_CONFINED",
                    f"PUBLIC can CONNECT to other database(s) on this server: {exposed}. "
                    "Revoke it there, or pass --dedicated-server on a disposable lab-only "
                    "server. Nothing was changed.",
                )
            if dedicated_server:
                others = sorted(
                    connection.execute(
                        text(
                            "select datname from pg_database where not datistemplate "
                            "and datname <> :d"
                        ),
                        {"d": database},
                    ).scalars()
                )
                unexpected = [name for name in others if name not in _SERVER_DATABASES]
                if unexpected:
                    raise LabError(
                        "LAB_DATABASE_SERVER_NOT_DEDICATED",
                        f"Server also holds {unexpected}; refusing server-wide changes. "
                        "Nothing was changed.",
                    )
            exists = connection.execute(
                text("select 1 from pg_roles where rolname=:r"), {"r": role}
            ).first()
            statement = "ALTER ROLE" if exists else "CREATE ROLE"
            # DDL cannot take bind parameters; the password matched a strict URL-safe pattern.
            connection.execute(
                text(
                    f"{statement} \"{role}\" LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT CONNECTION LIMIT 8"
                )
            )
            if not connection.execute(
                text("select 1 from pg_database where datname=:d"), {"d": database}
            ).first():
                connection.execute(text(f'CREATE DATABASE "{database}" TEMPLATE template0'))
            connection.execute(text(f'REVOKE ALL ON DATABASE "{database}" FROM PUBLIC'))
            for name in exposed:  # dedicated server only (preflight above)
                connection.execute(text(f'REVOKE CONNECT ON DATABASE "{name}" FROM PUBLIC'))
                revoked.append(name)
            connection.execute(text(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"'))
            connection.execute(text(f"ALTER ROLE \"{role}\" SET statement_timeout = '30s'"))
            connection.execute(text(f"ALTER ROLE \"{role}\" SET lock_timeout = '5s'"))
            connection.execute(text(f'ALTER ROLE "{role}" SET search_path = lab, public'))
        lab_engine = create_engine(admin.set(database=database), isolation_level="AUTOCOMMIT")
        try:
            with lab_engine.connect() as connection:
                connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                connection.execute(text("REVOKE CREATE ON SCHEMA public FROM PUBLIC"))
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS lab AUTHORIZATION "{role}"'))
                connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        finally:
            lab_engine.dispose()
        with engine.connect() as connection:
            reachable = sorted(
                connection.execute(
                    text(
                        "select datname from pg_database where not datistemplate "
                        "and has_database_privilege(:r, datname, 'CONNECT')"
                    ),
                    {"r": role},
                ).scalars()
            )
        if reachable != [database]:
            raise LabError("LAB_DATABASE_ROLE_NOT_CONFINED", f"Role can CONNECT to {reachable}")
    finally:
        engine.dispose()
    url = admin.set(username=role, password=password, database=database).render_as_string(
        hide_password=False
    )
    return LabProvision(str(url), tuple(revoked))


def role_privileges(lab_url: str) -> dict[str, Any]:
    """Introspect what the lab role can actually do (used by tests and the review bundle)."""
    engine = create_engine(lab_url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "select rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
                    "rolconnlimit from pg_roles where rolname = current_user"
                )
            ).one()
            databases = [
                name
                for (name,) in connection.execute(
                    text(
                        "select datname from pg_database where not datistemplate "
                        "and has_database_privilege(datname, 'CONNECT')"
                    )
                )
            ]
            public_create = connection.execute(
                text("select has_schema_privilege('public', 'CREATE')")
            ).scalar()
            lab_create = connection.execute(
                text("select has_schema_privilege('lab', 'CREATE')")
            ).scalar()
            settings = dict(
                connection.execute(
                    text(
                        "select name, setting from pg_settings "
                        "where name in ('statement_timeout', 'lock_timeout')"
                    )
                ).all()
            )
        return {
            "superuser": row[0],
            "createdb": row[1],
            "createrole": row[2],
            "replication": row[3],
            "bypassrls": row[4],
            "connection_limit": row[5],
            "connectable_databases": sorted(databases),
            "public_schema_create": public_create,
            "lab_schema_create": lab_create,
            "timeouts": settings,
        }
    finally:
        engine.dispose()

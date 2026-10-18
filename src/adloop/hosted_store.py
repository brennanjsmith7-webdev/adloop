"""Durable hosted state in an isolated PostgreSQL schema; no import-time I/O."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

from adloop.safety.preview import ChangePlan, set_plan_store
from adloop.safety.audit import set_audit_sink


class HostedStoreError(RuntimeError):
    """State unavailable: stop rather than falling back to local state."""


class PlanBusyError(HostedStoreError):
    """Another instance is confirming this plan."""


class PlanAlreadyAttemptedError(HostedStoreError):
    """A provider mutation may already have occurred; never retry blindly."""


def _connect(url: str):
    import psycopg
    return psycopg.connect(url, connect_timeout=10, options="-c statement_timeout=15000")


class PostgresState:
    def __init__(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            parsed.port
            sslmode = parse_qs(parsed.query).get("sslmode", [])
            valid = (parsed.scheme in ("postgres", "postgresql") and parsed.hostname
                     and parsed.username and parsed.password and parsed.path not in ("", "/")
                     and not parsed.fragment and sslmode in (["require"], ["verify-full"]))
        except ValueError:
            valid = False
        if not valid:
            raise HostedStoreError("A private PostgreSQL URL with required TLS is needed")
        self._url = url

    @contextmanager
    def connection(self):
        try:
            with _connect(self._url) as connection:
                yield connection
        except HostedStoreError:
            raise
        except Exception:
            raise HostedStoreError("Hosted state operation failed") from None

    def query(self, sql: str, params: tuple = (), *, fetch: bool = False):
        with self.connection() as connection:
            cursor = connection.execute(sql, params)
            return cursor.fetchone() if fetch else None


def _validate_identity(tenant: str, plan_id: str = "unused") -> None:
    if not tenant or not plan_id:
        raise HostedStoreError("Tenant and plan identity are required")


class PostgresPlanStore:
    def __init__(self, state: PostgresState) -> None:
        self.state = state

    def store(self, tenant: str, plan: ChangePlan) -> None:
        _validate_identity(tenant, plan.plan_id)
        self.state.query(
            "INSERT INTO adloop.plans (tenant,plan_id,data,expires_at) "
            "VALUES (%s,%s,%s::jsonb,now()+interval '24 hours') "
            "ON CONFLICT (tenant,plan_id) DO UPDATE SET data=EXCLUDED.data,expires_at=EXCLUDED.expires_at",
            (tenant, plan.plan_id, json.dumps(asdict(plan))),
        )

    def get(self, tenant: str, plan_id: str) -> ChangePlan | None:
        _validate_identity(tenant, plan_id)
        row = self.state.query(
            "SELECT data FROM adloop.plans WHERE tenant=%s AND plan_id=%s AND expires_at>now()",
            (tenant, plan_id), fetch=True,
        )
        if row is None:
            return None
        try:
            return ChangePlan(**row[0])
        except (TypeError, ValueError):
            raise HostedStoreError("Stored plan is invalid") from None

    def remove(self, tenant: str, plan_id: str) -> None:
        _validate_identity(tenant, plan_id)
        self.state.query("DELETE FROM adloop.plans WHERE tenant=%s AND plan_id=%s", (tenant, plan_id))

    def claim_execution(self, tenant: str, plan_id: str) -> None:
        """Commit an irreversible attempt fence BEFORE contacting the provider.

        An expired fence can be reclaimed atomically, but a live fence is never
        cleared, including on timeout or subsequent audit/plan deletion failures.
        """
        _validate_identity(tenant, plan_id)
        row = self.state.query(
            "INSERT INTO adloop.execution_attempts (tenant,plan_id,expires_at) "
            "VALUES (%s,%s,now()+interval '7 days') "
            "ON CONFLICT (tenant,plan_id) DO UPDATE SET expires_at=EXCLUDED.expires_at "
            "WHERE adloop.execution_attempts.expires_at<=now() RETURNING plan_id",
            (tenant, plan_id), fetch=True,
        )
        if row is None:
            raise PlanAlreadyAttemptedError(
                "This plan has already attempted a real write. Do not retry it; "
                "verify the upstream account state before creating a new plan."
            )

    begin_execution = claim_execution

    @contextmanager
    def execution_lock(self, tenant: str, plan_id: str) -> Iterator[None]:
        """Hold a transaction advisory lock across the entire confirmation flow.

        Separate state-operation connections remain free to read/update plans.
        Connection close/rollback releases this lock even after worker failure.
        """
        _validate_identity(tenant, plan_id)
        material = json.dumps([tenant, plan_id], separators=(",", ":")).encode()
        key = int.from_bytes(hashlib.sha256(material).digest()[:8], "big", signed=True)
        with self.state.connection() as connection:
            row = connection.execute("SELECT pg_try_advisory_xact_lock(%s)", (key,)).fetchone()
            if not row or not row[0]:
                raise PlanBusyError("This plan is already being confirmed")
            yield


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if any(word in str(k).lower() for word in
                ("token", "secret", "password", "authorization", "credential", "api_key")) else _redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class PostgresAuditSink:
    def __init__(self, state: PostgresState) -> None:
        self.state = state

    def record(self, entry: dict[str, Any], *, log_file: str) -> None:
        from adloop.runtime import current_tenant
        tenant = current_tenant()
        _validate_identity(tenant)
        record = _redact({**entry, "tenant": tenant, "actor": tenant})
        if record.get("error"):
            record["error"] = "Upstream operation failed; private details omitted"
        self.state.query("INSERT INTO adloop.audit (tenant,record) VALUES (%s,%s::jsonb)",
                         (tenant, json.dumps(record)))


def _configure_postgres_store() -> PostgresPlanStore:
    """Verify access without DDL, then install durable stores; never fall back."""
    state = PostgresState(os.environ.get("ADLOOP_DATABASE_URL", ""))
    with state.connection() as connection:
        if connection.execute("SELECT 1").fetchone() != (1,):
            raise HostedStoreError("Hosted state health check failed")
        for table, columns, privileges in (
            ("plans", "tenant,plan_id,data,expires_at", "SELECT,INSERT,UPDATE,DELETE"),
            ("audit", "id,tenant,record,created_at", "SELECT,INSERT"),
            ("execution_attempts", "tenant,plan_id,expires_at", "SELECT,INSERT,UPDATE"),
        ):
            connection.execute(f"SELECT {columns} FROM adloop.{table} LIMIT 0")
            for privilege in privileges.split(","):
                row = connection.execute("SELECT has_table_privilege(current_user,%s,%s)",
                                         (f"adloop.{table}", privilege)).fetchone()
                if not row or not row[0]:
                    raise HostedStoreError("Hosted state privileges are insufficient")
    store = PostgresPlanStore(state)
    set_plan_store(store)
    set_audit_sink(PostgresAuditSink(state))
    return store


def configure_store():
    """Select one explicit backend. Configuration errors never trigger fallback."""
    backend = os.environ.get("ADLOOP_STATE_BACKEND", "postgres")
    if backend == "postgres":
        return _configure_postgres_store()
    if backend == "redis":
        from adloop.hosted_redis_store import configure_redis_store
        return configure_redis_store()
    raise HostedStoreError("ADLOOP_STATE_BACKEND must be postgres or redis")


def _durable_store():
    from adloop.safety.preview import get_plan_store
    from adloop.hosted_redis_store import RedisPlanStore
    store = get_plan_store()
    if not isinstance(store, (PostgresPlanStore, RedisPlanStore)):
        raise HostedStoreError("Hosted durable plan store is not configured")
    return store


@contextmanager
def execution_lock(tenant: str, plan_id: str) -> Iterator[None]:
    from adloop.runtime import deployment_mode
    if deployment_mode() == "server":
        with _durable_store().execution_lock(tenant, plan_id):
            yield
    else:
        yield


def claim_execution(tenant: str, plan_id: str) -> None:
    _durable_store().claim_execution(tenant, plan_id)


begin_execution = claim_execution

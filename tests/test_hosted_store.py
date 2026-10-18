"""Hosted PostgreSQL contracts using a simulated psycopg connection."""
import json
import pytest
from adloop import hosted_store as hs
from adloop.safety.preview import ChangePlan

URL = "postgresql://adloop_app:private-pass@pool.example/db?sslmode=require"


class Cursor:
    def __init__(self, row=None):
        self.row = row
    def fetchone(self):
        return self.row


@pytest.fixture
def db(monkeypatch):
    plans, attempts, audits, locks, queries = {}, set(), [], set(), []
    class Connection:
        def __init__(self):
            self.owned = []
        def __enter__(self):
            return self
        def __exit__(self, *args):
            for lock in self.owned:
                locks.remove(lock)
        def execute(self, sql, params=()):
            queries.append((sql, params))
            if "pg_try_advisory" in sql:
                if params[0] in locks:
                    return Cursor((False,))
                locks.add(params[0]); self.owned.append(params[0])
                return Cursor((True,))
            if sql.startswith("INSERT INTO adloop.plans"):
                plans[params[:2]] = json.loads(params[2])
            elif sql.startswith("SELECT data"):
                data = plans.get(params)
                return Cursor(None if data is None else (data,))
            elif sql.startswith("DELETE FROM adloop.plans"):
                plans.pop(params, None)
            elif sql.startswith("INSERT INTO adloop.execution_attempts"):
                if params in attempts:
                    return Cursor()
                attempts.add(params)
                return Cursor((params[1],))
            elif sql.startswith("INSERT INTO adloop.audit"):
                audits.append((params[0], json.loads(params[1])))
            elif sql == "SELECT 1":
                return Cursor((1,))
            elif "has_table_privilege" in sql:
                return Cursor((True,))
            return Cursor()
    monkeypatch.setattr(hs, "_connect", lambda url: Connection())
    return hs.PostgresState(URL), plans, attempts, audits, queries


def test_plan_upsert_tenant_scope_and_expiration(db):
    state, plans, _, _, queries = db
    store = hs.PostgresPlanStore(state)
    plan = ChangePlan(plan_id="p")
    store.store("alice", plan)
    assert store.get("bob", "p") is None
    result = store.get("alice", "p")
    assert result == plan
    result.dry_run_result = {"status": "DRY_RUN_SUCCESS"}
    store.store("alice", result)
    assert store.get("alice", "p").dry_run_result == result.dry_run_result
    store.store("bob", plan)
    store.remove("alice", "p")
    assert store.get("alice", "p") is None
    assert store.get("bob", "p") == plan
    assert any("interval '24 hours'" in sql and "ON CONFLICT" in sql for sql, _ in queries)
    assert any("expires_at>now()" in sql for sql, _ in queries)


def test_concurrent_confirmation_lock_and_release(db):
    state, *_ = db
    first, second = hs.PostgresPlanStore(state), hs.PostgresPlanStore(state)
    with first.execution_lock("alice", "p"):
        with pytest.raises(hs.PlanBusyError):
            with second.execution_lock("alice", "p"):
                pytest.fail("concurrent confirmation")
        with second.execution_lock("bob", "p"):
            pass
    with pytest.raises(hs.HostedStoreError):
        with second.execution_lock("alice", "p"):
            raise ValueError("private provider detail")
    with second.execution_lock("alice", "p"):
        pass


def test_audit_sanitized_and_runtime_identity_wins(db, monkeypatch):
    state, _, _, audits, _ = db
    monkeypatch.setattr("adloop.runtime.current_tenant", lambda: "alice")
    hs.PostgresAuditSink(state).record({"tenant": "forged", "actor": "forged", "error": "private-pass",
                                      "changes": {"refresh_token": "secret", "budget": 5}},
                                     log_file="/private/path")
    tenant, record = audits[0]
    assert tenant == record["tenant"] == record["actor"] == "alice"
    assert record["changes"]["budget"] == 5
    assert "private-pass" not in json.dumps(record)
    assert record["changes"]["refresh_token"] == "[REDACTED]"
    assert "/private/path" not in json.dumps(record)


@pytest.mark.parametrize("url", ["", "http://host", "postgresql://u:p@host/db",
    "postgresql://u:p@host/db?sslmode=disable", "postgresql://u:p@host/db?sslmode=require&sslmode=disable"])
def test_tls_configuration_fails_closed(url):
    with pytest.raises(hs.HostedStoreError):
        hs.PostgresState(url)


def test_config_healthcheck_and_sanitized_failure(db, monkeypatch):
    monkeypatch.setenv("ADLOOP_DATABASE_URL", URL)
    installed = []
    monkeypatch.setattr(hs, "set_plan_store", installed.append)
    monkeypatch.setattr(hs, "set_audit_sink", installed.append)
    assert isinstance(hs.configure_store(), hs.PostgresPlanStore)
    assert len(installed) == 2
    installed.clear()
    def broken(url):
        raise RuntimeError("private-pass " + url)
    monkeypatch.setattr(hs, "_connect", broken)
    with pytest.raises(hs.HostedStoreError) as error:
        hs.configure_store()
    assert "private-pass" not in str(error.value)
    assert not installed


@pytest.mark.parametrize("failure", ["upstream", "audit", "remove"])
def test_execution_fence_prevents_replay_after_ambiguous_failure(db, monkeypatch, failure):
    from adloop.ads import write
    from adloop.config import AdLoopConfig, SafetyConfig
    from adloop import runtime
    from adloop.safety import audit, preview
    state, _, attempts, _, queries = db
    store = hs.PostgresPlanStore(state)
    monkeypatch.setattr(preview, "_active_store", store)
    monkeypatch.setattr(runtime, "_deployment_mode", "server")
    config = AdLoopConfig(safety=SafetyConfig(require_dry_run=False, two_phase_apply=True))
    plan = ChangePlan(plan_id="p", dry_run_result={"status": "DRY_RUN_SUCCESS"})
    calls = []
    def mutate(*args):
        calls.append("Google")
        if failure == "upstream":
            raise TimeoutError("ambiguous")
        return {"resource_name": "ok"}
    def log(*args, **kwargs):
        if failure == "audit":
            raise hs.HostedStoreError("audit unavailable")
    def remove(*args):
        raise hs.HostedStoreError("delete unavailable")
    monkeypatch.setattr(write, "_execute_plan", mutate)
    monkeypatch.setattr(audit, "log_mutation", log)
    if failure == "remove":
        monkeypatch.setattr(preview, "remove_plan", remove)
    with runtime.use_runtime(config, tenant="alice"):
        store.store("alice", plan)
        if failure == "upstream":
            assert "error" in write.confirm_and_apply(config, plan_id="p", dry_run=False)
        else:
            with pytest.raises(hs.HostedStoreError):
                write.confirm_and_apply(config, plan_id="p", dry_run=False)
        with pytest.raises(hs.PlanAlreadyAttemptedError):
            write.confirm_and_apply(config, plan_id="p", dry_run=False)
    assert calls == ["Google"]
    assert ("alice", "p") in attempts
    assert any("interval '7 days'" in sql and "expires_at<=now()" in sql for sql, _ in queries)


def test_dry_run_and_validation_refusal_do_not_consume_fence(db, monkeypatch):
    from adloop.ads import write
    from adloop.config import AdLoopConfig, SafetyConfig
    from adloop import runtime
    from adloop.safety import audit, preview
    state, _, attempts, _, _ = db
    store = hs.PostgresPlanStore(state)
    monkeypatch.setattr(preview, "_active_store", store)
    monkeypatch.setattr(runtime, "_deployment_mode", "server")
    monkeypatch.setattr(audit, "log_mutation", lambda *a, **kw: None)
    config = AdLoopConfig(safety=SafetyConfig(require_dry_run=False, two_phase_apply=True))
    with runtime.use_runtime(config, tenant="alice"):
        store.store("alice", ChangePlan(plan_id="p"))
        assert write.confirm_and_apply(config, plan_id="p", dry_run=False)["status"] == "DRY_RUN_REQUIRED"
        assert write.confirm_and_apply(config, plan_id="p", dry_run=True)["status"] == "DRY_RUN_SUCCESS"
    assert not attempts


def test_explicit_backend_selection_no_fallback(monkeypatch):
    from adloop import hosted_redis_store as redis
    called = []
    monkeypatch.setattr(hs, "_configure_postgres_store", lambda: called.append("postgres"))
    monkeypatch.setattr(redis, "configure_redis_store", lambda: called.append("redis"))
    monkeypatch.delenv("ADLOOP_STATE_BACKEND", raising=False)
    hs.configure_store()
    monkeypatch.setenv("ADLOOP_STATE_BACKEND", "redis")
    hs.configure_store()
    assert called == ["postgres", "redis"]
    monkeypatch.setenv("ADLOOP_STATE_BACKEND", "unknown")
    with pytest.raises(hs.HostedStoreError):
        hs.configure_store()
    def broken():
        raise hs.HostedStoreError("unavailable")
    monkeypatch.setattr(redis, "configure_redis_store", broken)
    monkeypatch.setenv("ADLOOP_STATE_BACKEND", "redis")
    with pytest.raises(hs.HostedStoreError):
        hs.configure_store()
    assert called == ["postgres", "redis"]


def test_redis_durability_scope_fence_and_lock(monkeypatch):
    from adloop import hosted_redis_store as redis
    values, commands = {}, []
    class Response:
        def __init__(self, result):
            self.result = result
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return json.dumps({"result": self.result}).encode()
    def send(req, timeout):
        cmd, *args = json.loads(req.data)
        commands.append([cmd, *args])
        if cmd == "SET":
            key, value, *flags = args
            result = None if "NX" in flags and key in values else "OK"
            if result:
                values[key] = value
        elif cmd == "GET":
            result = values.get(args[0])
        elif cmd == "DEL":
            result = int(values.pop(args[0], None) is not None)
        elif cmd == "EVAL":
            _, _, key, token = args
            result = 0
            if values.get(key) == token:
                del values[key]
                result = 1
        else:
            raise AssertionError(cmd)
        return Response(result)
    monkeypatch.setattr(redis, "urlopen", send)
    client = redis.RedisREST("https://test.upstash.io", "private-test-token")
    first, second = redis.RedisPlanStore(client), redis.RedisPlanStore(client)
    plan = ChangePlan(plan_id="p", dry_run_result={"status": "DRY_RUN_SUCCESS"})
    first.store("alice", plan)
    assert second.get("alice", "p") == plan
    assert second.get("bob", "p") is None
    with first.execution_lock("alice", "p"):
        with pytest.raises(hs.PlanBusyError):
            with second.execution_lock("alice", "p"):
                pytest.fail("duplicate")
        first.claim_execution("alice", "p")
    with second.execution_lock("alice", "p"):
        with pytest.raises(hs.PlanAlreadyAttemptedError):
            second.claim_execution("alice", "p")
    second.remove("alice", "p")
    with pytest.raises(hs.PlanAlreadyAttemptedError):
        first.claim_execution("alice", "p")
    assert any(c[-3:] == ["NX", "EX", 604800] for c in commands)
    assert any(c[-2:] == ["EX", 86400] for c in commands)

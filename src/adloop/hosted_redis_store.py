"""Optional Upstash REST durable storage; selected explicitly, never a fallback."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from contextlib import contextmanager
from dataclasses import asdict
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from adloop.hosted_store import HostedStoreError, PlanBusyError, PlanAlreadyAttemptedError, _redact
from adloop.safety.preview import ChangePlan, set_plan_store
from adloop.safety.audit import set_audit_sink


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


urlopen = build_opener(_NoRedirect()).open
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


class RedisREST:
    def __init__(self, url: str, token: str):
        try:
            p = urlsplit(url)
            p.port
            valid = (p.scheme == "https" and p.hostname and not p.username and not p.password
                     and not p.query and not p.fragment and p.path in ("", "/")
                     and token and not any(c.isspace() for c in token))
        except ValueError:
            valid = False
        if not valid:
            raise HostedStoreError("Valid HTTPS Redis endpoint and private token required")
        self.url, self._token = url.rstrip("/"), token

    def command(self, *args):
        req = Request(self.url, data=json.dumps(args).encode(), method="POST",
                      headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=15) as response:
                payload = json.loads(response.read())
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise HostedStoreError("Hosted state request failed") from None
        if not isinstance(payload, dict) or "error" in payload or "result" not in payload:
            raise HostedStoreError("Hosted state returned an invalid response")
        return payload["result"]


def _key(kind, tenant, identifier=""):
    if not tenant:
        raise HostedStoreError("Tenant is required for hosted state")
    scope = hashlib.sha256(tenant.encode()).hexdigest()
    suffix = hashlib.sha256(identifier.encode()).hexdigest() if identifier else "records"
    return f"adloop:v1:{scope}:{kind}:{suffix}"


class RedisPlanStore:
    def __init__(self, client):
        self.client = client

    def store(self, tenant, plan):
        if self.client.command("SET", _key("plan", tenant, plan.plan_id), json.dumps(asdict(plan)),
                               "EX", 86400) != "OK":
            raise HostedStoreError("Plan could not be persisted")

    def get(self, tenant, plan_id):
        result = self.client.command("GET", _key("plan", tenant, plan_id))
        if result is None:
            return None
        try:
            return ChangePlan(**json.loads(result))
        except (TypeError, ValueError):
            raise HostedStoreError("Stored plan is invalid") from None

    def remove(self, tenant, plan_id):
        self.client.command("DEL", _key("plan", tenant, plan_id))

    def claim_execution(self, tenant, plan_id):
        if self.client.command("SET", _key("execution", tenant, plan_id), "attempted",
                               "NX", "EX", 604800) != "OK":
            raise PlanAlreadyAttemptedError(
                "This plan has already attempted a real write. Do not retry it; "
                "verify the upstream account state before creating a new plan.")

    begin_execution = claim_execution

    @contextmanager
    def execution_lock(self, tenant, plan_id):
        # Hosted request maximum duration MUST remain below this 900-second lease.
        key, token = _key("lock", tenant, plan_id), secrets.token_hex(32)
        if self.client.command("SET", key, token, "NX", "EX", 900) != "OK":
            raise PlanBusyError("This plan is already being confirmed")
        try:
            yield
        finally:
            self.client.command("EVAL", _RELEASE, 1, key, token)


class RedisAuditSink:
    def __init__(self, client):
        self.client = client

    def record(self, entry, *, log_file):
        from adloop.runtime import current_tenant
        tenant = current_tenant()
        record = _redact({**entry, "tenant": tenant, "actor": tenant})
        if record.get("error"):
            record["error"] = "Upstream operation failed; private details omitted"
        result = self.client.command("RPUSH", _key("audit", tenant), json.dumps(record))
        if not isinstance(result, int) or result < 1:
            raise HostedStoreError("Audit record could not be persisted")


def configure_redis_store():
    client = RedisREST(os.environ.get("UPSTASH_REDIS_REST_URL") or os.environ.get("KV_REST_API_URL", ""),
                       os.environ.get("UPSTASH_REDIS_REST_TOKEN") or os.environ.get("KV_REST_API_TOKEN", ""))
    if client.command("PING") != "PONG":
        raise HostedStoreError("Hosted state health check failed")
    store = RedisPlanStore(client)
    set_plan_store(store)
    set_audit_sink(RedisAuditSink(client))
    return store

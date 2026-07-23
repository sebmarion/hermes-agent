"""Tests for the Chronos cron-fire webhook (POST /api/cron/fire) — Phase 4E.2.

The webhook authenticates a NAS-minted JWT via the pluggable fire-verifier
(NOT API_SERVER_KEY), then runs the job via the resolved provider's fire_due in
the background, returning 202. These tests monkeypatch the verifier and
resolve_cron_scheduler — the verifier itself is tested with real crypto in
test_chronos_verify.py.
"""

import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware

_MOD = "gateway.platforms.api_server"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-secret"}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/cron/fire", adapter._handle_cron_fire)
    return app


@pytest.fixture
def adapter():
    return _make_adapter()


class _SpyProvider:
    """Records fire_due calls; stands in for the resolved provider."""

    def __init__(self):
        self.fired = []
        self.leases = []

    def fire_due(
        self,
        job_id,
        *,
        adapters=None,
        loop=None,
        admission_lease=None,
    ):
        self.fired.append(job_id)
        self.leases.append(admission_lease)
        return True


@pytest.mark.asyncio
async def test_valid_token_accepts_and_fires(adapter, monkeypatch):
    """Valid NAS-JWT + {job_id} → 202 and fire_due invoked with that id."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    # verifier returns claims (valid token)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire", "aud": "agent:x"}),
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer good"},
                              json={"job_id": "abc123"})
        assert resp.status == 202
        data = await resp.json()
        assert data["job_id"] == "abc123"

    # fire runs in a background thread/task — give it a beat to land.
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == ["abc123"]
    assert len(spy.leases) == 1
    assert spy.leases[0] is not None


@pytest.mark.asyncio
async def test_accepted_fire_is_tracked_until_worker_finishes(adapter, monkeypatch):
    """A 202 response must not make finite cron work disappear from drain."""
    started = threading.Event()
    release = threading.Event()

    class _BlockingProvider:
        def fire_due(
            self,
            job_id,
            *,
            adapters=None,
            loop=None,
            admission_lease=None,
        ):
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release cron fire")
            return True

    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: _BlockingProvider(),
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire", "aud": "agent:x"}),
    )

    try:
        request = make_mocked_request(
            "POST",
            "/api/cron/fire",
            headers={"Authorization": "Bearer good"},
        )

        async def _json():
            return {"job_id": "abc123"}

        request._payload = None
        request.json = _json
        resp = await adapter._handle_cron_fire(request)
        assert resp.status == 202
        assert await asyncio.to_thread(started.wait, 2)
        from cron.admission import cron_admission_snapshot

        receipt = cron_admission_snapshot()
        assert receipt["active_count"] == 1
        assert receipt["active_job_ids"] == ["abc123"]
        assert len(adapter._drain_tracked_tasks) == 1
        assert adapter._readiness_work_counts()["api_background_tasks"] == 1
    finally:
        release.set()

    for _ in range(100):
        if not adapter._drain_tracked_tasks:
            break
        await asyncio.sleep(0.01)
    assert adapter._drain_tracked_tasks == set()
    assert adapter._readiness_work_counts()["api_background_tasks"] == 0
    assert cron_admission_snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_handler_waits_for_durable_admission_before_202(adapter, monkeypatch):
    import cron.scheduler as sched

    entered = threading.Event()
    allow = threading.Event()
    real_claim = sched._claim_cron_dispatch
    spy = _SpyProvider()

    def blocked_claim(job_id, **kwargs):
        entered.set()
        assert allow.wait(timeout=5)
        return real_claim(job_id, **kwargs)

    monkeypatch.setattr(sched, "_claim_cron_dispatch", blocked_claim)
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: spy,
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    request = make_mocked_request(
        "POST",
        "/api/cron/fire",
        headers={"Authorization": "Bearer good"},
    )

    async def _json():
        return {"job_id": "pre-202"}

    request.json = _json
    handler = asyncio.create_task(adapter._handle_cron_fire(request))
    assert await asyncio.to_thread(entered.wait, 2)
    assert handler.done() is False
    allow.set()
    response = await handler
    assert response.status == 202

    for _ in range(100):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == ["pre-202"]


@pytest.mark.asyncio
async def test_closed_admission_returns_503_without_provider_fire(
    adapter,
    monkeypatch,
):
    spy = _SpyProvider()
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: spy,
    )
    monkeypatch.setattr(
        "cron.scheduler._claim_cron_dispatch",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    request = make_mocked_request(
        "POST",
        "/api/cron/fire",
        headers={"Authorization": "Bearer good"},
    )

    async def _json():
        return {"job_id": "blocked"}

    request.json = _json
    response = await adapter._handle_cron_fire(request)

    assert response.status == 503
    assert spy.fired == []


@pytest.mark.asyncio
async def test_cancelling_async_wrapper_keeps_lease_until_thread_finishes(
    adapter,
    monkeypatch,
):
    from cron.admission import cron_admission_snapshot

    started = threading.Event()
    release = threading.Event()

    class _BlockingProvider:
        def fire_due(self, job_id, **kwargs):
            started.set()
            assert release.wait(timeout=5)
            return True

    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: _BlockingProvider(),
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    request = make_mocked_request(
        "POST",
        "/api/cron/fire",
        headers={"Authorization": "Bearer good"},
    )

    async def _json():
        return {"job_id": "cancelled-wrapper"}

    request.json = _json
    response = await adapter._handle_cron_fire(request)
    assert response.status == 202
    assert await asyncio.to_thread(started.wait, 2)
    task = next(iter(adapter._drain_tracked_tasks))

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert cron_admission_snapshot()["active_job_ids"] == ["cancelled-wrapper"]

    release.set()
    for _ in range(200):
        if cron_admission_snapshot()["active_count"] == 0:
            break
        await asyncio.sleep(0.01)
    assert cron_admission_snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_task_creation_failure_releases_preaccepted_lease(
    adapter,
    monkeypatch,
):
    from cron.admission import cron_admission_snapshot

    spy = _SpyProvider()
    monkeypatch.setattr(
        "cron.scheduler_provider.resolve_cron_scheduler",
        lambda: spy,
    )
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )
    monkeypatch.setattr(
        asyncio,
        "create_task",
        lambda _coro: (_ for _ in ()).throw(
            RuntimeError("injected task creation failure")
        ),
    )
    request = make_mocked_request(
        "POST",
        "/api/cron/fire",
        headers={"Authorization": "Bearer good"},
    )

    async def _json():
        return {"job_id": "task-create-failure"}

    request.json = _json
    with pytest.raises(RuntimeError, match="task creation failure"):
        await adapter._handle_cron_fire(request)

    assert spy.fired == []
    assert cron_admission_snapshot()["active_count"] == 0


@pytest.mark.asyncio
async def test_invalid_token_401_and_no_fire(adapter, monkeypatch):
    """Bad/forged token → 401, fire_due NOT invoked."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: None),  # verification fails
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer forged"},
                              json={"job_id": "abc123"})
        assert resp.status == 401

    await asyncio.sleep(0.05)
    assert spy.fired == []


@pytest.mark.asyncio
async def test_missing_token_401(adapter, monkeypatch):
    """No Authorization header → verifier gets empty token → 401."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    # Real verifier: empty token returns None.
    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire", json={"job_id": "abc123"})
        assert resp.status == 401
    assert spy.fired == []


@pytest.mark.asyncio
async def test_missing_job_id_400(adapter, monkeypatch):
    """Valid token but no job_id → 400, no fire."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer good"},
                              json={})
        assert resp.status == 400
    assert spy.fired == []


@pytest.mark.asyncio
async def test_fire_does_not_require_api_server_key(adapter, monkeypatch):
    """The fire endpoint must NOT gate on API_SERVER_KEY — auth is the NAS-JWT.
    A request with NO API key header but a valid fire token still succeeds."""
    spy = _SpyProvider()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: spy)
    monkeypatch.setattr(
        "plugins.cron_providers.chronos.verify.get_fire_verifier",
        lambda: (lambda **kw: {"purpose": "cron_fire"}),
    )

    app = _create_app(adapter)
    async with TestClient(TestServer(app)) as cli:
        # Bearer is the FIRE token, not the API_SERVER_KEY "sk-secret".
        resp = await cli.post("/api/cron/fire",
                              headers={"Authorization": "Bearer nas-jwt"},
                              json={"job_id": "j9"})
        assert resp.status == 202
    for _ in range(50):
        if spy.fired:
            break
        await asyncio.sleep(0.01)
    assert spy.fired == ["j9"]

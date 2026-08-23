"""Web 应用装配：认证、失败限流、安全头、错误响应、状态接口。

对应设计：docs/design/DD_WEB.md §5、§7。

用 httpx 的 ASGITransport 直接打应用，不起真实端口：中间件、依赖、错误处理器
全部照常执行，而不必为每个用例等一次 TCP 监听就绪。真实端口的端到端只在
tests/test_m4_acceptance.py 里留少量用例。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query

from r_proxy.app import Application
from r_proxy.web.app import create_app
from r_proxy.web.deps import AuthThrottle, _matches

TOKEN = "s3cret-token-value-long-enough"


def write_config(tmp_path: Path, *, token: str | None = None) -> Path:
    token_line = f'auth_token = "{token}"\n' if token is not None else ""
    text = (
        '[listen]\nhost = "127.0.0.1"\nport = 0\n'
        f"[webui]\nenabled = false\n{token_line}"
        "[database]\n"
        f'state_path = "{tmp_path / "state.db"}"\n'
        f'logs_path = "{tmp_path / "logs.db"}"\n'
        '\n[[upstreams]]\nname = "direct"\ntype = "direct"\n'
    )
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


async def running(tmp_path: Path, *, token: str | None = None) -> Application:
    """真实启动的 Application：状态与存储都是真的，只是不起 uvicorn。"""
    app = Application(config_path=write_config(tmp_path, token=token))
    await app.start()
    return app


def client_for(web: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web, client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    )


@pytest.fixture
async def guarded(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """配置了 token 的应用。"""
    app = await running(tmp_path, token=TOKEN)
    async with client_for(create_app(app)) as client:
        yield client
    await app.stop()


@pytest.fixture
async def open_access(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    """未配置 token 的应用（仅回环绑定允许）。"""
    app = await running(tmp_path)
    async with client_for(create_app(app)) as client:
        yield client
    await app.stop()


class TestHealthz:
    async def test_healthz_needs_no_token(self, guarded: httpx.AsyncClient) -> None:
        response = await guarded.get("/api/healthz")
        assert response.status_code == 200
        assert response.text == "ok"

    async def test_healthz_reveals_nothing(self, guarded: httpx.AsyncClient) -> None:
        """存活探针是唯一免认证的端点，不能借它读出任何内部信息。"""
        body = (await guarded.get("/api/healthz")).text
        assert body == "ok"


class TestAuthentication:
    async def test_missing_token_is_rejected(self, guarded: httpx.AsyncClient) -> None:
        response = await guarded.get("/api/status")
        assert response.status_code == 401
        assert response.json() == {"error": {"code": "UNAUTHORIZED", "message": "认证失败"}}

    async def test_a_wrong_token_looks_exactly_like_a_missing_one(
        self, guarded: httpx.AsyncClient
    ) -> None:
        """两者的响应必须逐字节相同，否则探测者能借差异确认「token 存在」。"""
        missing = await guarded.get("/api/status")
        wrong = await guarded.get("/api/status", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == missing.status_code
        assert wrong.json() == missing.json()

    async def test_bearer_token_is_accepted(self, guarded: httpx.AsyncClient) -> None:
        response = await guarded.get("/api/status", headers={"Authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200

    async def test_x_auth_token_header_is_accepted(self, guarded: httpx.AsyncClient) -> None:
        response = await guarded.get("/api/status", headers={"X-Auth-Token": TOKEN})
        assert response.status_code == 200

    async def test_token_in_the_query_string_is_not_accepted(
        self, guarded: httpx.AsyncClient
    ) -> None:
        """URL 会进入浏览器历史、Referer 头与反向代理日志，留存远超会话。"""
        response = await guarded.get(f"/api/status?token={TOKEN}")
        assert response.status_code == 401

    def test_comparing_a_non_ascii_token_does_not_raise(self) -> None:
        """``compare_digest`` 对 ``str`` 只接受纯 ASCII，必须按字节比较。

        非 ASCII 的 token 已在启动校验里被拒（``E_WEB_TOKEN_NON_ASCII``），
        这里是第二道：万一有一个绕过校验进来了，要返回 ``401`` 而不是抛
        ``TypeError`` 变成 ``500``。
        """
        assert _matches("令牌", "令牌") is True
        assert _matches("令牌", "other") is False

    async def test_no_configured_token_allows_access(self, open_access: httpx.AsyncClient) -> None:
        assert (await open_access.get("/api/status")).status_code == 200

    async def test_the_token_never_reaches_the_log(
        self, guarded: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            await guarded.get("/api/status", headers={"X-Auth-Token": "wrong-but-secret"})
        assert caplog.records
        for record in caplog.records:
            assert "wrong-but-secret" not in record.getMessage()


class TestAuthFailureLogging:
    """一次页面加载并发五个请求，五条一模一样的 WARNING 没有任何增量信息。"""

    async def test_a_burst_from_one_client_logs_once(
        self, guarded: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="r_proxy.web.deps"):
            for _ in range(5):
                await guarded.get("/api/status")
        assert len([r for r in caplog.records if "Web 认证失败" in r.getMessage()]) == 1

    async def test_the_suppressed_count_surfaces_when_the_client_is_locked_out(
        self, guarded: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """压制不等于丢弃：达到阈值时要把这一窗口的总次数说出来。"""
        with caplog.at_level(logging.WARNING, logger="r_proxy.web.deps"):
            for _ in range(12):
                await guarded.get("/api/status")
        assert any("连续认证失败" in r.getMessage() for r in caplog.records)


class TestAuthThrottle:
    def test_failures_below_the_threshold_do_not_block(self) -> None:
        throttle = AuthThrottle(max_failures=3, window=60.0, clock=lambda: 100.0)
        for _ in range(2):
            throttle.record_failure("10.0.0.1")
        assert throttle.blocked("10.0.0.1") is False

    def test_reaching_the_threshold_blocks(self) -> None:
        throttle = AuthThrottle(max_failures=3, window=60.0, clock=lambda: 100.0)
        for _ in range(3):
            throttle.record_failure("10.0.0.1")
        assert throttle.blocked("10.0.0.1") is True

    def test_blocking_is_per_client(self) -> None:
        throttle = AuthThrottle(max_failures=2, window=60.0, clock=lambda: 100.0)
        for _ in range(2):
            throttle.record_failure("10.0.0.1")
        assert throttle.blocked("10.0.0.2") is False

    def test_failures_expire_with_the_window(self) -> None:
        now = 100.0
        throttle = AuthThrottle(max_failures=2, window=60.0, clock=lambda: now)
        for _ in range(2):
            throttle.record_failure("10.0.0.1")
        now += 61.0
        assert throttle.blocked("10.0.0.1") is False

    def test_tracked_clients_are_bounded(self) -> None:
        """可增长资源必须有界：伪造源地址的请求不该把内存撑满。"""
        throttle = AuthThrottle(max_failures=1, capacity=4, clock=lambda: 100.0)
        for index in range(20):
            throttle.record_failure(f"10.0.0.{index}")
        assert throttle.blocked("10.0.0.19") is True
        assert throttle.blocked("10.0.0.0") is False


class TestThrottleIntegration:
    async def test_too_many_failures_yield_429(self, guarded: httpx.AsyncClient) -> None:
        for _ in range(10):
            attempt = await guarded.get("/api/status", headers={"X-Auth-Token": "x"})
            assert attempt.status_code == 401
        response = await guarded.get("/api/status", headers={"X-Auth-Token": "x"})
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_REQUESTS"

    async def test_a_blocked_client_is_refused_even_with_the_right_token(
        self, guarded: httpx.AsyncClient
    ) -> None:
        """超限后不再做 token 比较，否则爆破者只要撞对一次就翻盘。"""
        for _ in range(10):
            await guarded.get("/api/status", headers={"X-Auth-Token": "x"})
        response = await guarded.get("/api/status", headers={"X-Auth-Token": TOKEN})
        assert response.status_code == 429


class TestSecurityHeaders:
    async def test_csp_is_set(self, open_access: httpx.AsyncClient) -> None:
        headers = (await open_access.get("/api/status")).headers
        assert "script-src 'self'" in headers["content-security-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

    async def test_sniffing_and_referrer_are_disabled(self, open_access: httpx.AsyncClient) -> None:
        headers = (await open_access.get("/api/status")).headers
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"

    async def test_headers_are_present_on_errors_too(self, guarded: httpx.AsyncClient) -> None:
        """错误响应同样会被渲染，缺了头就等于给自己留了个例外。"""
        headers = (await guarded.get("/api/status")).headers
        assert "content-security-policy" in headers


class TestStatus:
    async def test_status_reports_the_running_proxy(self, open_access: httpx.AsyncClient) -> None:
        body = (await open_access.get("/api/status")).json()
        assert body["proxy"]["port"] > 0
        assert body["config_version"]
        assert body["uptime_seconds"] >= 0.0
        assert body["connections"]["active"] == 0

    async def test_no_attempts_yet_is_not_full_success(
        self, open_access: httpx.AsyncClient
    ) -> None:
        """「还没跑过」不是「全部成功」，成功率报 0。"""
        body = (await open_access.get("/api/status")).json()
        assert body["requests"]["attempts"] == 0
        assert body["requests"]["success_rate"] == 0.0

    async def test_status_exposes_storage_metrics(self, open_access: httpx.AsyncClient) -> None:
        storage = (await open_access.get("/api/status")).json()["storage"]
        assert storage["dropped_critical"] == 0
        assert storage["queue_capacity"] > 0
        assert storage["merge_ratio"] == 1.0


class TestErrorEnvelope:
    async def test_unhandled_exceptions_return_a_request_id_only(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        web = create_app(app)

        @web.get("/api/boom")
        async def boom() -> None:
            raise RuntimeError("内部细节：/etc/r-proxy/config.toml 打不开")

        transport = httpx.ASGITransport(app=web, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://webui.test") as client:
            response = await client.get("/api/boom")
        await app.stop()

        assert response.status_code == 500
        error = response.json()["error"]
        assert error == {
            "code": "INTERNAL_ERROR",
            "message": "服务内部错误",
            "request_id": error["request_id"],
        }
        assert "config.toml" not in response.text

    async def test_sqlite_operational_error_is_a_503_not_a_500(self, tmp_path: Path) -> None:
        """慢磁盘下的忙锁是「稍后重试」，不是「服务坏了」——新发现 4。"""
        app = await running(tmp_path)
        web = create_app(app)

        @web.get("/api/locked")
        async def locked() -> None:
            raise sqlite3.OperationalError("database is locked")

        async with client_for(web) as client:
            response = await client.get("/api/locked")
        await app.stop()

        assert response.status_code == 503
        error = response.json()["error"]
        assert error == {
            "code": "UNAVAILABLE",
            "message": "存储暂时繁忙，请稍后重试",
            "request_id": error["request_id"],
        }

    async def test_validation_errors_do_not_echo_the_input(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        web = create_app(app)

        @web.get("/api/paged")
        async def paged(size: int = Query(50, le=1000)) -> dict[str, int]:
            return {"size": size}

        async with client_for(web) as client:
            response = await client.get("/api/paged?size=100000")
        await app.stop()

        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "VALIDATION_ERROR"
        assert body["details"][0]["location"] == "query.size"
        assert "100000" not in response.text

    async def test_structured_details_survive(self, tmp_path: Path) -> None:
        """`409` 这类冲突要带上具体位置，前端才能直接指给用户。"""
        app = await running(tmp_path)
        web = create_app(app)

        @web.delete("/api/thing")
        async def thing() -> None:
            raise HTTPException(
                409,
                detail={
                    "code": "UPSTREAM_IN_USE",
                    "message": "仍被规则引用",
                    "details": [{"file": "user.rules", "line": 12}],
                },
            )

        async with client_for(web) as client:
            response = await client.delete("/api/thing")
        await app.stop()

        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "UPSTREAM_IN_USE",
            "message": "仍被规则引用",
            "details": [{"file": "user.rules", "line": 12}],
        }

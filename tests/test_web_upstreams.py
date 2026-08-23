"""出口列表与连通性测试。

对应设计：docs/design/DD_WEB.md §7.3、§7.4，验收点 M4-10。

连通性测试是 Web 界面里唯一会主动发起外部连接的功能，因此这里的用例分两组：
一组盯**输出**（凭据不出响应、响应体不回传），一组盯**输入**（地址只能来自配置，
请求体无法左右选址）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from r_proxy.app import Application
from r_proxy.config.model import RoutingConfig, UpstreamConfig
from r_proxy.contracts import FailureKind, RequestTarget
from r_proxy.egress.connector import ConnectorError, TunnelConn, UpstreamConn
from r_proxy.web import probe as probing
from r_proxy.web.app import create_app

CONFIG = """
[listen]
host = "127.0.0.1"
port = 0

[webui]
enabled = false

[database]
state_path = "{state}"
logs_path = "{logs}"

[[upstreams]]
name = "proxy-a"
type = "http"
address = "{address}"
priority = 10

[upstreams.auth]
username = "squid-user"
password = "s3cr3t-pass"

[[upstreams]]
name = "proxy-b"
type = "http"
address = "127.0.0.1:1"
priority = 20
enabled = false

[[upstreams]]
name = "direct"
type = "direct"
priority = 100
"""


async def running(tmp_path: Path, *, address: str = "127.0.0.1:1") -> Application:
    path = tmp_path / "config.toml"
    path.write_text(
        CONFIG.format(state=tmp_path / "state.db", logs=tmp_path / "logs.db", address=address),
        encoding="utf-8",
    )
    app = Application(config_path=path)
    await app.start()
    return app


def client_for(app: Application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app), client=("127.0.0.1", 12345)),
        base_url="http://webui.test",
    )


@pytest.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    app = await running(tmp_path)
    async with client_for(app) as c:
        yield c
    await app.stop()


class TestUpstreamList:
    async def test_upstreams_come_back_in_candidate_chain_order(
        self, client: httpx.AsyncClient
    ) -> None:
        body = (await client.get("/api/upstreams")).json()
        assert [u["name"] for u in body["upstreams"]] == ["proxy-a", "proxy-b", "direct"]

    async def test_credentials_are_reduced_to_a_boolean(self, client: httpx.AsyncClient) -> None:
        """M4-10：凭据只以 ``has_auth`` 体现。"""
        response = await client.get("/api/upstreams")
        body = response.json()
        by_name = {u["name"]: u for u in body["upstreams"]}
        assert by_name["proxy-a"]["has_auth"] is True
        assert by_name["direct"]["has_auth"] is False

    async def test_neither_username_nor_password_appears_anywhere(
        self, client: httpx.AsyncClient
    ) -> None:
        """在**原始响应文本**上断言：只查字段名会漏掉「凭据被塞进某个消息里」。"""
        raw = (await client.get("/api/upstreams")).text
        assert "s3cr3t-pass" not in raw
        assert "squid-user" not in raw
        assert "password" not in raw

    async def test_health_is_nested_and_reflects_the_circuit(self, tmp_path: Path) -> None:
        app = await running(tmp_path)
        try:
            for _ in range(5):
                app.state.health.record_result(
                    "proxy-a",
                    ok=False,
                    kind=FailureKind.UPSTREAM_ERROR,
                    now=time.monotonic(),
                    error="连不上",
                )
            async with client_for(app) as c:
                body = (await c.get("/api/upstreams")).json()
        finally:
            await app.stop()
        health = body["upstreams"][0]["health"]
        assert health["circuit_state"] == "open"
        assert health["available"] is False
        assert health["consecutive_failures"] == 5

    async def test_the_flat_health_endpoint_keeps_its_shape(
        self, client: httpx.AsyncClient
    ) -> None:
        """两个端点共用同一份投影，但线上形状不同：`/api/health` 保持扁平。"""
        item = (await client.get("/api/health")).json()["upstreams"][0]
        assert item["circuit_state"] == "closed"
        assert "health" not in item


class TestConnectivityProbe:
    async def test_an_unknown_upstream_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/upstreams/ghost/test")).status_code == 404

    async def test_a_submitted_address_cannot_redirect_the_probe(self, tmp_path: Path) -> None:
        """SSRF 边界：请求体里的地址一律无效，探测目标与出口都来自服务端。"""
        seen: list[tuple[str | None, str]] = []

        class Recording:
            async def connect_tunnel(
                self, upstream: UpstreamConfig, target: RequestTarget, routing: RoutingConfig
            ) -> TunnelConn:
                seen.append((upstream.address, target.authority))
                raise ConnectorError(FailureKind.UPSTREAM_ERROR, "ECONNREFUSED")

        app = await running(tmp_path, address="127.0.0.1:3128")
        original = probing.UpstreamConnector
        probing.UpstreamConnector = Recording  # type: ignore[misc, assignment]
        try:
            async with client_for(app) as c:
                response = await c.post(
                    "/api/upstreams/proxy-a/test",
                    json={"address": "10.0.0.1:22", "target": "169.254.169.254:80"},
                )
        finally:
            probing.UpstreamConnector = original  # type: ignore[misc]
            await app.stop()
        assert response.status_code == 200
        assert seen == [("127.0.0.1:3128", f"{probing.PROBE_HOST}:{probing.PROBE_PORT}")]

    async def test_the_timeout_is_fixed_and_ignores_the_configuration(self) -> None:
        """出口自己配的 ``connect_timeout`` 优先级更高，只改 routing 会被它盖掉。"""
        captured: list[tuple[float | None, float]] = []

        class Recording:
            async def connect_tunnel(
                self, upstream: UpstreamConfig, target: RequestTarget, routing: RoutingConfig
            ) -> TunnelConn:
                captured.append((upstream.connect_timeout, routing.connect_timeout))
                raise ConnectorError(FailureKind.UPSTREAM_ERROR, "ETIMEDOUT")

        cfg = UpstreamConfig(name="slow", type="http", address="127.0.0.1:1", connect_timeout=30.0)
        await probing.probe(cfg, connector=Recording())  # type: ignore[arg-type]
        assert captured == [(probing.PROBE_TIMEOUT_S, probing.PROBE_TIMEOUT_S)]

    async def test_a_refused_connection_is_a_result_not_an_error(
        self, client: httpx.AsyncClient
    ) -> None:
        """探测失败是正常结果。让它冒成 500 会把「出口挂了」显示成「界面挂了」。"""
        body = (await client.post("/api/upstreams/proxy-a/test")).json()
        assert body["ok"] is False
        assert body["http_status"] is None
        assert body["error"]

    async def test_the_result_carries_no_response_body(self, tmp_path: Path) -> None:
        """返回响应体就等于交出一个通用的内网探测器。"""
        server = await _fake_proxy(status=b"HTTP/1.1 200 Connection Established")
        host, port = server.sockets[0].getsockname()[:2]
        app = await running(tmp_path, address=f"{host}:{port}")
        try:
            async with client_for(app) as c:
                body = (await c.post("/api/upstreams/proxy-a/test")).json()
        finally:
            await app.stop()
            server.close()
            await server.wait_closed()
        assert body["ok"] is True
        assert body["http_status"] == 200
        assert set(body) == {"name", "ok", "elapsed_ms", "target", "http_status", "error"}

    async def test_a_refusing_proxy_is_reported_without_its_message(self, tmp_path: Path) -> None:
        server = await _fake_proxy(status=b"HTTP/1.1 407 Proxy Authentication Required")
        host, port = server.sockets[0].getsockname()[:2]
        app = await running(tmp_path, address=f"{host}:{port}")
        try:
            async with client_for(app) as c:
                body = (await c.post("/api/upstreams/proxy-a/test")).json()
        finally:
            await app.stop()
            server.close()
            await server.wait_closed()
        assert body["ok"] is False
        assert body["http_status"] == 407
        assert body["error"] == "PROXY_REFUSED_CONNECT"

    async def test_probing_leaves_the_health_state_alone(self, tmp_path: Path) -> None:
        """手动诊断不该改路由行为：一次失败的探测不能把出口熔断掉。"""
        app = await running(tmp_path)
        try:
            async with client_for(app) as c:
                for _ in range(5):
                    assert (await c.post("/api/upstreams/proxy-a/test")).status_code == 200
            health = app.state.health.snapshot_of("proxy-a")
        finally:
            await app.stop()
        assert (health.total_failure, health.consecutive_failures) == (0, 0)

    async def test_a_direct_probe_reaches_the_target_itself(self) -> None:
        """``direct`` 没有上级代理可握手，隧道状态人为置 200。"""
        opened: list[tuple[str, int]] = []

        class Recording:
            async def connect_tunnel(
                self, upstream: UpstreamConfig, target: RequestTarget, routing: RoutingConfig
            ) -> TunnelConn:
                opened.append((target.host, target.port))
                return TunnelConn(
                    conn=UpstreamConn(
                        reader=asyncio.StreamReader(),
                        writer=_ClosedWriter(),  # type: ignore[arg-type]
                        peer_host=target.host,
                        peer_port=target.port,
                    ),
                    status=200,
                )

        result = await probing.probe(
            UpstreamConfig(name="direct", type="direct", address=None),
            connector=Recording(),  # type: ignore[arg-type]
        )
        assert opened == [(probing.PROBE_HOST, probing.PROBE_PORT)]
        assert result.ok is True


class _ClosedWriter:
    """只需要满足 ``UpstreamConn.close()`` 的调用形状。"""

    def is_closing(self) -> bool:
        return True


async def _fake_proxy(*, status: bytes) -> asyncio.Server:
    """一个只回状态行的假上级代理。顺带回一段响应体，验证它不会被转发出去。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(status + b"\r\n\r\nSECRET-BODY")
            await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)

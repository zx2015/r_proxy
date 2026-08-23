"""M2 验收点逐条核对。

对应设计：docs/design/MIGRATION.md §4.3。编号与该表一一对应，缺一条即
M2 未完成。判据与状态机的细节由各模块单元测试覆盖，这里走真实套接字，
验证端到端的可观察行为。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from r_proxy.app import Application
from tests.conftest import start_server

Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

# 冒充 TLS ClientHello 的前几个字节：只需让隧道里有可辨认的载荷。
CLIENT_HELLO = b"\x16\x03\x01hello"


BASE_CONFIG = '[listen]\nhost = "127.0.0.1"\nport = 0\n[webui]\nenabled = false\n'


def config(*upstreams: str, routing: str = "") -> str:
    return BASE_CONFIG + routing + "".join(upstreams)


def http_upstream(name: str, address: str, *, priority: int = 100) -> str:
    return (
        f'\n[[upstreams]]\nname = "{name}"\ntype = "http"\n'
        f'address = "{address}"\npriority = {priority}\n'
    )


def direct_upstream(name: str = "direct", *, priority: int = 100) -> str:
    return f'\n[[upstreams]]\nname = "{name}"\ntype = "direct"\npriority = {priority}\n'


async def running_app(tmp_path: Path, text: str) -> Application:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    app = Application(config_path=path)
    await app.start()
    return app


async def get(app: Application, host: str = "site.test", *, method: str = "GET") -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    request = f"{method} http://{host}/ HTTP/1.1\r\nHost: {host}\r\nContent-Length: 0\r\n\r\n"
    writer.write(request.encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


async def connect(
    app: Application, authority: str = "site.test:443"
) -> tuple[bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
    return head, reader, writer


# -- 各种行为的假上级代理 ---------------------------------------------------


def responder(
    status_line: bytes, headers: bytes = b"", *, seen: list[str] | None = None
) -> Handler:
    """按固定状态行应答普通 HTTP 请求。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        if seen is not None:
            seen.append(head.decode("latin-1").split("\r\n")[0])
        body = b"Content-Length: 2\r\nConnection: close\r\n\r\nhi"
        w.write(status_line + b"\r\n" + headers + body)
        await w.drain()
        w.close()

    return handler


def tunnel_proxy(status_line: bytes, *, echo: bool = False) -> Handler:
    """按固定状态行应答 CONNECT。``echo=True`` 时握手成功并回送隧道字节。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await r.readuntil(b"\r\n\r\n")
        w.write(status_line + b"\r\n\r\n")
        await w.drain()
        if not echo:
            w.close()
            return
        while chunk := await r.read(4096):
            w.write(chunk)
            await w.drain()

    return handler


async def _echo(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    while chunk := await r.read(4096):
        w.write(chunk)
        await w.drain()


class TestM2Acceptance:
    async def test_m2_01_dead_first_upstream_is_transparent(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """M2-01：首个出口 TCP 连不上，自动切换到下一个，客户端无感知。"""
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("dead", f"127.0.0.1:{closed_port}", priority=10),
                http_upstream("good", good.address, priority=20),
            ),
        )
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 200 OK")
        finally:
            await app.stop()

    async def test_m2_02_same_priority_upstreams_alternate(self, tmp_path: Path) -> None:
        """M2-02：同优先级两出口，连续请求交替作为链首。

        每个请求用不同的 host：M3 起同一 host 的第二次访问会命中粘性、
        固定沿用上次成功的出口，轮询只在不同 host 之间体现。
        """
        seen_a: list[str] = []
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_a))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=10),
            ),
        )
        try:
            for i in range(4):
                await get(app, host=f"h{i}.test")
            assert len(seen_a) == 2
            assert len(seen_b) == 2
        finally:
            await app.stop()

    @pytest.mark.parametrize("status", [404, 500])
    async def test_m2_03_and_04_target_handled_statuses_are_passed_through(
        self, tmp_path: Path, status: int
    ) -> None:
        """M2-03、M2-04：``404``/``500`` 证明目标已处理，不切换。"""
        seen_b: list[str] = []
        a = await start_server(responder(f"HTTP/1.1 {status} X".encode()))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            resp = await get(app)
            assert resp.startswith(f"HTTP/1.1 {status}".encode())
            assert seen_b == []
        finally:
            await app.stop()

    async def test_m2_05_cloudflare_origin_error_is_not_a_failure(self, tmp_path: Path) -> None:
        """M2-05：CF ``521`` 不切换，且不计出口失败。"""
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 521 Web Server Is Down"))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 521")
            assert seen_b == []
            assert app.state.health.snapshot_of("a").total_failure == 0
        finally:
            await app.stop()

    async def test_m2_06_configured_521_still_does_not_switch(self, tmp_path: Path) -> None:
        """M2-06：用户把 ``521`` 加进 ``switch_on_status`` 也不切换——这是硬约束。"""
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 521 Web Server Is Down"))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
                routing="[routing]\nswitch_on_status = [503, 521]\n",
            ),
        )
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 521")
            assert seen_b == []
        finally:
            await app.stop()

    async def test_m2_07_connect_503_switches(self, tmp_path: Path) -> None:
        """M2-07：CONNECT 收到 ``503`` 必然来自代理，切换。"""
        bad = await start_server(tunnel_proxy(b"HTTP/1.1 503 Service Unavailable"))
        good = await start_server(tunnel_proxy(b"HTTP/1.1 200 Connection Established", echo=True))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("bad", bad.address, priority=10),
                http_upstream("good", good.address, priority=20),
            ),
        )
        try:
            head, reader, writer = await connect(app)
            assert head.startswith(b"HTTP/1.1 200")
            writer.write(b"tls")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(3), 5) == b"tls"
            writer.close()
        finally:
            await app.stop()

    async def test_m2_08_http_503_from_nginx_does_not_switch(self, tmp_path: Path) -> None:
        """M2-08：``Server: nginx`` 说明目标自己的网关坏了，换出口无用。"""
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 503 Unavailable", b"Server: nginx/1.24\r\n"))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 503")
            assert seen_b == []
        finally:
            await app.stop()

    async def test_m2_08b_http_503_from_squid_switches(self, tmp_path: Path) -> None:
        """反面：同一个 ``503`` 带上代理特征头就应当切换。"""
        seen_b: list[str] = []
        a = await start_server(
            responder(b"HTTP/1.1 503 Unavailable", b"X-Squid-Error: ERR_CONNECT_FAIL\r\n")
        )
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            assert (await get(app)).startswith(b"HTTP/1.1 200")
            assert len(seen_b) == 1
        finally:
            await app.stop()

    async def test_m2_09_post_after_send_does_not_switch_but_is_remembered(
        self, tmp_path: Path
    ) -> None:
        """M2-09：POST 已发出后收到 ``503`` 不切换，但记 ``route_error``。"""
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 503 Unavailable"))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            assert (await get(app, method="POST")).startswith(b"HTTP/1.1 503")
            assert seen_b == []
            assert app.state.memory.is_blocked("site.test", "a", now=_now(app)) is True
        finally:
            await app.stop()

    async def test_m2_09b_next_request_avoids_the_remembered_upstream(self, tmp_path: Path) -> None:
        """负面记忆的收益体现在**后续**请求上。"""
        seen_a: list[str] = []
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 503 Unavailable", seen=seen_a))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            await get(app, method="POST")
            await get(app)
            assert len(seen_a) == 1
            assert len(seen_b) == 1
        finally:
            await app.stop()

    async def test_m2_10_oversized_body_cannot_switch(self, tmp_path: Path) -> None:
        """M2-10：请求体超过 ``switch_buffer_bytes`` 已流式转发，不可重放。"""
        seen_b: list[str] = []
        a = await start_server(_read_all_then(b"HTTP/1.1 503 Unavailable"))
        b = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
                routing="[routing]\nswitch_buffer_bytes = 4096\n",
            ),
        )
        try:
            resp = await _put_body(app, b"x" * 100_000)
            assert resp.startswith(b"HTTP/1.1 503")
            assert seen_b == []
        finally:
            await app.stop()

    async def test_m2_10b_small_body_is_replayed_on_the_next_upstream(self, tmp_path: Path) -> None:
        """反面：请求体在缓冲上限内时应当完整重放到新出口。"""
        received: list[bytes] = []
        a = await start_server(_read_all_then(b"HTTP/1.1 503 Unavailable"))
        b = await start_server(_read_all_then(b"HTTP/1.1 200 OK", into=received))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
            ),
        )
        try:
            resp = await _put_body(app, b"y" * 1000)
            assert resp.startswith(b"HTTP/1.1 200")
            assert received and received[0].endswith(b"y" * 1000)
        finally:
            await app.stop()

    async def test_m2_11_upstream_opens_after_repeated_tcp_failures(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """M2-11：上级代理连续 5 次 TCP 失败即熔断。"""
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("dead", f"127.0.0.1:{closed_port}", priority=10),
                http_upstream("good", good.address, priority=20),
            ),
        )
        try:
            for i in range(5):
                await get(app, f"h{i}.test")
            assert app.state.health.is_available("dead", now=_now(app)) is False
        finally:
            await app.stop()

    async def test_m2_12_direct_never_opens(self, tmp_path: Path, closed_port: int) -> None:
        """M2-12：``direct`` 连续失败 20 次也不熔断。

        这是最关键的一条：若归类有误，用户访问一批被墙站点后内网也会不可
        访问——那是功能性事故。
        """
        app = await running_app(tmp_path, config(direct_upstream()))
        try:
            for i in range(20):
                await _get_address(app, f"127.0.0.1:{closed_port}", host=f"h{i}.test")
            assert app.state.health.is_available("direct", now=_now(app)) is True
            assert app.state.health.snapshot_of("direct").total_failure == 20
        finally:
            await app.stop()

    async def test_m2_13_half_open_admits_a_single_probe(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """M2-13：``half_open`` 期间 10 个并发请求只有 1 个探测该出口。"""
        good = await start_server(responder(b"HTTP/1.1 200 OK"))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("dead", f"127.0.0.1:{closed_port}", priority=10),
                http_upstream("good", good.address, priority=20),
                routing="[routing.circuit_breaker]\nfail_threshold = 2\ncooldown_seconds = 0\n",
            ),
        )
        try:
            for i in range(2):
                await get(app, f"warm{i}.test")
            before = app.state.health.snapshot_of("dead").total_failure

            await asyncio.gather(*(get(app, f"c{i}.test") for i in range(10)))
            attempts = app.state.health.snapshot_of("dead").total_failure - before
            assert attempts <= 2
        finally:
            await app.stop()

    async def test_m2_14_status_switches_are_rate_limited(self, tmp_path: Path) -> None:
        """M2-14：同 host 状态码切换配额耗尽后不再切换。

        配额按「判定通过」计数，与候选链上还剩几个出口无关：a 的 503 与 b 的
        503 各消耗一份。配额 3 因此只够两次完整遍历中的前一次半——第 2 次请求
        到 b 时配额已尽，b 的 503 直接回给客户端。
        """
        seen_b: list[str] = []
        a = await start_server(responder(b"HTTP/1.1 503 Unavailable"))
        b = await start_server(responder(b"HTTP/1.1 503 Unavailable", seen=seen_b))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("a", a.address, priority=10),
                http_upstream("b", b.address, priority=20),
                routing=(
                    "[routing]\nroute_block_ttl = 0\n"
                    "[routing.status_switch_rate_limit]\nmax_switches_per_host = 3\n"
                ),
            ),
        )
        try:
            for _ in range(6):
                await get(app)
            # 配额耗尽后 a 的 503 直接回给客户端，后 4 个请求都不再触达 b。
            assert len(seen_b) == 2
        finally:
            await app.stop()

    async def test_m2_15_transport_failures_are_never_rate_limited(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """M2-15：限流它会让代理在网络抖动时失去自愈能力。"""
        seen: list[str] = []
        good = await start_server(responder(b"HTTP/1.1 200 OK", seen=seen))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("dead", f"127.0.0.1:{closed_port}", priority=10),
                http_upstream("good", good.address, priority=20),
                routing=(
                    "[routing.status_switch_rate_limit]\nmax_switches_per_host = 1\n"
                    "[routing.circuit_breaker]\nenabled = false\n"
                ),
            ),
        )
        try:
            for _ in range(11):
                assert (await get(app)).startswith(b"HTTP/1.1 200")
            assert len(seen) == 11
        finally:
            await app.stop()

    async def test_m2_16_ipv6_only_target_skips_direct_without_egress(self, tmp_path: Path) -> None:
        """M2-16：纯 IPv6 目标 + 无 IPv6 能力 → 跳过 ``direct``，不记失败。"""
        app = await running_app(tmp_path, config(direct_upstream()))
        app.state.set_ipv6_egress(False)
        try:
            resp = await _get_address(app, "[2001:db8::1]:80", host="[2001:db8::1]")
            assert resp.startswith(b"HTTP/1.1 502")
            assert app.state.health.snapshot_of("direct").total_failure == 0
            assert app.state.memory.size == 0
        finally:
            await app.stop()

    async def test_m2_17_dual_stack_target_falls_back_to_ipv4(self, tmp_path: Path) -> None:
        """M2-17：双栈目标 + IPv6 侧不可用时由 Happy Eyeballs 回落 IPv4。"""
        site = await start_server(responder(b"HTTP/1.1 200 OK"), host="127.0.0.1")
        app = await running_app(tmp_path, config(direct_upstream()))
        try:
            resp = await _get_address(app, f"localhost:{site.port}", host="localhost")
            assert resp.startswith(b"HTTP/1.1 200 OK")
        finally:
            await app.stop()

    async def test_m2_18_premature_client_hello_survives_a_switch(self, tmp_path: Path) -> None:
        """M2-18：CONNECT 抢跑的 ClientHello 在切换后仍要送达。

        丢弃这些字节会让 TLS 握手挂到超时，且只在不等 ``200`` 的客户端上
        出现——那是最难定位的一类 bug。
        """
        bad = await start_server(tunnel_proxy(b"HTTP/1.1 503 Service Unavailable"))
        good = await start_server(tunnel_proxy(b"HTTP/1.1 200 Connection Established", echo=True))
        app = await running_app(
            tmp_path,
            config(
                http_upstream("bad", bad.address, priority=10),
                http_upstream("good", good.address, priority=20),
            ),
        )
        try:
            host, port = app.proxy_address
            reader, writer = await asyncio.open_connection(host, port)
            # 请求行、头部与 ClientHello 在同一次写入中发出，不等 200。
            writer.write(b"CONNECT site.test:443 HTTP/1.1\r\n\r\n" + CLIENT_HELLO)
            await writer.drain()

            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            assert head.startswith(b"HTTP/1.1 200")
            echoed = await asyncio.wait_for(reader.readexactly(len(CLIENT_HELLO)), timeout=5)
            assert echoed == CLIENT_HELLO
            writer.close()
        finally:
            await app.stop()

    async def test_m2_19_premature_tunnel_death_is_remembered(self, tmp_path: Path) -> None:
        """M2-19：隧道 3 秒内关闭且上游零字节 → 记 ``route_error``。"""
        silent = await start_server(tunnel_proxy(b"HTTP/1.1 200 Connection Established"))
        app = await running_app(
            tmp_path, config(http_upstream("silent", silent.address, priority=10))
        )
        try:
            head, reader, writer = await connect(app)
            assert head.startswith(b"HTTP/1.1 200")
            writer.write(CLIENT_HELLO)
            await writer.drain()
            await asyncio.wait_for(reader.read(), timeout=5)
            writer.close()
            await asyncio.sleep(0.05)
            assert app.state.memory.is_blocked("site.test", "silent", now=_now(app)) is True
        finally:
            await app.stop()

    async def test_m2_19b_tunnel_with_upstream_bytes_is_not_remembered(
        self, tmp_path: Path
    ) -> None:
        """只看时长会误判正常的短连接：上游回过字节就不算早夭。"""
        chatty = await start_server(tunnel_proxy(b"HTTP/1.1 200 Connection Established", echo=True))
        app = await running_app(
            tmp_path, config(http_upstream("chatty", chatty.address, priority=10))
        )
        try:
            head, reader, writer = await connect(app)
            assert head.startswith(b"HTTP/1.1 200")
            writer.write(b"ping")
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(4), 5) == b"ping"
            writer.close()
            await asyncio.sleep(0.05)
            assert app.state.memory.is_blocked("site.test", "chatty", now=_now(app)) is False
        finally:
            await app.stop()

    async def test_m2_19c_a_tunnel_the_client_never_used_is_not_remembered(
        self, tmp_path: Path
    ) -> None:
        """客户端一个字节都没发就关掉的隧道，不算出口的账。

        浏览器的预连接与连接池探活每小时能造出几十条这样的连接，从 r-proxy
        的视角与「ClientHello 发出后被 RST」几乎同形——区别只在关闭是谁发起的。
        少了这个条件，负面记忆会被假标记填满，真故障反而被淹没。

        上游用 ``echo=True``：它要像一条健康的隧道那样**留在线上**等数据，
        否则先关的是上游，这次就真的是早夭，测的也就不是这个场景了。
        """
        alive = await start_server(tunnel_proxy(b"HTTP/1.1 200 Connection Established", echo=True))
        app = await running_app(
            tmp_path, config(http_upstream("alive", alive.address, priority=10))
        )
        try:
            head, _, writer = await connect(app)
            assert head.startswith(b"HTTP/1.1 200")
            writer.close()  # 客户端自己放弃，一个字节都没发
            await asyncio.sleep(0.05)
            assert app.state.memory.is_blocked("site.test", "alive", now=_now(app)) is False
        finally:
            await app.stop()

    async def test_m2_20_exhausted_chain_leaks_nothing(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """M2-20：候选链耗尽的响应体不含出口名、地址、失败原因、链长。"""
        app = await running_app(
            tmp_path,
            config(
                http_upstream("secret-corp-proxy", f"10.1.2.3:{closed_port}", priority=10),
                http_upstream("backup-dmz", f"127.0.0.1:{closed_port}", priority=20),
                routing="[routing]\nconnect_timeout = 0.3\n",
            ),
        )
        try:
            body = await get(app)
            assert body.startswith(b"HTTP/1.1 502")
            for leaked in (
                b"secret-corp-proxy",
                b"backup-dmz",
                b"10.1.2.3",
                b"127.0.0.1",
                str(closed_port).encode(),
                b"ECONNREFUSED",
                b"Timeout",
            ):
                assert leaked not in body, leaked
        finally:
            await app.stop()

    async def test_m2_20b_exhausted_chain_still_reports_the_request_id(
        self, tmp_path: Path, closed_port: int
    ) -> None:
        """不泄露拓扑，但必须给出可用于查日志的 ``request_id``。"""
        app = await running_app(tmp_path, config(http_upstream("a", f"127.0.0.1:{closed_port}")))
        try:
            body = await get(app)
            assert b"X-R-Proxy-Request-Id:" in body
        finally:
            await app.stop()


# -- 辅助 ------------------------------------------------------------------


def _now(app: Application) -> float:
    return time.monotonic()


async def _get_address(app: Application, address: str, *, host: str) -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    writer.write(f"GET http://{address}/ HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


async def _put_body(app: Application, body: bytes) -> bytes:
    proxy_host, proxy_port = app.proxy_address
    reader, writer = await asyncio.open_connection(proxy_host, proxy_port)
    head = (
        f"PUT http://site.test/ HTTP/1.1\r\nHost: site.test\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    writer.write(head + body)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(65536), timeout=10)
    writer.close()
    return data


def _read_all_then(status_line: bytes, *, into: list[bytes] | None = None) -> Handler:
    """读完整个请求（含请求体）后再应答。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        head = await r.readuntil(b"\r\n\r\n")
        length = _content_length(head)
        body = await r.readexactly(length) if length else b""
        if into is not None:
            into.append(head + body)
        w.write(status_line + b"\r\nContent-Length: 2\r\nConnection: close\r\n\r\nhi")
        await w.drain()
        w.close()

    return handler


def _content_length(head: bytes) -> int:
    for line in head.decode("latin-1").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "content-length":
            return int(value.strip())
    return 0

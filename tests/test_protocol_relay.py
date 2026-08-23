"""protocol/relay.py 的中继与背压测试。

对应设计：docs/design/DD_PROXY.md §5.2、§6.2
"""

from __future__ import annotations

import asyncio

import pytest

from r_proxy.protocol.relay import RelayStats, pump, relay_bidirectional
from tests.conftest import FakeServer, start_server


async def connect(srv: FakeServer) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection(srv.host, srv.port)


class TestPump:
    async def test_copies_all_bytes(self, echo_server: FakeServer) -> None:
        reader, writer = await connect(echo_server)
        src = asyncio.StreamReader()
        src.feed_data(b"hello world")
        src.feed_eof()

        moved = await pump(src, writer, idle_timeout=5)
        writer.write_eof()
        assert moved == 11
        assert await reader.readexactly(11) == b"hello world"
        writer.close()

    async def test_reports_bytes_through_callback(self, echo_server: FakeServer) -> None:
        _, writer = await connect(echo_server)
        src = asyncio.StreamReader()
        src.feed_data(b"a" * 100)
        src.feed_eof()

        seen: list[int] = []
        await pump(src, writer, idle_timeout=5, on_bytes=seen.append)
        assert sum(seen) == 100
        writer.close()

    async def test_empty_source_moves_nothing(self, echo_server: FakeServer) -> None:
        _, writer = await connect(echo_server)
        src = asyncio.StreamReader()
        src.feed_eof()
        assert await pump(src, writer, idle_timeout=5) == 0
        writer.close()

    async def test_idle_timeout_fires_when_no_bytes_flow(self, echo_server: FakeServer) -> None:
        _, writer = await connect(echo_server)
        src = asyncio.StreamReader()  # 永不喂数据也不 EOF
        with pytest.raises(TimeoutError):
            await pump(src, writer, idle_timeout=0.05)
        writer.close()

    async def test_idle_timeout_resets_on_each_chunk(self, echo_server: FakeServer) -> None:
        """空闲超时衡量的是「无字节流动」，不是总时长——否则会误杀大文件下载。"""
        _, writer = await connect(echo_server)
        src = asyncio.StreamReader()

        async def feed() -> None:
            for _ in range(5):
                await asyncio.sleep(0.03)
                src.feed_data(b"x")
            src.feed_eof()

        task = asyncio.create_task(feed())
        assert await pump(src, writer, idle_timeout=0.1) == 5
        await task
        writer.close()

    async def test_stalled_destination_bounds_the_write_buffer(self) -> None:
        """对端不读时，drain() 必须让 pump 挂起，而不是把数据全攒在写缓冲里。

        漏掉 drain() 时这个上界会涨到接近喂入的总量——慢客户端下载大文件
        就是这样把内存吃光的。
        """
        stop = asyncio.Event()

        async def never_read(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await stop.wait()

        srv = await start_server(never_read)
        _, writer = await asyncio.open_connection(srv.host, srv.port)
        src = asyncio.StreamReader()
        task = asyncio.create_task(pump(src, writer, idle_timeout=30))
        try:
            total = 32 * 1024 * 1024
            peak = 0
            block = b"z" * 65536
            for _ in range(total // 65536):
                src.feed_data(block)
                await asyncio.sleep(0)
                peak = max(peak, writer.transport.get_write_buffer_size())
            # 内核收发缓冲通常各几百 KB；有背压时应用层写缓冲停在高水位附近。
            assert peak < 4 * 1024 * 1024, peak
        finally:
            task.cancel()
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            writer.close()

    async def test_writer_closed_midway_raises(self, echo_server: FakeServer) -> None:
        _, writer = await connect(echo_server)
        writer.close()
        await writer.wait_closed()
        src = asyncio.StreamReader()
        src.feed_data(b"data")
        src.feed_eof()
        with pytest.raises((ConnectionError, OSError)):
            await pump(src, writer, idle_timeout=5)


class TestRelayBidirectional:
    async def test_both_directions_carry_data(self) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            data = await r.readexactly(4)
            w.write(b"srv:" + data)
            await w.drain()
            await asyncio.sleep(0.3)

        srv = await start_server(handler)
        try:
            up_r, up_w = await connect(srv)
            client_r = asyncio.StreamReader()
            client_r.feed_data(b"ping")

            collected = bytearray()
            sink_r, sink_w = await _pipe_into(collected)

            stats = RelayStats()
            task = asyncio.create_task(
                relay_bidirectional(client_r, sink_w, up_r, up_w, idle_timeout=1, stats=stats)
            )
            await asyncio.sleep(0.15)
            client_r.feed_eof()
            await task

            assert bytes(collected) == b"srv:ping"
            assert stats.bytes_up == 4
            assert stats.bytes_down == 8
            sink_r.feed_eof()
        finally:
            await srv.close()

    async def test_one_side_closing_ends_the_relay(self) -> None:
        """任一方向关闭即结束，不等另一方向——否则半死连接会一直挂着。"""

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()  # 上游立刻关闭

        srv = await start_server(handler)
        try:
            up_r, up_w = await connect(srv)
            client_r = asyncio.StreamReader()  # 客户端方向永不结束
            _, sink_w = await _pipe_into(bytearray())

            await asyncio.wait_for(
                relay_bidirectional(client_r, sink_w, up_r, up_w, idle_timeout=5),
                timeout=2,
            )
        finally:
            await srv.close()

    async def test_no_pending_task_warnings(self, recwarn: pytest.WarningsRecorder) -> None:
        """被取消的方向必须 await 到真正结束，否则会有 Task destroyed 告警。"""

        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.close()

        srv = await start_server(handler)
        try:
            up_r, up_w = await connect(srv)
            client_r = asyncio.StreamReader()
            _, sink_w = await _pipe_into(bytearray())
            await relay_bidirectional(client_r, sink_w, up_r, up_w, idle_timeout=5)
            await asyncio.sleep(0)
        finally:
            await srv.close()
        assert not [w for w in recwarn if "pending" in str(w.message)]

    async def test_stats_record_duration(self) -> None:
        async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            await asyncio.sleep(0.1)
            w.close()

        srv = await start_server(handler)
        try:
            up_r, up_w = await connect(srv)
            client_r = asyncio.StreamReader()
            _, sink_w = await _pipe_into(bytearray())
            stats = RelayStats()
            await relay_bidirectional(client_r, sink_w, up_r, up_w, idle_timeout=5, stats=stats)
            assert stats.duration_ms >= 50
        finally:
            await srv.close()


async def _pipe_into(sink: bytearray) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """返回一对流，写入 writer 的字节会落进 sink。"""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        while chunk := await r.read(4096):
            sink.extend(chunk)

    srv = await start_server(handler)
    reader, writer = await asyncio.open_connection(srv.host, srv.port)
    return reader, writer

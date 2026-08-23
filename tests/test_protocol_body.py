"""protocol/body.py 的请求体读取与请求走私防护测试。

对应设计：docs/design/DD_PROXY.md §3.5
"""

from __future__ import annotations

import asyncio

import pytest

from r_proxy.contracts import Headers
from r_proxy.protocol.body import BodyPlan, plan_body, stream_body
from r_proxy.protocol.parse import BadRequest


def headers(*pairs: tuple[str, str]) -> Headers:
    return Headers(pairs)


async def reader_of(data: bytes) -> asyncio.StreamReader:
    r = asyncio.StreamReader()
    r.feed_data(data)
    r.feed_eof()
    return r


class TestPlanBody:
    def test_no_body_headers_means_no_body(self) -> None:
        assert plan_body(headers(("Host", "x.com"))) == BodyPlan(kind="none", length=0)

    def test_content_length(self) -> None:
        assert plan_body(headers(("Content-Length", "42"))) == BodyPlan(kind="length", length=42)

    def test_zero_content_length_is_no_body(self) -> None:
        assert plan_body(headers(("Content-Length", "0"))).kind == "none"

    def test_chunked(self) -> None:
        assert plan_body(headers(("Transfer-Encoding", "chunked"))).kind == "chunked"

    def test_chunked_is_case_insensitive(self) -> None:
        assert plan_body(headers(("Transfer-Encoding", "Chunked"))).kind == "chunked"

    def test_both_framing_headers_is_rejected(self) -> None:
        """请求走私的经典入口：代理与上游按不同头解释边界。"""
        with pytest.raises(BadRequest) as e:
            plan_body(headers(("Content-Length", "5"), ("Transfer-Encoding", "chunked")))
        assert "走私" in str(e.value) or "smuggling" in str(e.value).lower()

    def test_duplicate_content_length_with_same_value_is_ok(self) -> None:
        assert plan_body(headers(("Content-Length", "5"), ("Content-Length", "5"))).length == 5

    def test_duplicate_content_length_with_different_values_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            plan_body(headers(("Content-Length", "5"), ("Content-Length", "6")))

    def test_non_numeric_content_length_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            plan_body(headers(("Content-Length", "abc")))

    def test_negative_content_length_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            plan_body(headers(("Content-Length", "-1")))

    def test_unsupported_transfer_encoding_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            plan_body(headers(("Transfer-Encoding", "gzip")))


class TestStreamBody:
    async def test_none_plan_consumes_nothing(self) -> None:
        src = await reader_of(b"leftover")
        out = bytearray()
        assert await stream_body(src, out.extend, BodyPlan(kind="none", length=0)) == 0
        assert await src.read() == b"leftover"

    async def test_length_plan_reads_exactly(self) -> None:
        src = await reader_of(b"12345rest")
        out = bytearray()
        assert await stream_body(src, out.extend, BodyPlan(kind="length", length=5)) == 5
        assert bytes(out) == b"12345"
        assert await src.read() == b"rest"

    async def test_length_plan_streams_in_chunks(self) -> None:
        """1GB 的 Content-Length 不能一次读进内存。"""
        src = await reader_of(b"x" * 200_000)
        sizes: list[int] = []
        await stream_body(
            src, lambda c: sizes.append(len(c)), BodyPlan(kind="length", length=200_000)
        )
        assert len(sizes) > 1
        assert max(sizes) <= 65536

    async def test_truncated_length_body_is_a_disconnect(self) -> None:
        src = await reader_of(b"123")
        with pytest.raises(EOFError):
            await stream_body(src, lambda c: None, BodyPlan(kind="length", length=10))

    async def test_chunked_body_is_forwarded_verbatim(self) -> None:
        """上游同样按 chunked 解析，原样转发即可，不必解码再编码。"""
        raw = b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
        src = await reader_of(raw + b"AFTER")
        out = bytearray()
        await stream_body(src, out.extend, BodyPlan(kind="chunked", length=None))
        assert bytes(out) == raw
        assert await src.read() == b"AFTER"

    async def test_chunked_with_extension_and_trailer(self) -> None:
        raw = b"5;name=v\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n"
        src = await reader_of(raw)
        out = bytearray()
        await stream_body(src, out.extend, BodyPlan(kind="chunked", length=None))
        assert bytes(out) == raw

    async def test_chunked_empty_body(self) -> None:
        src = await reader_of(b"0\r\n\r\n")
        out = bytearray()
        await stream_body(src, out.extend, BodyPlan(kind="chunked", length=None))
        assert bytes(out) == b"0\r\n\r\n"

    async def test_malformed_chunk_size_is_rejected(self) -> None:
        src = await reader_of(b"zz\r\nhello\r\n0\r\n\r\n")
        with pytest.raises(BadRequest):
            await stream_body(src, lambda c: None, BodyPlan(kind="chunked", length=None))

    async def test_truncated_chunked_body_is_a_disconnect(self) -> None:
        src = await reader_of(b"5\r\nhel")
        with pytest.raises(EOFError):
            await stream_body(src, lambda c: None, BodyPlan(kind="chunked", length=None))

    async def test_oversized_chunk_size_line_is_rejected(self) -> None:
        src = await reader_of(b"5" * 9000 + b"\r\n")
        with pytest.raises(BadRequest):
            await stream_body(src, lambda c: None, BodyPlan(kind="chunked", length=None))

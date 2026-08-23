"""protocol/parse.py 的请求解析测试。

对应设计：docs/design/DD_PROXY.md §3
"""

from __future__ import annotations

import asyncio

import pytest

from r_proxy.contracts import AddressFamily, Headers, Method
from r_proxy.protocol.parse import (
    MAX_HEADER_COUNT,
    BadRequest,
    ClientDisconnected,
    HeaderTooLarge,
    RequestLineTooLong,
    normalize_host,
    parse_head,
    parse_target,
    read_head,
    strip_hop_by_hop,
)


def head(*lines: str) -> bytes:
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def reader_of(data: bytes, *, limit: int = 65536) -> asyncio.StreamReader:
    r = asyncio.StreamReader(limit=limit)
    r.feed_data(data)
    r.feed_eof()
    return r


class TestNormalizeHost:
    def test_lowercases(self) -> None:
        assert normalize_host("Example.COM") == "example.com"

    def test_strips_fqdn_trailing_dot(self) -> None:
        assert normalize_host("example.com.") == "example.com"

    def test_strips_brackets(self) -> None:
        assert normalize_host("[2001:db8::1]") == "2001:db8::1"

    def test_compresses_ipv6(self) -> None:
        assert normalize_host("2001:0db8:0000:0000:0000:0000:0000:0001") == "2001:db8::1"

    def test_uppercase_ipv6_is_normalised(self) -> None:
        assert normalize_host("2001:DB8::1") == "2001:db8::1"

    def test_bracketed_and_bare_ipv6_agree(self) -> None:
        """两种写法必须产生同一个粘性键，否则记忆会分裂。"""
        assert normalize_host("[2001:0DB8::1]") == normalize_host("2001:db8::1")

    def test_ipv4_is_unchanged(self) -> None:
        assert normalize_host("192.168.1.1") == "192.168.1.1"

    def test_surrounding_whitespace_removed(self) -> None:
        assert normalize_host("  example.com  ") == "example.com"


class TestParseTargetConnect:
    def test_host_and_port(self) -> None:
        t = parse_target("CONNECT", "example.com:443", Headers())
        assert (t.host, t.port, t.is_connect, t.url) == ("example.com", 443, True, None)
        assert t.method is Method.CONNECT

    def test_bracketed_ipv6(self) -> None:
        t = parse_target("CONNECT", "[2001:db8::1]:443", Headers())
        assert (t.host, t.port) == ("2001:db8::1", 443)
        assert t.family is AddressFamily.IPV6_ONLY

    def test_bare_ipv6_is_rejected_with_a_hint(self) -> None:
        """歧义不能靠猜：`...::1:443` 的 443 可能是端口也可能是地址末段。"""
        with pytest.raises(BadRequest) as e:
            parse_target("CONNECT", "2001:db8::1:443", Headers())
        assert "[" in str(e.value) and "]" in str(e.value)

    def test_port_is_mandatory(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "example.com", Headers())

    def test_non_numeric_port(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "example.com:https", Headers())

    def test_port_out_of_range(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "example.com:70000", Headers())

    def test_unclosed_bracket(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "[2001:db8::1:443", Headers())

    def test_garbage_after_bracket(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "[2001:db8::1]x443", Headers())

    def test_invalid_ipv6_inside_brackets(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("CONNECT", "[not:an:address:]:443", Headers())


class TestParseTargetAbsoluteForm:
    def test_http_defaults_to_80(self) -> None:
        t = parse_target("GET", "http://example.com/a/b", Headers())
        assert (t.host, t.port, t.is_connect) == ("example.com", 80, False)
        assert t.url == "http://example.com/a/b"

    def test_https_defaults_to_443(self) -> None:
        t = parse_target("GET", "https://example.com/", Headers())
        assert t.port == 443

    def test_explicit_port_wins(self) -> None:
        assert parse_target("GET", "http://example.com:8080/", Headers()).port == 8080

    def test_bracketed_ipv6_authority(self) -> None:
        t = parse_target("GET", "http://[2001:db8::1]:8080/x", Headers())
        assert (t.host, t.port) == ("2001:db8::1", 8080)

    def test_host_header_is_ignored_when_absolute(self) -> None:
        """绝对形式的请求行是权威来源，Host 头可能与之不符。"""
        t = parse_target("GET", "http://real.com/", Headers([("Host", "spoofed.com")]))
        assert t.host == "real.com"


class TestParseTargetOriginForm:
    def test_uses_host_header(self) -> None:
        t = parse_target("GET", "/index.html", Headers([("Host", "example.com")]))
        assert (t.host, t.port) == ("example.com", 80)
        assert t.url == "http://example.com/index.html"

    def test_host_header_port(self) -> None:
        t = parse_target("GET", "/", Headers([("Host", "example.com:8080")]))
        assert (t.host, t.port) == ("example.com", 8080)

    def test_missing_host_header_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            parse_target("GET", "/index.html", Headers())

    def test_host_header_lookup_is_case_insensitive(self) -> None:
        assert parse_target("GET", "/", Headers([("HOST", "example.com")])).host == "example.com"


class TestMethodAndFamily:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("GET", Method.GET), ("post", Method.POST), ("PATCH", Method.PATCH)],
    )
    def test_known_methods(self, raw: str, expected: Method) -> None:
        assert parse_target(raw, "http://x.com/", Headers()).method is expected

    def test_unknown_method_maps_to_other(self) -> None:
        assert parse_target("FROBNICATE", "http://x.com/", Headers()).method is Method.OTHER

    def test_post_and_other_are_not_idempotent(self) -> None:
        assert not Method.POST.idempotent
        assert not Method.OTHER.idempotent
        assert Method.GET.idempotent

    def test_ipv4_literal_family(self) -> None:
        assert parse_target("GET", "http://1.2.3.4/", Headers()).family is AddressFamily.IPV4_ONLY

    def test_domain_family_is_unknown_until_resolved(self) -> None:
        assert parse_target("GET", "http://x.com/", Headers()).family is AddressFamily.UNKNOWN


class TestStripHopByHop:
    def test_removes_standard_hop_headers(self) -> None:
        h = strip_hop_by_hop(
            Headers(
                [
                    ("Host", "x.com"),
                    ("Connection", "keep-alive"),
                    ("Proxy-Connection", "keep-alive"),
                    ("Keep-Alive", "timeout=5"),
                    ("Transfer-Encoding", "chunked"),
                    ("Upgrade", "h2c"),
                ]
            )
        )
        assert [k.lower() for k, _ in h.items()] == ["host"]

    def test_removes_proxy_authorization(self) -> None:
        """凭据必须终止于代理，且不得进入后续流程或日志。"""
        h = strip_hop_by_hop(Headers([("Proxy-Authorization", "Basic c2VjcmV0")]))
        assert h.get("proxy-authorization") is None
        assert "c2VjcmV0" not in repr(h)

    def test_removes_headers_named_by_connection(self) -> None:
        h = strip_hop_by_hop(
            Headers(
                [
                    ("Connection", "X-Custom-Thing, Foo"),
                    ("X-Custom-Thing", "1"),
                    ("Foo", "2"),
                    ("Bar", "3"),
                ]
            )
        )
        assert [k for k, _ in h.items()] == ["Bar"]

    def test_keeps_end_to_end_headers(self) -> None:
        h = strip_hop_by_hop(Headers([("Accept", "*/*"), ("Authorization", "Bearer x")]))
        assert h.get("accept") == "*/*"
        assert h.get("authorization") == "Bearer x"


class TestParseHead:
    def test_request_line_and_headers(self) -> None:
        h = parse_head(head("GET /x HTTP/1.1", "Host: example.com", "Accept: */*"))
        assert (h.method, h.target, h.version) == ("GET", "/x", "HTTP/1.1")
        assert h.headers.get("host") == "example.com"

    def test_duplicate_headers_are_preserved(self) -> None:
        h = parse_head(head("GET / HTTP/1.1", "Set-Cookie: a=1", "Set-Cookie: b=2"))
        assert [v for k, v in h.headers.items() if k.lower() == "set-cookie"] == ["a=1", "b=2"]

    def test_value_whitespace_is_trimmed(self) -> None:
        h = parse_head(head("GET / HTTP/1.1", "Host:    example.com   "))
        assert h.headers.get("host") == "example.com"

    def test_empty_value_is_allowed(self) -> None:
        assert parse_head(head("GET / HTTP/1.1", "X-Empty:")).headers.get("x-empty") == ""

    def test_request_line_with_two_fields_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            parse_head(head("GET /x"))

    def test_header_without_colon_is_rejected(self) -> None:
        with pytest.raises(BadRequest):
            parse_head(head("GET / HTTP/1.1", "NotAHeader"))

    def test_too_many_headers(self) -> None:
        lines = ["GET / HTTP/1.1"] + [f"X-{i}: v" for i in range(MAX_HEADER_COUNT + 1)]
        with pytest.raises(HeaderTooLarge):
            parse_head(head(*lines))

    def test_overlong_request_line(self) -> None:
        with pytest.raises(RequestLineTooLong):
            parse_head(head("GET /" + "a" * 9000 + " HTTP/1.1", "Host: x.com"))

    def test_overlong_header_line(self) -> None:
        with pytest.raises(HeaderTooLarge):
            parse_head(head("GET / HTTP/1.1", "X-Big: " + "a" * 9000))

    def test_non_ascii_in_head_is_rejected(self) -> None:
        raw = "GET / HTTP/1.1\r\nHost: \u4e2d\u6587.com\r\n\r\n".encode()
        with pytest.raises(BadRequest):
            parse_head(raw)

    def test_a_bare_lf_inside_the_request_target_is_rejected(self) -> None:
        """裸露的 \\n（没有配对的 \\r）不会被 ``split("\\r\\n")`` 当作行终止符
        吃掉，会原样残留在 target 里；这类字节一旦流到出口连接器构造的
        CONNECT 请求或转发请求里，就是一次请求走私/头部注入。"""
        raw = b"CONNECT evil\ncom:443 HTTP/1.1\r\nHost: x\r\n\r\n"
        with pytest.raises(BadRequest):
            parse_head(raw)

    def test_a_bare_cr_inside_a_header_value_is_rejected(self) -> None:
        raw = b"GET / HTTP/1.1\r\nHost: x.com\r\nX-Evil: a\rSet-Cookie: pwned=1\r\n\r\n"
        with pytest.raises(BadRequest):
            parse_head(raw)


class TestReadHead:
    async def test_reads_a_complete_head(self) -> None:
        r = await reader_of(head("GET / HTTP/1.1", "Host: example.com"))
        assert (await read_head(r, timeout=5)).headers.get("host") == "example.com"

    async def test_body_bytes_are_left_in_the_reader(self) -> None:
        r = await reader_of(head("POST / HTTP/1.1", "Host: x.com") + b"body-bytes")
        await read_head(r, timeout=5)
        assert await r.read(10) == b"body-bytes"

    async def test_disconnect_before_head_completes(self) -> None:
        r = await reader_of(b"GET / HTTP/1.1\r\nHost: x.com\r\n")
        with pytest.raises(ClientDisconnected):
            await read_head(r, timeout=5)

    async def test_empty_connection_is_a_disconnect_not_an_error(self) -> None:
        r = await reader_of(b"")
        with pytest.raises(ClientDisconnected):
            await read_head(r, timeout=5)

    async def test_head_exceeding_reader_limit(self) -> None:
        r = asyncio.StreamReader(limit=256)
        r.feed_data(b"GET / HTTP/1.1\r\n" + b"X-Big: " + b"a" * 4000 + b"\r\n\r\n")
        r.feed_eof()
        with pytest.raises(HeaderTooLarge):
            await read_head(r, timeout=5)

    async def test_slow_client_times_out(self) -> None:
        r = asyncio.StreamReader()
        r.feed_data(b"GET / HTTP/1.1\r\n")  # 永不结束
        with pytest.raises(TimeoutError):
            await read_head(r, timeout=0.05)

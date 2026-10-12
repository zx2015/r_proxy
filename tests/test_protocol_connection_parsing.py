"""``parse_response_head`` 的状态行解析。

对应设计：docs/design/DD_PROXY.md §7。切换判据依赖这里给出的状态码，解析
错误会退化成 502，从而误判「上游坏了」。
"""

from __future__ import annotations

from r_proxy.protocol.connection import parse_response_head


class TestParseResponseHead:
    def test_a_normal_status_line_is_parsed(self) -> None:
        status, headers = parse_response_head(b"HTTP/1.1 200 OK\r\nServer: nginx\r\n\r\n")
        assert status == 200
        assert headers.get("server") == "nginx"

    def test_extra_spaces_in_the_status_line_do_not_degrade_to_502(self) -> None:
        """单个空格是规范做法，但部分非标实现会连发多个空格；把它当「不像 HTTP」
        比它本身更糟——会让一个正常的响应被误判为上游故障。"""
        status, _ = parse_response_head(b"HTTP/1.1  200  OK\r\n\r\n")
        assert status == 200

    def test_a_missing_status_code_falls_back_to_502(self) -> None:
        status, _ = parse_response_head(b"garbage\r\n\r\n")
        assert status == 502


class TestBuildRequest:
    def test_request_line_forms_by_upstream_type(self) -> None:
        from r_proxy.config.model import UpstreamConfig
        from r_proxy.contracts import AddressFamily, Headers, Method, RequestTarget
        from r_proxy.protocol.connection import build_request
        from r_proxy.protocol.parse import RawHead

        target = RequestTarget(
            host="example.com",
            port=80,
            method=Method.GET,
            url="http://example.com/api/v1",
            is_connect=False,
            family=AddressFamily.IPV4_ONLY,
        )
        head = RawHead(
            method="GET",
            target="http://example.com/api/v1",
            version="HTTP/1.1",
            headers=Headers([("User-Agent", "curl/7.88.1")]),
        )

        http_up = UpstreamConfig(name="http_proxy", type="http", address="127.0.0.1:8080")
        direct_up = UpstreamConfig(name="direct", type="direct", address=None)
        socks5_up = UpstreamConfig(name="socks_proxy", type="socks5", address="127.0.0.1:1080")

        # 1. http 出口必须发 absolute-form
        http_req = build_request(target, head, http_up).decode("latin-1")
        assert http_req.startswith("GET http://example.com/api/v1 HTTP/1.1\r\n")

        # 2. direct 出口必须发 origin-form
        direct_req = build_request(target, head, direct_up).decode("latin-1")
        assert direct_req.startswith("GET /api/v1 HTTP/1.1\r\n")

        # 3. socks5 出口与 direct 一致，必须发 origin-form（端到端 L4 代理）
        socks5_req = build_request(target, head, socks5_up).decode("latin-1")
        assert socks5_req.startswith("GET /api/v1 HTTP/1.1\r\n")


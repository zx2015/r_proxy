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

"""decision/classify.py 的状态码分类与 502/503/504 来源判定测试。

对应设计：docs/design/DD_SWITCHING.md §4、§5
"""

from __future__ import annotations

import pytest

from r_proxy.contracts import Headers
from r_proxy.decision.classify import (
    Origin,
    StatusCategory,
    classify_status,
    determine_origin,
)


def headers(*pairs: tuple[str, str]) -> Headers:
    return Headers(pairs)


class TestClassifyStatus:
    @pytest.mark.parametrize("status", [100, 101, 103])
    def test_1xx_is_informational(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.INFORMATIONAL

    @pytest.mark.parametrize("status", [200, 204, 301, 304, 399])
    def test_2xx_and_3xx_mean_the_target_handled_it(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.TARGET_HANDLED

    @pytest.mark.parametrize(
        "status", [400, 401, 404, 405, 406, 409, 410, 415, 421, 422, 500, 501, 505]
    )
    def test_target_handled_statuses(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.TARGET_HANDLED

    @pytest.mark.parametrize("status", [407, 511])
    def test_proxy_layer_statuses(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.PROXY_LAYER

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_ambiguous_statuses(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.AMBIGUOUS

    @pytest.mark.parametrize("status", [403, 429, 451])
    def test_egress_related_statuses(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.EGRESS_RELATED

    def test_408_is_its_own_category(self) -> None:
        assert classify_status(408) is StatusCategory.INCOMPLETE_REQUEST

    @pytest.mark.parametrize("status", list(range(520, 527)))
    def test_cloudflare_origin_errors(self, status: int) -> None:
        """520–526 证明出口是通的：CDN 连上了，是它到源站那段坏了。"""
        assert classify_status(status) is StatusCategory.CDN_ORIGIN_ERROR

    def test_527_is_not_a_cloudflare_origin_error(self) -> None:
        assert classify_status(527) is not StatusCategory.CDN_ORIGIN_ERROR

    @pytest.mark.parametrize("status", [418, 460, 599, 999])
    def test_unrecognized_statuses_are_unknown(self, status: int) -> None:
        assert classify_status(status) is StatusCategory.UNKNOWN

    def test_cdn_check_precedes_target_handled(self) -> None:
        """520 落在 5xx 区间，但必须先被判为 CDN 才能对抗用户误配。"""
        assert classify_status(520) is StatusCategory.CDN_ORIGIN_ERROR


class TestDetermineOrigin:
    def test_connect_non_2xx_is_always_from_the_proxy(self) -> None:
        """CONNECT 的非 2xx 必然来自上级代理：目标还没参与对话。"""
        assert determine_origin(Headers(), is_connect=True) is Origin.PROXY

    def test_connect_verdict_ignores_misleading_headers(self) -> None:
        origin = determine_origin(headers(("Server", "nginx")), is_connect=True)
        assert origin is Origin.PROXY

    @pytest.mark.parametrize(
        "name", ["X-Squid-Error", "X-Cache", "X-Tinyproxy", "Proxy-Connection"]
    )
    def test_proxy_error_headers_point_at_the_proxy(self, name: str) -> None:
        assert determine_origin(headers((name, "x")), is_connect=False) is Origin.PROXY

    @pytest.mark.parametrize(
        "server", ["squid/5.7", "tinyproxy/1.11", "privoxy", "polipo", "mitmproxy/9"]
    )
    def test_proxy_server_prefixes(self, server: str) -> None:
        origin = determine_origin(headers(("Server", server)), is_connect=False)
        assert origin is Origin.PROXY

    def test_server_prefix_match_is_case_insensitive(self) -> None:
        origin = determine_origin(headers(("Server", "SQUID/5.7")), is_connect=False)
        assert origin is Origin.PROXY

    @pytest.mark.parametrize("via", ["1.1 squid", "1.1 tinyproxy", "1.1 my-proxy"])
    def test_via_tokens_point_at_the_proxy(self, via: str) -> None:
        assert determine_origin(headers(("Via", via)), is_connect=False) is Origin.PROXY

    @pytest.mark.parametrize(
        "server",
        ["nginx/1.24", "Apache/2.4", "cloudflare", "openresty", "gunicorn", "IIS/10", "caddy"],
    )
    def test_target_server_prefixes(self, server: str) -> None:
        origin = determine_origin(headers(("Server", server)), is_connect=False)
        assert origin is Origin.TARGET

    def test_no_signal_is_undetermined(self) -> None:
        assert determine_origin(Headers(), is_connect=False) is Origin.UNDETERMINED

    def test_unrecognized_server_is_undetermined(self) -> None:
        origin = determine_origin(headers(("Server", "my-app/1.0")), is_connect=False)
        assert origin is Origin.UNDETERMINED

    def test_proxy_signal_wins_over_target_signal(self) -> None:
        """代理透传了目标的 Server 头时，代理自己的特征头更可信。"""
        origin = determine_origin(
            headers(("Server", "nginx"), ("X-Squid-Error", "ERR_CONNECT_FAIL")),
            is_connect=False,
        )
        assert origin is Origin.PROXY

    def test_empty_server_header_does_not_match_anything(self) -> None:
        origin = determine_origin(headers(("Server", "")), is_connect=False)
        assert origin is Origin.UNDETERMINED

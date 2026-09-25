"""web/schemas.py 里带行为的模型：目前只有主机流量榜的默认时间窗解析。

对应设计：docs/design/DD_WEB.md §4.2b。
"""

from __future__ import annotations

from datetime import datetime

from r_proxy.web.schemas import HostTrafficQuery


class TestHostTrafficQueryResolvedRange:
    def test_defaults_to_todays_local_midnight_through_now(self) -> None:
        moment = datetime(2026, 9, 24, 15, 30, 0).astimezone()
        since, until = HostTrafficQuery().resolved_range(now=moment)
        midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
        assert since == int(midnight.timestamp())
        assert until == int(moment.timestamp())

    def test_explicit_since_overrides_the_default(self) -> None:
        moment = datetime(2026, 9, 24, 15, 30, 0).astimezone()
        since, until = HostTrafficQuery(since=1000).resolved_range(now=moment)
        assert since == 1000
        assert until == int(moment.timestamp())

    def test_explicit_until_overrides_the_default(self) -> None:
        moment = datetime(2026, 9, 24, 15, 30, 0).astimezone()
        since, until = HostTrafficQuery(until=2_000_000_000).resolved_range(now=moment)
        assert until == 2_000_000_000

    def test_limit_defaults_to_twenty(self) -> None:
        assert HostTrafficQuery().limit == 20

"""0.3.3: upstream failures must never globally exclude an exit."""

from r_proxy.config.model import CircuitBreakerConfig
from r_proxy.contracts import FailureKind
from r_proxy.state.health import HealthTable


def test_repeated_errors_do_not_exclude_an_exit_or_limit_concurrency():
    table = HealthTable(CircuitBreakerConfig(enabled=True, fail_threshold=1))
    for i in range(50):
        table.record_result("proxy", ok=False, kind=FailureKind.UPSTREAM_ERROR, now=float(i))
    assert table.is_available("proxy", now=50)
    assert all(table.acquire_probe("proxy") for _ in range(10))
    assert table.snapshot_of("proxy").total_failure == 50

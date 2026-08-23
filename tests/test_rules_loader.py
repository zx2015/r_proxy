"""规则编译与遮蔽检测：一次收集全部问题，告警不阻断。

对应设计：docs/design/DD_RULES.md §6。验收点 M5-17、M5-18、M5-20。
"""

from __future__ import annotations

from r_proxy.rules.loader import (
    E_CONDITION,
    W_CATCH_ALL,
    W_DUPLICATE,
    W_REDOS,
    W_SHADOWED,
    RuleRow,
    compile_rules,
    load_rules,
)


def rows(*pairs: tuple[str, str]) -> list[RuleRow]:
    return [(i, condition, upstream) for i, (condition, upstream) in enumerate(pairs)]


class FakeSource:
    def __init__(self, *pairs: tuple[str, str]) -> None:
        self._rows = tuple(rows(*pairs))
        self.reads = 0

    def read_rules(self) -> tuple[RuleRow, ...]:
        self.reads += 1
        return self._rows


class TestCompilation:
    def test_position_comes_from_the_row_not_the_loop(self) -> None:
        """位置由库里的 `position` 决定：坏行被跳过时后面的行不能整体前移。"""
        result = compile_rules([(0, "^(unclosed", "a"), (1, "*.example.com", "b")])
        assert [r.position for r in result.rule_set.rules] == [1]

    def test_a_bad_row_does_not_discard_the_good_ones(self) -> None:
        result = compile_rules(rows(("^(unclosed", "a"), ("*.ok.com", "b")))
        assert [r.raw for r in result.rule_set.rules] == ["*.ok.com"]
        assert not result.ok

    def test_all_errors_are_reported_at_once(self) -> None:
        """用户改规则时希望一次看到所有问题，界面据此高亮多行。"""
        result = compile_rules(
            rows(("[2001:db8::zz]", "a"), ("^(unclosed", "a"), ("ok.com:8443", "a"))
        )
        assert [i.location for i in result.errors] == ["rules[0]", "rules[1]", "rules[2]"]
        assert {i.code for i in result.errors} == {E_CONDITION}

    def test_empty_input_is_valid(self) -> None:
        result = compile_rules([])
        assert result.ok
        assert result.rule_set.is_empty

    def test_targets_point_at_the_first_occurrence(self) -> None:
        result = compile_rules(rows(("*.a.com", "proxy-a"), ("*.b.com", "direct")))
        assert result.rule_set.targets() == {"proxy-a": "rules[0]", "direct": "rules[1]"}

    def test_redos_warns_but_keeps_the_rule(self) -> None:
        result = compile_rules(rows(("^(a+)+$", "direct")))
        assert result.ok
        assert [i.code for i in result.issues] == [W_REDOS]
        assert len(result.rule_set.rules) == 1


class TestShadowing:
    def test_catch_all_warns_on_itself(self) -> None:
        result = compile_rules(rows(("*", "direct")))
        assert [(i.code, i.location) for i in result.issues] == [(W_CATCH_ALL, "rules[0]")]

    def test_everything_after_catch_all_is_shadowed(self) -> None:
        """M5-17：首匹配胜出，`*` 之后的规则永远轮不到。"""
        result = compile_rules(rows(("*", "direct"), ("*.a.com", "proxy-a")))
        assert [(i.code, i.location) for i in result.issues] == [
            (W_CATCH_ALL, "rules[0]"),
            (W_SHADOWED, "rules[1]"),
        ]

    def test_duplicate_points_at_the_later_one(self) -> None:
        """M5-17：顺序语义反转后，不生效的是**后**出现的那条。"""
        result = compile_rules(rows(("*.example.com", "proxy-a"), ("*.example.com", "direct")))
        assert [(i.code, i.location) for i in result.issues] == [(W_DUPLICATE, "rules[1]")]
        assert "rules[0]" in result.issues[0].message

    def test_ipv6_written_two_ways_counts_as_a_duplicate(self) -> None:
        result = compile_rules(rows(("[2001:db8::1]", "a"), ("[2001:0db8:0:0:0:0:0:1]", "b")))
        assert [i.code for i in result.issues] == [W_DUPLICATE]

    def test_suffix_shadows_a_more_specific_host_beneath_it(self) -> None:
        result = compile_rules(rows(("*.example.com", "proxy-a"), ("api.example.com", "direct")))
        assert [(i.code, i.location) for i in result.issues] == [(W_SHADOWED, "rules[1]")]

    def test_suffix_shadows_a_narrower_suffix(self) -> None:
        result = compile_rules(rows(("*.example.com", "a"), ("*.api.example.com", "b")))
        assert [i.code for i in result.issues] == [W_SHADOWED]

    def test_a_more_specific_rule_placed_first_is_fine(self) -> None:
        """这正是首匹配胜出下的正确写法，不该有任何告警。"""
        result = compile_rules(rows(("api.example.com", "direct"), ("*.example.com", "proxy-a")))
        assert result.issues == []

    def test_unrelated_suffixes_do_not_shadow(self) -> None:
        result = compile_rules(rows(("*.example.com", "a"), ("notexample.com", "b")))
        assert result.issues == []

    def test_ip_literal_is_not_shadowed_by_a_suffix(self) -> None:
        """匹配器对 IP 走地址比较分支，`*.1.10` 不可能命中 192.168.1.10。"""
        result = compile_rules(rows(("*.1.10", "a"), ("192.168.1.10", "b")))
        assert result.issues == []

    def test_regex_shadowing_is_not_reported(self) -> None:
        """M5-18：正则包含关系不可判定，宁可漏报也不误报。"""
        result = compile_rules(rows((r"^.*\.example\.com$", "a"), ("api.example.com", "b")))
        assert result.issues == []

    def test_wildcard_shadowing_is_not_reported(self) -> None:
        result = compile_rules(rows(("*.example.*", "a"), ("api.example.com", "b")))
        assert result.issues == []


class TestLoad:
    def test_disabled_does_not_touch_the_database(self) -> None:
        """M5-20：救场开关下连库都不打开——那个库可能正是坏的那个。"""
        source = FakeSource(("*", "direct"))
        result = load_rules(source, enabled=False)
        assert source.reads == 0
        assert result.rule_set.is_empty
        assert result.ok

    def test_enabled_reads_and_compiles(self) -> None:
        source = FakeSource(("*.example.com", "direct"))
        result = load_rules(source, enabled=True)
        assert source.reads == 1
        assert [r.raw for r in result.rule_set.rules] == ["*.example.com"]

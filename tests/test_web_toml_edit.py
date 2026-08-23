"""配置文本的风格保留编辑。

对应设计：docs/design/DD_WEB.md §6.2.1，需求：WEBUI_SPEC.md §6.2.2。

这一层的价值全在「没被改的东西一个字节都没动」。因此断言大多是对原文片段的
逐字比对，而不是「解析回来看值对不对」——后者对丢注释、丢空行完全无感。
"""

from __future__ import annotations

import tomllib

import pytest

from r_proxy.web.toml_edit import (
    TomlEditError,
    apply_settings,
    remove_upstream,
    set_priorities,
    upsert_upstream,
)

ORIGINAL = """\
# r-proxy 配置
# 第二行注释

[listen]
host = "127.0.0.1"
port = 6060          # 代理端口

[routing]
connect_timeout = 10.0
# 下面这些码会触发切换
switch_on_status = [403, 502]

# 家里的代理
[[upstreams]]
name = "home"
type = "http"
address = "198.51.100.100:7890"
priority = 10

[upstreams.auth]
username = "alice"
password = "s3cr3t"

# 兜底直连
[[upstreams]]
name = "direct"
type = "direct"
priority = 100
"""


class TestApplySettings:
    def test_comments_and_blank_lines_survive(self) -> None:
        """朴素的「解析成字典再全量序列化」会丢掉这些，用户手写的说明就永久消失。"""
        out = apply_settings(ORIGINAL, {"routing.connect_timeout": 5.0})
        assert "# r-proxy 配置" in out
        assert "# 下面这些码会触发切换" in out
        assert "port = 6060          # 代理端口" in out

    def test_only_the_named_key_changes(self) -> None:
        out = apply_settings(ORIGINAL, {"routing.connect_timeout": 5.0})
        assert "connect_timeout = 5.0" in out
        assert "connect_timeout = 10.0" not in out
        # 未指名的项不该被显式写出来，否则文件会被默认值撑大。
        assert "read_timeout" not in out

    def test_a_missing_intermediate_table_is_created(self) -> None:
        out = apply_settings(ORIGINAL, {"database.retention_days": 7})
        assert tomllib.loads(out)["database"]["retention_days"] == 7

    def test_a_created_table_is_not_inline(self) -> None:
        """行内表虽然合法，但与文件其余部分的风格不一致。"""
        out = apply_settings(ORIGINAL, {"database.retention_days": 7})
        assert "[database]" in out

    def test_nested_tables_are_reachable(self) -> None:
        out = apply_settings(ORIGINAL, {"routing.circuit_breaker.fail_threshold": 9})
        assert tomllib.loads(out)["routing"]["circuit_breaker"]["fail_threshold"] == 9

    def test_a_list_value_round_trips(self) -> None:
        out = apply_settings(ORIGINAL, {"routing.switch_on_status": [403, 429]})
        assert tomllib.loads(out)["routing"]["switch_on_status"] == [403, 429]


class TestUpsertUpstream:
    def test_updating_keeps_the_credentials_subtable(self) -> None:
        """改优先级不该顺手抹掉 `[upstreams.auth]`。"""
        out = upsert_upstream(ORIGINAL, name="home", fields={"priority": 20})
        data = tomllib.loads(out)
        home = next(u for u in data["upstreams"] if u["name"] == "home")
        assert home["priority"] == 20
        assert home["auth"] == {"username": "alice", "password": "s3cr3t"}

    def test_updating_keeps_the_leading_comment(self) -> None:
        out = upsert_upstream(ORIGINAL, name="home", fields={"priority": 20})
        assert "# 家里的代理" in out

    def test_a_new_upstream_is_appended(self) -> None:
        out = upsert_upstream(
            ORIGINAL,
            name="office",
            fields={"type": "http", "address": "10.0.0.1:8080", "priority": 30},
        )
        names = [u["name"] for u in tomllib.loads(out)["upstreams"]]
        assert names == ["home", "direct", "office"]

    def test_a_new_upstream_does_not_steal_the_next_sections_comment(self) -> None:
        """`aot.append()` 会把新表插到下一个章节的注释之后，让它顶着别人的说明。"""
        text = (
            '[[upstreams]]\nname = "home"\n\n# 这段注释属于 routing\n'
            "[routing]\nconnect_timeout = 10\n"
        )
        out = upsert_upstream(text, name="office", fields={"type": "direct"})
        assert out.index('name = "office"') < out.index("# 这段注释属于 routing")
        assert [u["name"] for u in tomllib.loads(out)["upstreams"]] == ["home", "office"]

    def test_a_hostile_name_cannot_inject_toml(self) -> None:
        """``name`` 来自请求体。字符串拼接会让它变成攻击者可控的 TOML 片段。"""
        out = upsert_upstream(ORIGINAL, name='evil"\nport = 9999\nx = "', fields={"type": "direct"})
        data = tomllib.loads(out)
        assert "port" not in data
        assert data["upstreams"][-1]["name"] == 'evil"\nport = 9999\nx = "'

    def test_an_unknown_field_is_refused(self) -> None:
        """写进去之后加载器会以 E_UNKNOWN_KEY 拒绝启动，必须在这里挡住。"""
        with pytest.raises(TomlEditError):
            upsert_upstream(ORIGINAL, name="home", fields={"backdoor": 1})

    def test_a_none_value_removes_the_key(self) -> None:
        """`direct` 没有地址。留一个空字符串会被当成配错了地址的上级代理。"""
        out = upsert_upstream(ORIGINAL, name="home", fields={"address": None})
        home = next(u for u in tomllib.loads(out)["upstreams"] if u["name"] == "home")
        assert "address" not in home

    def test_appending_to_a_file_without_upstreams(self) -> None:
        out = upsert_upstream("[listen]\nport = 6060\n", name="direct", fields={"type": "direct"})
        assert tomllib.loads(out)["upstreams"] == [{"name": "direct", "type": "direct"}]


class TestRemoveUpstream:
    def test_the_table_and_its_leading_comment_go_together(self) -> None:
        """注释描述的就是这个出口，留着会指向一个已不存在的条目。"""
        out = remove_upstream(ORIGINAL, "home")
        assert "# 家里的代理" not in out
        assert [u["name"] for u in tomllib.loads(out)["upstreams"]] == ["direct"]

    def test_the_next_upstream_keeps_its_own_comment(self) -> None:
        """`tomlkit` 的 `del aot[i]` 会让注释错位一格，`direct` 顶上「家里的代理」。"""
        out = remove_upstream(ORIGINAL, "home")
        assert "# 兜底直连" in out
        assert out.index("# 兜底直连") < out.index('name = "direct"')

    def test_the_other_upstreams_are_untouched(self) -> None:
        out = remove_upstream(ORIGINAL, "home")
        assert "# r-proxy 配置" in out
        assert "# 下面这些码会触发切换" in out

    def test_removing_the_last_upstream_spares_the_next_section(self) -> None:
        """表后面的注释可能属于另一个章节，删表不能顺手吃掉它。"""
        text = (
            '[[upstreams]]\nname = "home"\npriority = 10\n\n'
            "# 这段注释属于 routing\n[routing]\nconnect_timeout = 10\n"
        )
        out = remove_upstream(text, "home")
        assert "# 这段注释属于 routing" in out
        assert tomllib.loads(out) == {"routing": {"connect_timeout": 10}}

    def test_a_separated_comment_block_is_not_swallowed(self) -> None:
        """隔了空行的注释是章节分隔，不是这个出口的说明。"""
        text = '# 出口列表\n\n[[upstreams]]\nname = "home"\n\n[[upstreams]]\nname = "direct"\n'
        out = remove_upstream(text, "home")
        assert "# 出口列表" in out

    def test_removing_the_credentials_subtable_too(self) -> None:
        out = remove_upstream(ORIGINAL, "home")
        assert "s3cr3t" not in out

    def test_an_unknown_name_raises(self) -> None:
        with pytest.raises(TomlEditError):
            remove_upstream(ORIGINAL, "ghost")


class TestSetPriorities:
    def test_all_values_are_rewritten(self) -> None:
        out = set_priorities(ORIGINAL, {"home": 10, "direct": 20})
        data = {u["name"]: u["priority"] for u in tomllib.loads(out)["upstreams"]}
        assert data == {"home": 10, "direct": 20}

    def test_an_unknown_name_leaves_the_text_alone(self) -> None:
        """批量更新必须整体成败：中途出错时调用方拿不到任何文本，也就写不出去。"""
        with pytest.raises(TomlEditError):
            set_priorities(ORIGINAL, {"home": 50, "ghost": 60})

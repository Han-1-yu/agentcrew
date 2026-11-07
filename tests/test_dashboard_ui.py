"""R32：控制台 UI 的契约护栏——令牌化、明暗主题、侧栏布局与关键 id。

重构 UI 最怕「看起来更漂亮了，但某个 id 被改名导致功能静默失效」。这里用纯字符串
断言把几条硬约束钉住：颜色只能出现在 :root 令牌表里、明暗两套令牌齐全、
六个面板与关键控件 id 都还在、主题切换函数存在。
"""

from __future__ import annotations

import re

from agentcrew.api.dashboard import DASHBOARD_HTML

#: 页面必须存在的 id（JS 按 id 取元素，改名就是功能失效）。
REQUIRED_IDS = (
    "conn-status", "run-id", "btn-delete", "run-status", "model-alias",
    "model-role", "model-current", "run-list", "ledger-event-filter",
    "stats-panel", "tools-list", "tool-trial-args", "keys-list",
    "flow-canvas", "flow-props", "flow-yaml",
)

#: 六个面板（switchTab 的 name 与 pane-<name> 必须对齐）。
PANES = ("board", "ledger", "stats", "tools", "keys", "flow")


def test_required_ids_present():
    missing = [name for name in REQUIRED_IDS if f'id="{name}"' not in DASHBOARD_HTML]
    assert not missing, f"看板缺少 id：{missing}"


def test_all_panes_and_nav_entries_present():
    for name in PANES:
        assert f'id="pane-{name}"' in DASHBOARD_HTML
        assert f"switchTab('{name}', this)" in DASHBOARD_HTML


def test_dark_and_light_token_tables_are_complete():
    """两个主题的令牌集合必须一致——漏一个就会在切换后变成「隐形元素」。"""
    def tokens(block: str) -> set[str]:
        return set(re.findall(r"(--[a-z0-9-]+):", block))

    dark_start = DASHBOARD_HTML.index(":root {")
    dark = DASHBOARD_HTML[dark_start:DASHBOARD_HTML.index(":root[data-theme=\"light\"]")]
    light_start = DASHBOARD_HTML.index(':root[data-theme="light"]')
    light = DASHBOARD_HTML[light_start:DASHBOARD_HTML.index("* { box-sizing")]
    dark_tokens, light_tokens = tokens(dark), tokens(light)
    assert dark_tokens, "暗色令牌表为空"
    assert dark_tokens - {"--mono", "--radius", "--radius-sm"} == \
        light_tokens - {"--mono", "--radius", "--radius-sm"}


def test_theme_toggle_wired():
    assert "function toggleTheme()" in DASHBOARD_HTML
    assert "agentcrew-theme" in DASHBOARD_HTML          # localStorage 记忆
    assert "prefers-color-scheme: light" in DASHBOARD_HTML  # 默认跟随系统
    assert 'onclick="toggleTheme()"' in DASHBOARD_HTML


def test_no_hardcoded_colours_outside_tokens():
    """除令牌表与 flowToken 的兜底值外，页面不应再出现写死的十六进制颜色。"""
    body = DASHBOARD_HTML
    tokens_block = body[body.index(":root {"):body.index("* { box-sizing")]
    rest = body.replace(tokens_block, "")
    # flowToken("--accent", "#58a6ff") 这类兜底值允许存在
    rest = re.sub(r'flowToken\("[^"]+",\s*"#[0-9a-fA-F]{3,6}"\)', "", rest)
    offenders = sorted(set(re.findall(r"#[0-9a-fA-F]{6}\b", rest)))
    assert not offenders, f"还有写死的颜色（应改用 var(--token)）：{offenders}"


def test_sidebar_layout_structure():
    assert 'class="shell"' in DASHBOARD_HTML
    assert 'class="sidebar"' in DASHBOARD_HTML
    assert 'class="main"' in DASHBOARD_HTML
    assert DASHBOARD_HTML.count('<aside class="sidebar">') == 1
    assert "<main" in DASHBOARD_HTML and "</main>" in DASHBOARD_HTML

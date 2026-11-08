"""`docs/THREAT_MODEL.md` 的自检（P3）。

为什么给文档写测试：威胁模型最容易烂掉的方式不是「写错」，而是**慢慢与现实脱节**——
引用的测试文件被删了、缺口编号被改了一半、上线清单忘记带上最高风险项。这些都不会
让程序报错，只会让下一个评审的人以为「有证据」。

所以这里钉四条：

1. 必需章节在（不是「有文件就行」）；
2. 文档里点名的每个 `tests/test_*.py` **真实存在**——「每条控制都能指到测试」这句话
   必须是可验证的，否则就是空话；
3. 威胁编号 `T-xx` 连续无空洞、缺口编号 `G-x` 不重复，且正文提到的 G 编号都在缺口表里
   （避免「见 G-7」指向一个已经不存在的条目）；
4. 上线清单里仍然包含三条最高风险缺口（G-1 提示词注入 / G-2 配置即 RCE /
   G-9 run_python 无隔离）——它们的补偿措施不该被悄悄从清单里拿掉。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "THREAT_MODEL.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert DOC.is_file(), "docs/THREAT_MODEL.md 不存在"
    return DOC.read_text(encoding="utf-8")


def test_required_sections_present(text: str) -> None:
    """九个章节缺一不可——少了哪一节，文档就不再能当评审依据用。"""
    for heading in (
        "## 1. 这份文档怎么用",
        "## 2. 系统模型",
        "## 3. 资产",
        "## 4. 行为者与能力假设",
        "## 5. 威胁与控制",
        "## 6. 已知缺口与风险接受",
        "## 7. 控制 → 证据映射",
        "## 8. 上线前必须确认",
        "## 9. 维护规则",
    ):
        assert heading in text, f"缺少章节：{heading}"


def test_untrusted_input_table_lists_model_output(text: str) -> None:
    """B4（模型输出）是本类系统特有的边界，不可信输入清单必须点到它。

    这条断言防的是「把威胁模型写成普通 Web 服务的翻版」——那正是最容易漏掉
    提示词注入的写法。
    """
    assert "### 2.3 不可信输入清单" in text
    assert "模型发起的 `tool_calls`" in text
    section = text.split("### 2.3 不可信输入清单", 1)[1].split("## 3.", 1)[0]
    assert "完全不可信" in section or "**不可信**" in section


def test_referenced_test_files_exist(text: str) -> None:
    """文档点名的每个测试文件都必须真实存在。

    这是「控制 → 证据」这条链里唯一能被机器检查的一环：控制写没写对要人看，
    但「证据在不在」机器能看。以前出现过文档写着 `test_web_fetch.py`、实际那是
    `test_new_builtin_tools.py` 的情况——名字错了，链接就断了。
    """
    referenced = sorted(set(re.findall(r"tests/test_[A-Za-z0-9_]+\.py", text)))
    assert referenced, "文档里至少要引用一个测试文件"
    missing = [name for name in referenced if not (ROOT / name).is_file()]
    assert not missing, f"文档引用了不存在的测试文件：{missing}"


#: 威胁条目行：``| **T-01** | 场景 | ...``
_THREAT_ROW = re.compile(r"^\|\s*\*\*(T-\d{2})\*\*\s*\|", re.MULTILINE)
#: 缺口条目行：``| **G-1** | ...``
_GAP_ROW = re.compile(r"^\|\s*\*\*(G-\d+)\*\*\s*\|", re.MULTILINE)


def test_threat_ids_are_unique_and_contiguous(text: str) -> None:
    """T-01…T-NN 连续无空洞：漏号通常意味着某条威胁被删了却没留下替代。"""
    ids = _THREAT_ROW.findall(text)
    assert ids, "没有解析到任何威胁条目"
    assert len(ids) == len(set(ids)), f"威胁编号重复：{ids}"
    numbers = sorted(int(item[2:]) for item in ids)
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"威胁编号不连续：{numbers}")


def test_gap_ids_unique_and_all_references_resolve(text: str) -> None:
    """正文里「见 G-x」必须真的指向缺口表里的条目。"""
    defined = _GAP_ROW.findall(text)
    assert len(defined) >= 10, f"缺口表条目过少（{len(defined)}），是否被删减？"
    assert len(defined) == len(set(defined)), f"缺口编号重复：{defined}"

    known = set(defined)
    referenced = {f"G-{number}" for number in re.findall(r"G-(\d+)", text)}
    dangling = sorted(referenced - known)
    assert not dangling, f"正文引用了不存在的缺口编号：{dangling}"


def test_every_gap_has_compensation_or_trigger(text: str) -> None:
    """每个缺口都要写「为什么接受」和「补偿/触发条件」。

    只有「影响」没有「补偿」的缺口等于没评估过——上线时没人知道该做什么。
    """
    section = text.split("## 6. 已知缺口与风险接受", 1)[1].split("## 7.", 1)[0]
    rows = [
        line for line in section.splitlines()
        if _GAP_ROW.match(line)
    ]
    assert rows
    for line in rows:
        gap = _GAP_ROW.match(line).group(1)
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        assert len(cells) >= 5, f"{gap} 列数不足（缺补偿措施）：{line[:60]}"
        compensation = cells[-1]
        assert len(compensation) > 10, f"{gap} 的补偿措施太笼统：{compensation!r}"


def test_launch_checklist_keeps_top_risks(text: str) -> None:
    """上线清单必须仍点名三条最高风险缺口：G-1 / G-2 / G-9。"""
    section = text.split("## 8. 上线前必须确认", 1)[1].split("## 9.", 1)[0]
    for gap in ("G-1", "G-2", "G-9"):
        assert gap in section, f"上线清单里缺少 {gap}（最高风险项不该被静默移除）"


def test_defaults_stated_in_doc_match_code(text: str) -> None:
    """文档里写死的安全默认值必须与代码一致。

    只查最容易「文档说关、代码改开」的几项。用 `Settings.model_validate({})` 而不是
    `Settings.load()`：后者会读 `.env` 与 `agentcrew.yaml`，在别人机器上可能是另一个值，
    断言就变成了环境测试。
    """
    from agentcrew.config import Settings

    settings = Settings.model_validate({})
    if settings.api_auth_enabled:
        assert "`api_auth_enabled` 默认 `True`" in text
    if not settings.code_exec_enabled:
        assert "`code_exec_enabled=false`" in text
    if not settings.otel_capture_content:
        assert "`otel_capture_content` **默认 `false`**" in text
    if not settings.api_rate_limit_enabled:
        assert "**限流默认关**" in text

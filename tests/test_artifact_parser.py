"""围栏块产物解析测试（spec §9.1）。

``parse_fenced_blocks`` 的输入是**不可信的模型回复**，敌对与畸形是常态而非
例外。除 brief 给定的用例外，这里把每条边界行为都钉死：能落盘 / 进 notes /
报错三种结局必须可预期，不能靠巧合。
"""

from typing import get_args

import pytest
from pydantic import ValidationError

from agentcrew.artifacts.parser import (
    LANG_TO_KIND,
    ArtifactParseError,
    ParsedOutput,
    normalize_lang,
    parse_fenced_blocks,
)
from agentcrew.artifacts.spec import ArtifactKind


# ── brief 指定的用例 ──────────────────────────────────────────────


def test_parses_single_file_block():
    raw = '说明文字\n\n```python file=backend/app/main.py\nprint(1)\n```\n\n尾部说明'
    out = parse_fenced_blocks(raw)
    assert out.files == {"backend/app/main.py": "print(1)\n"}
    assert "说明文字" in out.notes and "尾部说明" in out.notes


def test_parses_multiple_blocks():
    raw = ('```python file=a.py\nx=1\n```\n'
           '```markdown file=docs/README.md\n# hi\n```\n')
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"a.py", "docs/README.md"}
    assert out.files["docs/README.md"] == "# hi\n"


def test_block_without_file_is_note_not_written():
    raw = '```python\nprint("example")\n```\n```python file=a.py\nx=1\n```'
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"a.py"}
    assert "example" in out.notes


def test_unclosed_block_raises():
    with pytest.raises(ArtifactParseError):
        parse_fenced_blocks("```python file=a.py\nx=1\n")


def test_duplicate_file_takes_last_and_notes_warning():
    raw = '```python file=a.py\nold\n```\n```python file=a.py\nnew\n```'
    out = parse_fenced_blocks(raw)
    assert out.files["a.py"] == "new\n"
    assert "a.py" in out.notes and "重复" in out.notes


def test_backslash_paths_normalized():
    out = parse_fenced_blocks("```python file=backend\\app\\main.py\nx=1\n```")
    assert set(out.files) == {"backend/app/main.py"}


def test_no_blocks_returns_empty_files_and_full_note():
    out = parse_fenced_blocks("just prose, no code")
    assert out.files == {} and "just prose" in out.notes


def test_four_backtick_fence_is_not_confused():
    """4 反引号开块、只有 3 反引号收尾 → 3 < 4 不算闭合 → 未闭合报错。

    brief 原始的 raw 以 **4** 反引号收尾，按 CommonMark「≥ 开栏数才闭合」已经是
    合法块（见 ``test_four_backtick_block_with_matching_close_parses``）；这里把
    收尾改成 3 反引号，保住该用例原本要保护的属性：畸形的围栏绝不被静默吞掉。
    """
    raw = '````python file=a.py\nx=1\n```'
    with pytest.raises(ArtifactParseError, match="未闭合的代码块，起始行 1"):
        parse_fenced_blocks(raw)


# ── Review Focus #3：非 ASCII 路径 ──
def test_chinese_file_path_parses():
    out = parse_fenced_blocks("```markdown file=docs/接口文档.md\n# 接口\n```")
    assert set(out.files) == {"docs/接口文档.md"}
    assert out.files["docs/接口文档.md"] == "# 接口\n"


def test_path_with_spaces_parses():
    out = parse_fenced_blocks("```text file=docs/my notes.txt\nhi\n```")
    assert set(out.files) == {"docs/my notes.txt"}


@pytest.mark.parametrize("lang,kind", [
    ("py", "python"), ("python", "python"), ("yml", "yaml"), ("yaml", "yaml"),
    ("md", "markdown"), ("markdown", "markdown"), ("js", "js"),
    ("html", "html"), ("css", "css"), ("json", "json"), ("sh", "shell"),
    ("toml", "toml"), ("sql", "sql"), ("dockerfile", "dockerfile"),
])
def test_lang_normalization(lang, kind):
    assert normalize_lang(lang) == kind


def test_unknown_lang_returns_none():
    assert normalize_lang("brainfuck") is None


# ── 语言名表与 ArtifactKind 对齐 ─────────────────────────────────


def test_lang_table_values_cover_exactly_the_twelve_kinds():
    """表里的值必须恰好是 ``ArtifactKind`` 的 12 个取值（Task 12 会依赖）。"""
    kinds = set(get_args(ArtifactKind))
    assert len(kinds) == 12
    assert set(LANG_TO_KIND.values()) == kinds


@pytest.mark.parametrize("lang,kind", [
    ("PY", "python"), ("Markdown", "markdown"), ("YML", "yaml"),
    ("DockerFile", "dockerfile"), ("  sql  ", "sql"),
])
def test_normalize_lang_ignores_case_and_surrounding_blank(lang, kind):
    assert normalize_lang(lang) == kind


@pytest.mark.parametrize("lang,kind", [
    ("txt", "text"), ("text", "text"), ("javascript", "js"),
    ("bash", "shell"), ("zsh", "shell"), ("htm", "html"),
])
def test_lang_alias_table_covers_common_spellings(lang, kind):
    assert normalize_lang(lang) == kind


@pytest.mark.parametrize("lang", ["", "   ", "brainfuck", "text/plain", "ts", "scss"])
def test_unknown_language_names_fall_back_to_the_contract(lang):
    """没有对应 ``ArtifactKind`` 的语言名一律 ``None``：由调用方按契约兜底。"""
    assert normalize_lang(lang) is None


# ── 空输入与结尾 ─────────────────────────────────────────────────


def test_empty_input_returns_empty_result():
    assert parse_fenced_blocks("") == ParsedOutput(files={}, notes="")


def test_whitespace_only_input_yields_empty_note():
    out = parse_fenced_blocks("\n")
    assert out.files == {} and out.notes == ""


def test_reply_without_trailing_newline_parses():
    out = parse_fenced_blocks("```python file=a.py\nx=1\n```")
    assert out.files == {"a.py": "x=1\n"}


def test_lone_trailing_newline_after_close_fence_is_ignored():
    out = parse_fenced_blocks("```python file=a.py\nx=1\n```\n")
    assert out.files == {"a.py": "x=1\n"}


def test_crlf_reply_is_parsed_and_lf_normalized():
    out = parse_fenced_blocks("```python file=a.py\r\nx=1\r\ny=2\r\n```\r\n")
    assert out.files == {"a.py": "x=1\ny=2\n"}


def test_lone_cr_line_endings_are_accepted():
    out = parse_fenced_blocks("说明\r```python file=a.py\rx=1\r```")
    assert out.files == {"a.py": "x=1\n"}
    assert "说明" in out.notes and "\r" not in out.notes


def test_empty_block_body_yields_empty_content():
    out = parse_fenced_blocks("```python file=a.py\n```")
    assert out.files == {"a.py": ""}


def test_blank_lines_inside_body_are_preserved():
    out = parse_fenced_blocks("```python file=a.py\nx=1\n\ny=2\n```")
    assert out.files == {"a.py": "x=1\n\ny=2\n"}


# ── 围栏识别 ─────────────────────────────────────────────────────


def test_close_fence_with_surrounding_blank_is_accepted():
    out = parse_fenced_blocks("```python file=a.py\nx=1\n   ```  ")
    assert out.files == {"a.py": "x=1\n"}


def test_fence_line_with_language_never_closes_a_block():
    """块内出现带语言名的围栏行只是正文：畸形输入构造不出嵌套围栏。"""
    raw = "```python file=a.py\n```python file=b.py\n```"
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "```python file=b.py\n"}


def test_bare_fence_in_body_closes_the_block_early():
    """正文里裸的一行三反引号会提前闭合：这是本协议的代价，随后的裸围栏自配对。"""
    raw = "```python file=a.py\nx=1\n```\ny=2\n```\nz\n```"
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "x=1\n"}
    assert "y=2" in out.notes and "z" in out.notes
    assert "x=1" not in out.notes


def test_indented_start_fence_is_prose():
    """起始围栏必须顶格（正则锚定行首）：缩进的整段按说明处理，不报错也不落盘。"""
    raw = "  ```python file=a.py\n  x=1\n  ```"
    out = parse_fenced_blocks(raw)
    assert out.files == {}
    assert "x=1" in out.notes


def test_lone_fence_without_newline_is_an_unclosed_block():
    with pytest.raises(ArtifactParseError, match="起始行 1"):
        parse_fenced_blocks("```")


def test_unclosed_error_names_the_opening_line():
    with pytest.raises(ArtifactParseError, match="起始行 3"):
        parse_fenced_blocks("第一行\n\n```python file=a.py\nx=1\n")


# ── CommonMark 围栏语义：N 反引号的块由「≥ N 个裸反引号」的一行闭合 ──


def test_four_backtick_block_may_contain_three_backtick_examples():
    """README/docs 的核心场景：4 反引号块内嵌 3 反引号示例，整块存活为一个产物。"""
    raw = (
        "````markdown file=README.md\n"
        "# Todo\n"
        "\n"
        "```bash\n"
        "uvicorn app.main:app --reload\n"
        "```\n"
        "````\n"
    )
    out = parse_fenced_blocks(raw)
    assert out.files == {
        "README.md": "# Todo\n\n```bash\nuvicorn app.main:app --reload\n```\n"
    }
    assert out.notes == ""


def test_three_backtick_block_still_closes_at_the_first_bare_three_backtick_line():
    """3 反引号块的行为不变：第一个裸的 3 反引号行就闭合，容不下裸围栏行。"""
    raw = "```markdown file=README.md\n# Todo\n```\n说明"
    out = parse_fenced_blocks(raw)
    assert out.files == {"README.md": "# Todo\n"}
    assert out.notes == "说明"


def test_three_backtick_line_does_not_close_a_four_backtick_block():
    raw = "````markdown file=README.md\n# T\n```\nmore\n````\n"
    out = parse_fenced_blocks(raw)
    assert out.files == {"README.md": "# T\n```\nmore\n"}
    assert out.notes == ""


def test_four_backtick_block_is_closed_by_a_longer_fence():
    """闭栏只要**不短于**开栏即可：4 开、5 闭。"""
    raw = "````python file=a.py\nx=1\n`````\n"
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "x=1\n"}
    assert out.notes == ""


@pytest.mark.parametrize("ticks", ["````", "`````", "``````"])
def test_any_fence_length_of_three_or_more_is_supported(ticks):
    raw = f"{ticks}python file=a.py\n```\nx=1\n{ticks}\n"
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "```\nx=1\n"}
    assert out.notes == ""


def test_four_backtick_block_with_matching_close_parses():
    """brief 最初的 raw（4 开 4 闭）：新规则下是合法块 —— 记录这处行为变更。"""
    out = parse_fenced_blocks('````python file=a.py\nx=1\n````')
    assert out.files == {"a.py": "x=1\n"}
    assert out.notes == ""


def test_longer_bare_fence_closes_a_shorter_block():
    """新规则的另一面：3 反引号块遇到更长的裸围栏行同样闭合（与 CommonMark 一致）。"""
    out = parse_fenced_blocks("```python file=a.py\nx=1\n````\ny=2")
    assert out.files == {"a.py": "x=1\n"}
    assert out.notes == "y=2"


def test_bare_two_backtick_line_neither_opens_nor_closes():
    """少于 3 个反引号的两行反引号不是围栏：开不了块，也闭不了块。"""
    out = parse_fenced_blocks("```python file=a.py\nx=1\n``\ny=2\n```")
    assert out.files == {"a.py": "x=1\n``\ny=2\n"}


# ── 路径的原样提取（安全校验不在这里） ───────────────────────────


def test_empty_file_value_is_treated_as_no_file():
    """``file=`` 后没有值：视同没有 ``file=``，正文进 notes，不落盘也不报错。"""
    out = parse_fenced_blocks("```python file=\nx=1\n```")
    assert out.files == {}
    assert "x=1" in out.notes


def test_blank_only_file_value_is_treated_as_no_file():
    out = parse_fenced_blocks("```python file=   \nx=1\n```")
    assert out.files == {}
    assert "x=1" in out.notes


def test_second_file_attribute_becomes_part_of_the_path():
    """一行里两个 ``file=``：整段尾串都算路径（允许空格/中文的必然后果）。

    这样的路径会在 Task 11 的契约校验处被判为未声明产物 —— 解析器不替它猜。
    """
    out = parse_fenced_blocks("```python file=a.py file=b.py\nx=1\n```")
    assert set(out.files) == {"a.py file=b.py"}


def test_backticks_inside_path_are_captured_verbatim():
    out = parse_fenced_blocks("```python file=a```b.py\nx=1\n```")
    assert set(out.files) == {"a```b.py"}


def test_path_surrounding_blank_is_stripped_but_inner_spaces_kept():
    out = parse_fenced_blocks("```python file=  docs/a b.txt  \nx=1\n```")
    assert set(out.files) == {"docs/a b.txt"}


def test_parent_traversal_path_is_passed_through_untouched():
    """解析器只做忠实提取：``../`` 由 Task 11 的 ArtifactStore 拒收。"""
    out = parse_fenced_blocks("```python file=../evil.py\nx=1\n```")
    assert set(out.files) == {"../evil.py"}


def test_unknown_language_still_yields_the_artifact():
    """语言名不认识不阻塞落盘：契约校验在 Task 11，语言名只用于兜底。"""
    out = parse_fenced_blocks("```brainfuck file=a.py\nx=1\n```")
    assert out.files == {"a.py": "x=1\n"}


# ── notes 的顺序、去重告警与不泄漏 ───────────────────────────────


def test_notes_preserve_document_order_across_blocks():
    raw = ("开头\n"
           "```python\n示例\n```\n"
           "中间\n"
           "```python file=a.py\nx=1\n```\n"
           "```text\n纯样例\n```\n"
           "结尾")
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"a.py"}
    positions = [out.notes.index(f) for f in ("开头", "示例", "中间", "纯样例", "结尾")]
    assert positions == sorted(positions)


def test_artifact_body_does_not_leak_into_notes():
    raw = "说明\n```python file=a.py\nsecret = 1\n```\n"
    out = parse_fenced_blocks(raw)
    assert "secret" not in out.notes
    assert out.notes.startswith("说明")


def test_duplicate_warning_is_emitted_once_per_path():
    raw = "".join(f"```python file=a.py\n{v}\n```\n" for v in ("one", "two", "three"))
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "three\n"}
    assert out.notes.count("重复") == 1


def test_duplicate_warnings_follow_first_duplication_order():
    raw = ("```python file=a.py\n1\n```\n```python file=b.py\n1\n```\n"
           "```python file=a.py\n2\n```\n```python file=b.py\n2\n```\n")
    out = parse_fenced_blocks(raw)
    assert out.files == {"a.py": "2\n", "b.py": "2\n"}
    assert out.notes.index("a.py") < out.notes.index("b.py")


# ── 提前截断提示：3 反引号块里残留围栏行 ─────────────────────────


def test_three_backtick_artifact_with_embedded_fence_gets_truncation_advisory():
    """3 反引号开栏却内含围栏行 → 产物多半已被提前截断：给一条提示，但不报错。"""
    raw = (
        "```markdown file=README.md\n"
        "# Todo\n"
        "```bash\n"
        "uvicorn app.main:app --reload\n"
        "```\n"
        "更多说明\n"
    )
    out = parse_fenced_blocks(raw)
    # 内层的裸围栏把外层块闭合：README 在示例处截断，余下内容成为说明
    assert out.files == {
        "README.md": "# Todo\n```bash\nuvicorn app.main:app --reload\n"
    }
    assert "更多说明" in out.notes
    assert "README.md" in out.notes
    assert "截断" in out.notes and "4 个反引号" in out.notes


def test_four_backtick_artifact_with_embedded_fence_gets_no_advisory():
    """同内容改用 4 反引号开栏 → 合法嵌套，绝不误报（这是该提示的全部依据）。"""
    raw = (
        "````markdown file=README.md\n"
        "# Todo\n"
        "```bash\n"
        "uvicorn app.main:app --reload\n"
        "```\n"
        "````\n"
    )
    out = parse_fenced_blocks(raw)
    assert out.files == {
        "README.md": "# Todo\n```bash\nuvicorn app.main:app --reload\n```\n"
    }
    assert out.notes == ""


def test_truncation_advisory_is_emitted_once_per_path():
    raw = ("```markdown file=README.md\n# A\n```bash\nx\n```\n"
           "```markdown file=README.md\n# B\n```bash\ny\n```\n")
    out = parse_fenced_blocks(raw)
    assert out.files == {"README.md": "# B\n```bash\ny\n"}
    assert out.notes.count("4 个反引号") == 1


def test_truncation_advisory_reflects_the_surviving_block():
    """同一路径被后一个块覆盖：提示只看最终存活的产物，不残留误报。"""
    raw = ("```markdown file=README.md\n# A\n```bash\nx\n```\n"
           "````markdown file=README.md\n# B\n```bash\ny\n```\n````\n")
    out = parse_fenced_blocks(raw)
    assert out.files == {"README.md": "# B\n```bash\ny\n```\n"}
    assert "4 个反引号" not in out.notes


# ── 结果对象本身 ─────────────────────────────────────────────────


def test_artifact_parse_error_is_a_value_error():
    assert issubclass(ArtifactParseError, ValueError)


def test_parsed_output_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ParsedOutput(files={}, notes="", unexpected=1)


# ── 路径写在围栏头「下一行」：真实模型（实测 deepseek-chat）的高频写法 ──
# 不接纳时整篇文档会静默变成 notes、交付物一个都不落盘，而错误信息只会说
# 「缺少必需产物」，从症状完全看不出病因。

def test_path_on_the_line_after_the_fence_header_is_accepted():
    raw = "```markdown\nfile=docs/ARCHITECTURE.md\n# 架构\n正文\n```\n"
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"docs/ARCHITECTURE.md"}
    assert out.files["docs/ARCHITECTURE.md"] == "# 架构\n正文\n"
    assert "file=" not in out.notes


def test_path_on_next_line_with_four_backticks_and_embedded_example():
    raw = ("````markdown\nfile=README.md\n用法：\n```bash\necho hi\n```\n````\n")
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"README.md"}
    assert "```bash" in out.files["README.md"]


def test_header_form_still_wins_and_is_unaffected():
    raw = "```markdown file=docs/A.md\n# A\n```\n"
    out = parse_fenced_blocks(raw)
    assert set(out.files) == {"docs/A.md"}
    assert out.files["docs/A.md"] == "# A\n"


def test_only_the_immediately_following_line_is_considered():
    # 空行隔开就不算 —— 只接纳「紧邻下一行」这一种形态，避免把正文吞成路径
    raw = "```markdown\n\nfile=docs/A.md\n正文\n```\n"
    out = parse_fenced_blocks(raw)
    assert out.files == {}
    assert "file=docs/A.md" in out.notes


def test_block_without_any_path_stays_prose():
    raw = "```\njust prose\nmore\n```\n"
    out = parse_fenced_blocks(raw)
    assert out.files == {}
    assert "just prose" in out.notes

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
    raw = '````python file=a.py\nx=1\n````'      # 4 个反引号
    with pytest.raises(ArtifactParseError):
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


def test_long_fence_error_message_is_explicit():
    with pytest.raises(ArtifactParseError, match="4 个或更多反引号"):
        parse_fenced_blocks("````python file=a.py\nx=1\n````")


@pytest.mark.parametrize("ticks", ["````", "`````"])
def test_five_or_more_backticks_are_also_rejected(ticks):
    with pytest.raises(ArtifactParseError, match="4 个或更多反引号"):
        parse_fenced_blocks(f"{ticks}python file=a.py\nx=1\n{ticks}")


def test_long_fence_inside_a_body_still_raises():
    """四反引号检测是全局预扫：行为与它落在块内还是块外无关。"""
    raw = "```python file=a.py\nx=1\n````\n```\n"
    with pytest.raises(ArtifactParseError, match="4 个或更多反引号"):
        parse_fenced_blocks(raw)


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


# ── 结果对象本身 ─────────────────────────────────────────────────


def test_artifact_parse_error_is_a_value_error():
    assert issubclass(ArtifactParseError, ValueError)


def test_parsed_output_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ParsedOutput(files={}, notes="", unexpected=1)

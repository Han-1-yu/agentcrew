"""按 kind 的解析原语与占位符检测测试（Task 12）。

``parse_check`` 的输入是**不可信的模型产物**：畸形是常态而非例外。因此除了
brief 给定的用例外，这里还钉死两类行为 —— 能解析的 kind 必须报出可读错误而
**绝不抛异常**（深度嵌套、超长整数、孤立代理项都会穿透常规解析器的异常类型），
以及 ``has_placeholder`` 的三条规则标定（HTML 桩 37 字符 vs 真实内容 47 字符）。
"""

import warnings
from typing import get_args

import pytest

from agentcrew.artifacts import contract
from agentcrew.artifacts.contract import KIND_PARSERS, has_placeholder, parse_check
from agentcrew.artifacts.spec import ArtifactKind

# ── brief 指定的用例 ──────────────────────────────────────────────


def test_python_valid_passes():
    assert parse_check("python", "def f():\n    return 1\n") is None


def test_python_syntax_error_reports_line():
    msg = parse_check("python", "def f(\n  pass\n")
    assert msg is not None and "语法错误" in msg


def test_json_valid_passes():
    assert parse_check("json", '{"a": 1}') is None


def test_json_invalid_reports():
    msg = parse_check("json", '{"a": }')
    assert msg is not None and "JSON" in msg


def test_yaml_valid_passes():
    assert parse_check("yaml", "a: 1\nb:\n  - x\n") is None


def test_yaml_invalid_reports():
    assert parse_check("yaml", "a: [1, 2\n") is not None


@pytest.mark.skipif(not contract._HAS_TOMLLIB, reason="Python 3.10 无 tomllib，toml 检查跳过")
def test_toml_valid_and_invalid():
    assert parse_check("toml", 'a = 1\n') is None
    assert parse_check("toml", 'a = = 1') is not None


def test_markdown_and_text_always_pass():
    assert parse_check("markdown", "") is None
    assert parse_check("text", "anything") is None


def test_unknown_kind_is_error():
    assert parse_check("nonsense", "x") is not None


def test_kind_parsers_covers_all_twelve_kinds():
    assert set(KIND_PARSERS) == set(get_args(ArtifactKind))


@pytest.mark.parametrize("marker", ["TODO", "<placeholder>", "FIXME",
                                    "your code here", "YOUR CODE HERE",
                                    "<!-- 待补充 -->", "待补充"])
def test_placeholder_markers_detected(marker):
    assert has_placeholder(f"# file\n{marker}\n")


def test_real_content_is_not_placeholder():
    assert not has_placeholder("# Todo App\n\nFastAPI service with CRUD endpoints.\n")


def test_ellipsis_only_detected_when_line_is_just_ellipsis():
    assert has_placeholder("def f():\n    ...\n")
    assert not has_placeholder("x = compute(...)  # 正常展开语法\n" + "y = 1\n" * 20)


def test_html_js_placeholder_detected():
    assert has_placeholder("<html><body>placeholder</body></html>")


# ── KIND_PARSERS 与 parse_check 的覆盖面 ──────────────────────────


def test_non_parsing_kinds_always_pass():
    """8 个无结构可校验的 kind 恒返回 None（占位符检查归 Task 13 的 G1）。"""
    for kind in ("markdown", "text", "html", "css", "js", "sql", "shell",
                 "dockerfile"):
        assert parse_check(kind, "# TODO 待补充\n[[[ {{{ \x00") is None


def test_kind_parsers_values_are_callable():
    for kind, parser in KIND_PARSERS.items():
        assert callable(parser), kind


def test_every_kind_is_explicitly_classified():
    """每个 kind 必须显式归类：有结构解析器，或列入「无结构」名单。

    ``KIND_PARSERS`` 的键由字面量生成，所以 brief 的「集合相等」测试永远成立；
    这条才是拦住「新 kind 被 ``.get`` 默认值静默降级为不校验」的那道门。
    """
    literal = set(get_args(ArtifactKind))
    structural = set(contract._STRUCTURAL_PARSERS)
    no_structure = set(contract._NO_STRUCTURE_KINDS)
    assert structural | no_structure == literal
    assert not structural & no_structure


def test_unknown_kind_message_names_the_kind():
    msg = parse_check("nonsense", "x")
    assert msg is not None and "nonsense" in msg


@pytest.mark.skipif(not contract._HAS_TOMLLIB, reason="Python 3.10 无 tomllib，toml 检查跳过")
def test_error_messages_carry_kind_and_position():
    json_msg = parse_check("json", '{"a": }')
    assert json_msg is not None and "JSON" in json_msg and "line 1" in json_msg
    yaml_msg = parse_check("yaml", "a: [1, 2\n")
    assert yaml_msg is not None and "YAML" in yaml_msg and "line" in yaml_msg
    toml_msg = parse_check("toml", "a = = 1")
    assert toml_msg is not None and "TOML" in toml_msg and "line" in toml_msg


# ── parse_check 绝不抛异常 ────────────────────────────────────────


#: 常规解析器异常类型之外的穿透样本（均实测能穿透）：
#: 孤立代理项让 ``ast.parse`` 抛 ``UnicodeEncodeError``；上千层嵌套让四种
#: 解析器都抛 ``RecursionError``；超过 4300 位的整数让 json/toml/yaml 抛出
#: 裸 ``ValueError``（int 转换上限）。畸形产物是数据，不是异常。
HOSTILE_CONTENTS = [
    "",
    "\ufeff",                          # 孤立 BOM
    "\x00\x01\ufffd",                  # NUL、控制字符、替换字符
    "x = '\ud800'",                    # 孤立代理项
    "[" * 3000,                        # 上千层嵌套
    "a = " + "1" * 5000,               # 超过 int 转换上限的整数
    "x" * 1_000_000,                   # 1MB 单体文本
]


@pytest.mark.parametrize("kind", get_args(ArtifactKind))
@pytest.mark.parametrize("content", HOSTILE_CONTENTS, ids=[
    "empty", "bom", "control", "surrogate", "deep_nesting", "huge_int", "one_mb",
])
def test_parse_check_never_raises(kind, content):
    result = parse_check(kind, content)
    assert result is None or isinstance(result, str)


@pytest.mark.skipif(not contract._HAS_TOMLLIB, reason="Python 3.10 无 tomllib，toml 检查跳过")
def test_deeply_nested_content_reports_instead_of_raising():
    assert parse_check("python", "-" * 3000 + "1") is not None
    assert parse_check("json", "[" * 3000) is not None
    assert parse_check("toml", "a = " + "[" * 3000) is not None
    assert parse_check("yaml", "[" * 3000) is not None


@pytest.mark.skipif(not contract._HAS_TOMLLIB, reason="Python 3.10 无 tomllib，toml 检查跳过")
def test_huge_integer_literal_reports_instead_of_raising():
    assert parse_check("json", "1" * 5000) is not None
    assert parse_check("toml", "a = " + "1" * 5000) is not None
    assert parse_check("yaml", "a: " + "1" * 5000) is not None


def test_python_lone_surrogate_reports_instead_of_raising():
    assert parse_check("python", "x = '\ud800'\n") is not None


def test_python_syntax_error_with_line_number_keeps_position():
    """行号拿得到时消息必须带行号（G1 的返工提示靠它定位）。"""
    msg = parse_check("python", "def f(\n  pass\n")
    assert msg is not None and "line 1" in msg


def test_python_null_byte_reports_instead_of_raising():
    """NUL 字节：3.12 起是 SyntaxError 且 ``lineno is None``，≤3.11 是裸 ValueError。

    两种解释器（目标运行时是 3.10+）都必须得到消息：前者走 SyntaxError 分支、
    后者走 ValueError 分支。拿不到行号时消息不得渲染成 ``line None``。
    """
    msg = parse_check("python", "x = 1\x00\n")
    assert isinstance(msg, str)
    assert "语法错误" in msg and "None" not in msg


def test_python_parser_value_error_is_reported(monkeypatch):
    """``ast.parse`` 抛裸 ``ValueError`` 的路径（≤3.11 的真实行为）在 3.12 上只能模拟。

    替换的是 ``contract`` 模块里的 ``ast`` 引用，不是 stdlib 的 ``ast`` 模块本身
    —— 后者会被 pytest 自己的 traceback 渲染用到，替换它会把测试会话搞崩。
    """
    class _FakeAst:
        @staticmethod
        def parse(source: str) -> object:
            raise ValueError("source code string cannot contain null bytes")

    monkeypatch.setattr(contract, "ast", _FakeAst)
    msg = contract.parse_check("python", "x = 1\n")
    assert isinstance(msg, str) and "语法错误" in msg and "None" not in msg


def test_syntax_message_renders_position_only_when_known():
    """行号拿得到才渲染：``line None`` 对返工的模型毫无帮助。"""
    with_line = SyntaxError(
        "'(' was never closed", ("<unknown>", 3, 6, "def f(", 1, 0))
    assert contract._syntax_message(with_line) == "语法错误：line 3: '(' was never closed"
    no_line = SyntaxError("source code string cannot contain null bytes")
    assert contract._syntax_message(no_line) == (
        "语法错误：source code string cannot contain null bytes")
    bare = ValueError("Exceeds the limit (4300 digits) for integer string conversion")
    assert contract._syntax_message(bare).startswith("语法错误：Exceeds the limit")


def test_python_invalid_escape_verdict_is_filter_independent():
    """无效转义序列是警告，不是语法错误：判决不能随警告过滤器翻转。

    这条用例走**真解析器**，因此覆盖的是「本解释器实际发的那个警告类」：默认
    配置下 ``ast.parse`` 只把警告打到 stderr 并解析成功，``-W error``（或
    ``PYTHONWARNINGS=error``）下同一份源码会变成异常。校验原语若随环境改判，
    G1 的结论就依赖跑它的人 —— 这里统一按「警告不是错误」处理。

    **注意**：警告类是随版本变的（3.12 起是 ``SyntaxWarning``，3.10/3.11 上是
    ``DeprecationWarning``），所以实现里两个类都压；下一节的模拟用例把这一点
    钉死，别把过滤器「简化」回只压 ``SyntaxWarning``。
    """
    source = 'import re\n\nPATTERN = re.compile("\\d+")\n'
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert parse_check("python", source) is None
    assert parse_check("python", source) is None


def test_python_suppression_covers_deprecation_warning(monkeypatch):
    """3.10/3.11 的无效转义序列是 ``DeprecationWarning``（3.12 起才提升为
    ``SyntaxWarning``）；本机没有那两个解释器，于是直接让「解析」发一条
    ``DeprecationWarning`` —— 校验的是**压制过滤器真的生效**，而不是当前
    解释器恰好发了哪个警告类。过滤器只压 ``SyntaxWarning`` 的话，这条警告会
    在 ``-W error`` 下从 ``parse_check`` 里穿出来。
    """
    class _WarningAst:
        @staticmethod
        def parse(source: str) -> object:
            warnings.warn("invalid escape sequence '\\d'", DeprecationWarning,
                          stacklevel=2)
            return None

    monkeypatch.setattr(contract, "ast", _WarningAst)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert contract.parse_check("python", "x = 1\n") is None


def test_toml_check_is_skipped_without_tomllib(monkeypatch):
    """Python 3.10 无 ``tomllib``：toml 退化为跳过检查而不是让模块不可导入。"""
    monkeypatch.setattr(contract, "_HAS_TOMLLIB", False)
    assert contract.parse_check("toml", "a = = 1") is None


def test_cross_kind_content_is_handled_by_the_label_kind():
    """同一段内容换个 kind 标签必须按标签裁决，而不是按内容「猜」。"""
    assert parse_check("json", "a: 1\n") is not None   # YAML 合法但不是 JSON
    assert parse_check("yaml", "a: 1\n") is None       # 同一段内容在 YAML 下合法
    assert parse_check("python", "def f(:\n") is not None
    assert parse_check("yaml", "def f(:\n") is None    # YAML 里是普通标量


# ── has_placeholder：三条规则与标定 ───────────────────────────────


def test_markers_are_detected_in_short_stubs():
    """规则 1 判的是「这段内容**是个桩**」，不是「文中出现过标记词」。

    空文件本身由 ``min_bytes`` 兜底；规则 1 负责的是「只写了个标记就交上来」。
    """
    assert has_placeholder("# TODO: 实现真正的逻辑\n")
    assert has_placeholder("# YOUR CODE HERE\n")
    assert has_placeholder("FIXME\n")


def test_todo_marker_is_uppercase_only():
    """``TODO`` 是全大写标记：小写/混合大小写的 ``todo`` 是演示项目的领域词。

    brief 的 ``test_real_content_is_not_placeholder`` 与控制器标定里的
    ``<input placeholder="Add a todo">`` 都要求它不触发占位判定。
    """
    long_body = "def add_todo(item):\n    return item\n" * 20
    assert not has_placeholder(long_body + "class Todo:\n    pass\n")
    assert not has_placeholder(long_body + "<!-- Add a todo -->\n")
    # 真正的 TODO 标记：短桩里照常命中（T-005 的 attempt-1 就是 87 字符的桩）
    assert has_placeholder("# TODO: implement\n")


def test_input_placeholder_attribute_is_legitimate():
    assert not has_placeholder('<input placeholder="Add a todo">\n')


def test_long_legit_frontend_is_not_placeholder():
    html = (
        "<!doctype html>\n<html lang=\"zh\">\n<head>\n"
        "  <meta charset=\"utf-8\">\n  <title>Todo App</title>\n</head>\n"
        "<body>\n  <ul id=\"list\"></ul>\n  <script src=\"app.js\"></script>\n"
        "</body>\n</html>\n"
    )
    assert not has_placeholder(html)


def test_html_stub_is_placeholder_by_length_rule():
    """37 字符、无结构字符 —— 与 47 字符的真实内容一起标定了阈值 40。"""
    stub = "<html><body>placeholder</body></html>"
    assert len(" ".join(stub.split())) == 37
    assert has_placeholder(stub)


def test_real_content_length_matches_calibration():
    content = "# Todo App\n\nFastAPI service with CRUD endpoints.\n"
    assert len(" ".join(content.split())) == 47
    assert not has_placeholder(content)


def test_short_but_structured_content_is_not_placeholder():
    """长度规则只针对「无结构短文本」；短而有结构的代码片段不算占位。"""
    assert not has_placeholder("x = 1")
    assert not has_placeholder('print("hi")')


def test_trailing_ellipsis_line_with_crlf_is_detected():
    assert has_placeholder("def f():\r\n    ...\r\n")


def test_ellipsis_with_other_tokens_on_line_is_not_detected():
    assert not has_placeholder("values = [1, 2, ...]\n" * 10)


def test_placeholder_markers_exact_value():
    """标记表逐字钉死：增删一条都会改变误报画像，必须显式改测试。"""
    assert contract.PLACEHOLDER_MARKERS == (
        "TODO", "<placeholder>", "FIXME", "your code here", "your implementation",
        "待补充", "待实现", "占位",
    )


def test_bare_word_placeholder_is_not_a_marker():
    """裸词 ``placeholder`` 不是标记 —— 它出现在合法 HTML 属性与普通英文里。"""
    prose = "the word placeholder appears here in this sentence of plain prose"
    assert len(" ".join(prose.split())) >= 40
    assert not has_placeholder(prose)


def test_length_rule_boundary_is_40_chars():
    assert has_placeholder("a" * 39)
    assert not has_placeholder("a" * 40)


def test_empty_and_whitespace_only_content_are_placeholder():
    """空内容没有结构也没有实质内容，判为占位（与契约的 ``min_bytes`` 同向）。"""
    assert has_placeholder("")
    assert has_placeholder("   \n\n  \t\n")


def test_long_document_with_an_ellipsis_in_a_code_example_is_not_a_placeholder():
    """实测真实运行：一篇 2092 字的完整中文 README 因 bash 示例里一行 ``...``
    被判「仅含占位内容」，连续两次驳回烧光返工额度、任务 FAILED。

    长文里的 ``...`` 几乎总是示例语法，不是「没写完」——规则 2 因此带长度上界。
    规则 1（标记）同理，见下一条。
    """
    body = (
        "# Todo List 后端\n\n"
        "基于 Flask 的 Todo List REST API，数据持久化在服务端 JSON 文件。\n\n"
        "## 依赖\n\n- Python 3.9+\n- Flask 3.x\n\n"
        "安装依赖：\n\n```bash\npip install -r requirements.txt\n...\n```\n\n"
        "## 启动\n\n```bash\npython app.py\n```\n"
    ) * 4
    assert "..." in body                      # 确实含整行省略号
    assert not has_placeholder(body)          # 但它不是占位文档


def test_todo_marker_in_long_content_is_not_a_placeholder():
    """长文里的 ``TODO`` / ``待实现`` 是**正常内容**，不是「没写完」。

    这条是真实模型运行留下的回归：一份完整的 README 因为在「后续工作」里写了
    ``## TODO`` 与 ``待实现``，被 G1 连判两次「仅含占位内容」，两次返工额度烧光、
    任务 FAILED —— 而那份文档 2092 字符、安装/启动/API 章节俱全。

    规则 1 与规则 2 共用长度上界，所以这里必须是 ``False``；T-005 的返工演示靠的是
    **短桩**里的标记命中（见 ``test_markers_are_detected_in_short_stubs`` 与
    ``test_todo_marker_is_uppercase_only``），不受影响。
    """
    long_readme = (
        "# Todo List 后端\n\n"
        "基于 Flask 的 Todo List REST API，数据持久化在服务端 JSON 文件。\n\n"
        "## 依赖\n\n- Python 3.9+\n- Flask 3.x\n\n"
        "安装依赖：\n\n```bash\npip install -r requirements.txt\n```\n\n"
        "## 启动\n\n```bash\npython app.py\n```\n\n"
        "## 环境变量\n\n- `PORT`：监听端口\n- `DATA_DIR`：数据目录\n\n"
        "## API\n\n| 方法 | 路径 |\n|---|---|\n| GET | `/api/todos` |\n"
        "| POST | `/api/todos` |\n| PUT | `/api/todos/<id>` |\n\n"
        "## 后续工作\n\n- [ ] TODO: 支持多用户\n- [ ] 待实现分页\n"
    ) * 3
    assert "TODO" in long_readme and "待实现" in long_readme   # 两个标记确实都在
    assert len(" ".join(long_readme.split())) > 400            # 且确实超过长度上界
    assert not has_placeholder(long_readme)


# ── 嵌套深度守卫：显式上限，不依赖解释器的 RecursionError ──────────────────
# 依赖 RecursionError 是**版本相关**的：CPython 3.14 已能解析 3000 层一元负号而
# 不抛异常，于是「恶意深层嵌套必须被拦」这条保证会随解释器版本漂移（同 Task 12
# 的 DeprecationWarning：判决不该取决于跑它的解释器）。


def test_deeply_nested_unary_minus_is_rejected_on_every_version():
    assert parse_check("python", "-" * 3000 + "1") is not None


def test_deeply_nested_not_chain_is_rejected():
    assert parse_check("python", "not " * 3000 + "x") is not None


def test_deeply_nested_brackets_are_rejected():
    assert parse_check("python", "[" * 3000) is not None


@pytest.mark.parametrize("src", [
    "x = a - b - c - d - e\n",            # 二元减号链：不是嵌套，不得误判
    "y = [[1, 2], [3, 4]]\n",             # 正常嵌套
    "flag = not other\n",                 # 单个 not
    "def f():\n    return -1\n",          # 普通代码
    "d = {'a': [1, {'b': (2, 3)}]}\n",    # 混合嵌套
])
def test_nesting_guard_has_no_false_positives(src):
    assert parse_check("python", src) is None


def test_nesting_guard_survives_untokenizable_input():
    """分词失败的输入必须退化成「不拦」并交回 ast.parse —— 守卫是辅助，不是权威。

    孤立代理项会让 tokenize 抛 UnicodeEncodeError；漏捕它会让异常穿透
    parse_check，违反「never raises」契约（test_parse_check_never_raises 抓过）。
    """
    assert parse_check("python", 'x = "\ud800"') is not None


# ── 长度阈值的单位是「信息量」而非字符数（中文标定） ──────────────────────
# 实测：真实模型生成的一份中文报告（「# 审计报告 / 结论：鉴权与输入校验均已覆盖。」，
# 22 字符）被 G1 判「仅含占位内容」而驳回 —— 判定是错的，一份 22 字的中文**是**
# 完整内容。根因是阈值 40 在 ASCII 上标定（37 vs 47），直接拿它比中文字符数会
# 系统性误杀中文：中文一个字符约等于一个词，信息密度是拉丁字母的三倍。
# 折算后两种文字的有效阈值才可比：中文约 13 字 ≈ 8 个词 ≈ 英文 40 字符 ≈ 8 个词。


def test_chinese_sentence_is_not_a_placeholder():
    """曾被误判的真实案例：一句完整的中文结论。"""
    assert not has_placeholder("# 审计报告\n\n结论：鉴权与输入校验均已覆盖。\n")


def test_short_chinese_module_description_is_not_a_placeholder():
    """15 字的中文一行说明 —— 恰是模块 docstring 最常见的写法。"""
    assert not has_placeholder("本模块负责产物契约与安全落盘。\n")


def test_chinese_stub_without_marker_is_still_placeholder():
    """放宽的是**长度单位**，不是判定本身：真正的短中文桩照拦。"""
    assert has_placeholder("这是第一版")


def test_cjk_characters_count_triple_in_length_rule():
    from agentcrew.artifacts.contract import _content_units
    assert _content_units("abc") == 3
    assert _content_units("审计") == 6
    assert _content_units("a审") == 4


def test_ascii_calibration_is_unchanged_by_the_weighting():
    """折算只影响 CJK：两个钉死锚点必须原样成立。"""
    html_stub = "<html><body>placeholder</body></html>"
    real_ascii = "# Todo App\n\nFastAPI service with CRUD endpoints.\n"
    assert has_placeholder(html_stub)          # 37 字符，纯 ASCII
    assert not has_placeholder(real_ascii)     # 47 字符，纯 ASCII


def test_chinese_needs_fourteen_characters_to_clear_the_rule():
    """边界：中文侧的有效阈值是 14 字（13×3=39 < 40，14×3=42 ≥ 40）。

    14 字 ≈ 7 个中文词，与英文侧 40 字符 ≈ 8 个词基本对齐 —— 这正是折算 3 倍的
    标定意图：两种文字的门槛落在同一个**信息量**上。
    """
    assert has_placeholder("一二三四五六七八九十十一二")        # 13 字 → 39 单位
    assert not has_placeholder("一二三四五六七八九十十一二三")    # 14 字 → 42 单位

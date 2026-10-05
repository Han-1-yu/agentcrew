"""按 kind 的解析原语（spec §5.2、§12 G1）。

``parse_check`` 是产物的「结构合法性」原语：只有真能解析出语法的 kind
（python/json/yaml/toml）在这里校验，其余 8 个 kind 恒通过 —— 它们的结构检查
由 Task 13 的 G1 用 :func:`has_placeholder` 与契约的 ``must_contain`` 承担，
本模块不发明按 kind 的额外判据。

硬性契约：**``parse_check`` 绝不抛异常**。畸形产物是数据，不是异常；返回的中文
消息会作为 G1 的 issue 回灌给模型当返工提示，因此每条都带 kind 名与（解析器
给出了的话）行列号。除各解析器的常规异常外，还有三类穿透样本必须显式接住：
孤立代理项让 ``ast.parse`` 抛 ``UnicodeEncodeError``；上千层嵌套让四种解析器
都抛 ``RecursionError``；超过 4300 位的整数让 json/toml/yaml 抛裸 ``ValueError``
（int 转换上限）；python 分支同样接 ``ValueError`` —— 3.12 起 null 字节是
``SyntaxError``，≤3.11 上却是裸 ``ValueError``，而目标运行时是 3.10+。以上
都在 3.12.10 上实测（≤3.11 的 ``ValueError`` 路径由 monkeypatch 覆盖），
并各有对应测试。python 分支另外压掉 ``SyntaxWarning`` 与 ``DeprecationWarning``
（无效转义序列在 3.12 是前者、3.10/3.11 是后者）：它们在 ``-W error`` 下会从
「解析成功」翻成异常，判决不能随环境变。
"""

import ast
import io
import json
import sys
import tokenize
import warnings
from collections.abc import Callable
from typing import get_args

import yaml

from agentcrew.artifacts.spec import ArtifactKind

#: 解析器签名：内容合法返回 ``None``，否则返回人类可读的中文错误消息。
KINDParser = Callable[[str], str | None]

#: 深度嵌套触发的解释器递归上限：不是语法错误，但同样必须变成消息。
_TOO_DEEP = "解析失败：嵌套层级过深，超出解析上限"

#: Python 3.10 的标准库没有 ``tomllib``（项目又不引入 ``tomli`` 依赖）：此时
#: toml 退化为「跳过检查」，但模块必须仍可导入 —— 否则 12 个 kind 会跟着一起
#: 不可用。故 ``import tomllib`` 放在条件分支里，由 :data:`_HAS_TOMLLIB` 兜底。
_HAS_TOMLLIB: bool = sys.version_info >= (3, 11)
if _HAS_TOMLLIB:
    import tomllib


def parse_check(kind: str, content: str) -> str | None:
    """按 ``kind`` 校验产物内容：通过返回 ``None``，否则返回中文错误消息。

    未知 kind 返回错误消息而不是 ``None``：放行一个不认识的类型，等于让这份
    产物绕过全部校验。
    """
    parser = KIND_PARSERS.get(kind)
    if parser is None:
        return f"未知的产物类型：{kind!r}，无法校验"
    return parser(content)


def _syntax_message(exc: Exception) -> str:
    """语法错误 → 消息：**行号拿得到才渲染**。

    ``SyntaxError`` 只在编译器知道位置时才带 ``lineno`` —— 3.12 上 null 字节的
    ``SyntaxError`` 就是 ``lineno is None``；≤3.11 上 null 字节与 int 转换上限
    抛的裸 ``ValueError`` 更是什么位置都没有。渲染成 ``line None`` 对要返工的
    模型毫无帮助。
    """
    reason = getattr(exc, "msg", None) or str(exc)
    lineno = getattr(exc, "lineno", None)
    if lineno is None:
        return f"语法错误：{reason}"
    return f"语法错误：line {lineno}: {reason}"


#: 源码嵌套深度上限（见 _check_python 的说明）。
_MAX_NESTING_DEPTH = 100


def _max_nesting(source: str) -> int | None:
    """源码的嵌套深度上界；无法分词时返回 ``None``（交给 ``ast.parse`` 定夺）。

    统计两样东西取较大者：

    - **括号嵌套**（``((((x))))``）—— 数括号即可；
    - **连续一元运算符**（``----1``、``not not not x``）—— 它同样把解析器推深，
      却一个括号都没有，只数括号抓不到。

    二元运算符不会误计：``a - b - c`` 的 ``-`` 之间隔着操作数，计数器会归零。
    """
    depth = 0
    brackets = 0
    unary = 0
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.OP:
                if token.string in "([{":
                    brackets += 1
                    depth = max(depth, brackets)
                elif token.string in ")]}":
                    brackets = max(0, brackets - 1)
                    unary = 0
                elif token.string in "+-~":
                    unary += 1
                    depth = max(depth, unary)
                else:
                    unary = 0
            elif token.type == tokenize.NAME and token.string == "not":
                unary += 1
                depth = max(depth, unary)
            elif token.type in (
                tokenize.NUMBER, tokenize.STRING, tokenize.NAME,
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT,
            ):
                unary = 0
    except (tokenize.TokenError, IndentationError, SyntaxError, ValueError,
            RecursionError):
        # 分词失败就**不下判断**，交回 ``ast.parse``：本函数是**辅助守卫**，任何
        # 它处理不了的输入都必须退化成「不拦」，绝不能自己抛出去 ——
        # ``parse_check`` 的契约是永不抛异常（越界判定由 ast.parse 负责）。
        # ValueError 覆盖 UnicodeError：孤立代理项会让 tokenize 抛
        # UnicodeEncodeError；RecursionError 覆盖 tokenize 自身被深嵌套压爆。
        return None
    return depth


def _check_python(content: str) -> str | None:
    """``ast.parse`` 校验语法；错误消息带行号（G1 直接当返工提示用）。"""
    nesting = _max_nesting(content)
    if nesting is not None and nesting > _MAX_NESTING_DEPTH:
        # 显式上限，**不依赖 RecursionError**：那是版本相关的行为 —— CPython 3.14
        # 已能解析 3000 层一元负号而不抛异常，于是「恶意深层嵌套必须被拦」这条
        # 保证会随解释器版本漂移（同一类问题：Task 12 的 DeprecationWarning）。
        # 上限取 100：远超任何真实代码，远低于解析器的递归上限。
        return f"{_TOO_DEEP}（约 {nesting} 层，上限 {_MAX_NESTING_DEPTH}）"
    try:
        with warnings.catch_warnings():
            # 无效转义序列（如 "\d"）是警告而非语法错误：默认只打到 stderr 并
            # 解析成功，``-W error`` / ``PYTHONWARNINGS=error`` 下同一份源码却
            # 变成异常 —— 判决会随跑它的人翻转。**两个警告类都必须压**：3.12
            # 起是 SyntaxWarning，3.10/3.11 上还是 DeprecationWarning（目标
            # 运行时是 3.10+），只压前者会让 ≤3.11 在 ``-W error`` 下抛
            # DeprecationWarning 穿出本函数。校验原语只认语法，这里按「警告
            # 不是错误」统一裁决，顺带保证输出干净。
            warnings.simplefilter("ignore", SyntaxWarning)
            warnings.simplefilter("ignore", DeprecationWarning)
            ast.parse(content)
    except SyntaxError as exc:
        return _syntax_message(exc)
    except UnicodeError:
        # 孤立代理项：ast.parse 先按 UTF-8 编码源码，抛 UnicodeEncodeError
        return "语法错误：源码含无法编码的孤立代理项"
    except ValueError as exc:
        # ≤3.11：null 字节与 int 转换上限抛的是裸 ValueError（3.12 把前者改判成
        # SyntaxError）。UnicodeError 是 ValueError 子类，故其分支必须排在前。
        return _syntax_message(exc)
    except RecursionError:
        return _TOO_DEEP
    return None


def _check_json(content: str) -> str | None:
    """``json.loads`` 校验；错误消息带行列号。"""
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        return f"JSON 解析失败：line {exc.lineno} column {exc.colno}: {exc.msg}"
    except ValueError as exc:
        # 超长整数触发 int 转换上限，抛的是裸 ValueError（非 JSONDecodeError）
        return f"JSON 解析失败：{exc}"
    except RecursionError:
        return _TOO_DEEP
    return None


def _check_yaml(content: str) -> str | None:
    """``yaml.safe_load`` 校验（**绝不用** ``yaml.load``）；错误消息带行列号。"""
    try:
        yaml.safe_load(content)
    except yaml.YAMLError as exc:
        return _yaml_message(exc)
    except ValueError as exc:
        # 超长整数触发 int 转换上限（YAMLError 不是 ValueError 的子类，故另立）
        return f"YAML 解析失败：{exc}"
    except RecursionError:
        return _TOO_DEEP
    return None


def _check_toml(content: str) -> str | None:
    """``tomllib.loads`` 校验；Python 3.10 无 ``tomllib`` 时跳过检查。"""
    if not _HAS_TOMLLIB:
        return None
    try:
        tomllib.loads(content)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        # TOMLDecodeError 的消息自带 (at line L, column C)；ValueError 兜住
        # 超长整数的 int 转换上限（实测抛裸 ValueError）
        return f"TOML 解析失败：{exc}"
    except RecursionError:
        return _TOO_DEEP
    return None


def _yaml_message(exc: yaml.YAMLError) -> str:
    """YAML 错误 → 一行短消息，优先用 :class:`yaml.MarkedYAMLError` 的行列号。

    不用 ``str(exc)`` 整体：它会把上下文、源码片段与插入符一起摊成多行，
    作为返工提示太长。
    """
    problem = getattr(exc, "problem", None)
    if not problem:
        first_line = str(exc).strip().split("\n", 1)[0]
        problem = first_line or type(exc).__name__
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return f"YAML 解析失败：{problem}"
    return f"YAML 解析失败：line {mark.line + 1} column {mark.column + 1}: {problem}"


def _always_ok(content: str) -> str | None:
    """无独立结构可校验的 kind 的解析器：恒通过。"""
    return None


#: 有语法可校验的 kind → 解析器。
_STRUCTURAL_PARSERS: dict[str, KINDParser] = {
    "python": _check_python,
    "json": _check_json,
    "yaml": _check_yaml,
    "toml": _check_toml,
}

#: 无独立结构可校验的 kind：G1 的 ``has_placeholder`` 与契约 ``must_contain``
#: 才是它们的判据。名单显式列出而不是只靠 ``.get`` 的默认值 —— 新增 kind 时
#: 「有没有校验」必须被主动决定，不能默认滑成不校验；由 ``tests/test_contract.py``
#: 的 ``test_every_kind_is_explicitly_classified`` 钉死「结构解析器 + 本名单」
#: 必须恰好铺满 :data:`ArtifactKind`。
_NO_STRUCTURE_KINDS: tuple[str, ...] = (
    "markdown", "text", "html", "css", "js", "sql", "shell", "dockerfile",
)

#: kind → 解析器。键由 :data:`ArtifactKind` 字面量生成，与字面量不可能漂移。
KIND_PARSERS: dict[str, KINDParser] = {
    kind: _STRUCTURAL_PARSERS.get(kind, _always_ok)
    for kind in get_args(ArtifactKind)
}


#: 占位符标记。除 ``TODO`` 外都大小写不敏感匹配。
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "TODO", "<placeholder>", "FIXME", "your code here", "your implementation",
    "待补充", "待实现", "占位",
)

#: 只认全大写的标记。小写/混合大小写的 ``todo`` 是本项目演示目标（Todo 应用）
#: 的领域词：``# Todo App``、``class Todo``、``<input placeholder="Add a todo">``
#: 都是合法产物，brief 的 ``test_real_content_is_not_placeholder`` 与控制器标定
#: 都要求它们不被判为占位。其余标记照常大小写不敏感（``YOUR CODE HERE`` 必须
#: 能被 ``your code here`` 命中）。
_CASE_SENSITIVE_MARKERS = frozenset({"TODO"})

#: 整行（去缩进后）只是省略号也算占位；``x = compute(...)`` 不算。
_ELLIPSIS_LINES = ("...", "…")

#: 规则 3：折叠空白后的**信息量**短于该阈值、且不含结构字符 → 判为占位。
#: 阈值 40 与下面的结构字符表是共同标定的：HTML 桩
#: ``<html><body>placeholder</body></html>``（37 字符、无结构字符）必须命中，
#: 而真实内容 ``# Todo App ... endpoints.``（47 字符）不能。
#:
#: **单位是信息量而非字符数**，见 :func:`_content_units`。
_PLACEHOLDER_MAX_UNITS = 40
_STRUCTURE_CHARS = frozenset("=:(){}[];\"'")

#: 规则 1、2（标记 / 整行省略号）的长度上界：只有**短文**里的这些信号才说明
#: 「这篇还没写」。长文里的它们是正常内容 —— 代码示例里的 `...` 省略、真实代码
#: 里的 `TODO` 注释。
#:
#: 实测标定：一篇 2092 字的完整中文 README（标题/依赖/启动/环境变量/示例俱全）
#: 因 bash 示例里一行 `...` 被判占位，连续两次驳回烧光返工额度、任务 FAILED。
#: 400 取值只需满足两点：比任何真实文档短、比任何桩长（桩通常是几行）。
#:
#: 单位同样是信息量（:func:`_content_units`）。
_STUB_MAX_UNITS = 400

#: 一个 CJK 字符折合多少「信息量单位」。
#:
#: 拉丁字母平均 5 个字符一个词，而中文一个字大致就是一个词 —— 同一个意思，中文
#: 所需的字符数只有英文的三分之一左右。两个阈值都是在 ASCII 上标定的（37 vs 47），
#: 若直接拿它们比**中文字符数**，就会系统性误杀中文：「本模块负责产物契约与安全
#: 落盘。」（15 字，一句完整的话）会被判成占位，而它在信息量上与 45 字符的英文等价。
_CJK_WEIGHT = 3

#: 按权重计的 CJK 码点区间：汉字、假名、谚文、CJK 标点与全角形式。
#: 标点与全角形式一并计入 —— 它们同样是双宽字符，且只出现在 CJK 文本里。
_CJK_RANGES = (
    (0x3000, 0x303F),   # CJK 标点（。、：；「」）
    (0x3040, 0x30FF),   # 平假名 / 片假名
    (0x3400, 0x4DBF),   # 汉字扩展 A
    (0x4E00, 0x9FFF),   # 汉字基本区
    (0xAC00, 0xD7AF),   # 谚文音节
    (0xF900, 0xFAFF),   # 汉字兼容表意
    (0xFF00, 0xFFEF),   # 全角形式（：（）％等）
)


def _content_units(text: str) -> int:
    """内容长度，单位是**信息量**：一个 CJK 字符记 :data:`_CJK_WEIGHT` 个单位。

    两个长度阈值（:data:`_PLACEHOLDER_MAX_UNITS` 与 :data:`_STUB_MAX_UNITS`）回答
    的是同一个问题 ——「这段内容是不是短得不像真东西」。字符数不是这个问题的合适
    单位：中文的信息密度约为拉丁字母的三倍，同样的意思只需三分之一的字符。折算
    之后，两种文字的有效阈值才可比。
    """
    units = 0
    for char in text:
        codepoint = ord(char)
        if any(low <= codepoint <= high for low, high in _CJK_RANGES):
            units += _CJK_WEIGHT
        else:
            units += 1
    return units


def has_placeholder(content: str) -> bool:
    """内容是否为**只有**占位信号（标记 / 整行省略号 / 无结构的超短文本）。

    消息是「仅含占位内容」，判定就必须真的看**是否仅含**。因此**规则 1、2 共用
    同一个长度上界** :data:`_STUB_MAX_UNITS`：占位是**一条短桩**的特征，不是
    「文中某处出现过标记词」。长文里的标记与省略号都是正常内容 —— 代码示例里的
    ``...`` 省略、真实代码里的 ``TODO`` 注释、文档末尾的 ``## TODO`` 小节、
    「后续工作」里的「待实现」。

    实测教训（两次，代价都是整个任务）：

    - 一篇 2092 字的完整中文 README 因 bash 示例里一行 ``...`` 被判占位，连续
      两次驳回烧光返工额度、任务 FAILED；
    - 同一次真实模型运行里，另一份 619 字、含 ``## TODO`` 小节的 README 仍然
      被判占位 —— 上界当时只加在规则 2 上，规则 1 漏了。**「文档越完整越可能
      正经提到这些词」意味着无上界的标记匹配方向是反的。**

    规则 1 设上界**不会**削弱返工演示：T-005 的两份桩产物只有 87 与 40 字符，
    远在上界之内，照常命中。

    三条规则的标定见 :data:`_STUB_MAX_UNITS`、:data:`_PLACEHOLDER_MAX_UNITS`
    与 :data:`_CASE_SENSITIVE_MARKERS`；改动其中一条都会让标定失效。

    两个长度阈值的单位是**信息量**而非字符数（:func:`_content_units`）—— 直接按
    字符数比较会系统性误杀中文，因为中文一个字符约等于一个词。
    """
    collapsed = " ".join(content.split())
    if _content_units(collapsed) >= _STUB_MAX_UNITS:
        return False
    lowered = content.lower()
    for marker in PLACEHOLDER_MARKERS:
        if marker in _CASE_SENSITIVE_MARKERS:
            if marker in content:
                return True
        elif marker.lower() in lowered:
            return True
    for line in content.split("\n"):
        if line.strip() in _ELLIPSIS_LINES:
            return True
    if _content_units(collapsed) >= _PLACEHOLDER_MAX_UNITS:
        return False
    return not any(char in collapsed for char in _STRUCTURE_CHARS)

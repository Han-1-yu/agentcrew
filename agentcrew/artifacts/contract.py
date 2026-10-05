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
import json
import sys
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


def _check_python(content: str) -> str | None:
    """``ast.parse`` 校验语法；错误消息带行号（G1 直接当返工提示用）。"""
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

#: 规则 3：折叠空白后短于该长度、且不含结构字符 → 判为占位。阈值 40 与下面的
#: 结构字符表是共同标定的：HTML 桩 ``<html><body>placeholder</body></html>``
#: （37 字符、无结构字符）必须命中，而真实内容 ``# Todo App ... endpoints.``
#: （47 字符）不能。
_PLACEHOLDER_MAX_CHARS = 40
_STRUCTURE_CHARS = frozenset("=:(){}[];\"'")

#: 规则 1、2（标记 / 整行省略号）的长度上界：只有**短文**里的这些信号才说明
#: 「这篇还没写」。长文里的它们是正常内容 —— 代码示例里的 `...` 省略、真实代码
#: 里的 `TODO` 注释。
#:
#: 实测标定：一篇 2092 字的完整中文 README（标题/依赖/启动/环境变量/示例俱全）
#: 因 bash 示例里一行 `...` 被判占位，连续两次驳回烧光返工额度、任务 FAILED。
#: 400 取值只需满足两点：比任何真实文档短、比任何桩长（桩通常是几行）。
_STUB_MAX_CHARS = 400


def has_placeholder(content: str) -> bool:
    """内容是否为**只有**占位信号（标记 / 整行省略号 / 无结构的超短文本）。

    消息是「仅含占位内容」，判定就必须真的看**是否仅含** —— 规则 1、2 因此都带
    长度上界 :data:`_STUB_MAX_CHARS`：只有短文里的标记与省略号才说明「这篇东西
    还没写」，长文里的它们是**正常内容**（代码示例里的 `...` 省略、真实代码里的
    `TODO` 注释）。

    实测教训：一篇 2092 字的完整中文 README（标题/依赖/启动/环境变量/示例俱全）
    因为 bash 示例里有一行 `...` 被判成占位，连续两次驳回把返工额度烧光、任务
    FAILED。上界就是为这个加的。

    三条规则的标定见 :data:`_STUB_MAX_CHARS`、:data:`_PLACEHOLDER_MAX_CHARS`
    与 :data:`_CASE_SENSITIVE_MARKERS`；改动其中一条都会让标定失效。
    """
    collapsed = " ".join(content.split())
    if len(collapsed) < _STUB_MAX_CHARS:
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
    if len(collapsed) >= _PLACEHOLDER_MAX_CHARS:
        return False
    return not any(char in collapsed for char in _STRUCTURE_CHARS)

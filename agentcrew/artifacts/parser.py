"""围栏块产物解析（spec §9.1）。

模型回复里**只有带 ``file=`` 头的围栏代码块会成为产物**，其余文字与无路径的
代码块都是说明（``notes``），不落盘。本模块是「模型原始回复 → 待写文件」的
唯一通路，输入是不可信的：敌对与畸形按下面钉死的规则裁决，不做猜测性修复。

解析规则（与 spec §9.1 一致；刻意的加严/放宽已在括号中标出）：

- 起始围栏必须**顶格**且整行匹配 ``^```lang[ file=path]``，语言名可省略。
  ``file=`` 的值取「到行尾」（非贪婪），因此允许空格与中文路径 —— spec 原文
  写 ``\\S+``，会把 ``docs/my notes.txt`` 截在第一个空格处。
- 围栏按 **CommonMark 语义**判定：开栏是**顶格、行首 ≥3 个反引号**；闭栏是
  「去掉首尾空白后**只有反引号**、且长度**不短于开栏**」的一行。于是 3 反引号
  的块仍被第一个裸的 3 反引号行闭合（与最初一致），而 **4 反引号的块可以安全
  地内嵌 3 反引号的代码示例** —— 这正是 README/docs 里写运行命令所必需的
  （N 开 M 闭当且仅当 M ≥ N）。
- 带语言名或 ``file=`` 的行永远是**起始候选**、绝不闭合（畸形输入因此构造不出
  嵌套围栏）；少于 3 个反引号的行既开不了块也闭不了块。
- 未闭合的块 → :class:`ArtifactParseError`，消息带起始行号（1 起）。
- 同一个路径重复出现 → 取**最后一次**的内容，并在 ``notes`` 末尾追加一条
  **按路径去重**的告警；被覆盖块的正文既不落盘也不进 notes。
- ``file=`` 的值为空（``file=`` / ``file=   ``）视同没有 ``file=``：该块是说明。
- 路径只做 ``\\`` → ``/`` 和首尾空白剥离，**不做安全与契约校验**：``../x``
  必须原样交给 Task 11 的 ``ArtifactStore`` 拒收，否则诊断信息会丢失 ——
  解析器的契约是「忠实提取模型写了什么」。
- 行尾接受 ``\\n`` / ``\\r\\n`` / 孤立 ``\\r``；产物正文每行补 ``\\n``
  （因此总以换行结尾，空正文是空串），``notes`` 为说明行以 ``\\n`` 拼接。
"""

import re

from pydantic import BaseModel, ConfigDict, Field

from agentcrew.artifacts.spec import ArtifactKind

#: 起始围栏：顶格、≥3 个反引号 + 可选语言名 + 可选 ``file=`` 路径（到行尾，
#: 允许空格）。围栏长度记进 ``fence`` 组：闭栏必须不短于它（CommonMark）。
#: ``path`` 用 ``.*?`` 而非 ``.+?``：``file=`` 后为空也要能匹配上，才能与
#: ``file=   ``（只有空白）得到完全一致的裁决 —— 否则同一处坏写法会因为多一个
#: 空格而改走另一条分支。
_START_FENCE_RE = re.compile(
    r"^(?P<fence>`{3,})(?P<lang>[A-Za-z0-9_+.\-]*)(?:\s+file=(?P<path>.*?))?\s*$"
)


class ArtifactParseError(ValueError):
    """回复不符合围栏块协议：围栏块一直没有闭合。

    消息为中文且带起始行号，可直接作为失败原因回灌给模型；Task 24 捕获后按
    ``RUNNING → READY`` 重试。
    """


class ParsedOutput(BaseModel):
    """一次围栏解析的结果。

    ``files``：待落盘文件，键为规范化后的相对路径（``\\`` 已转 ``/``），
    值为块正文（每行以 ``\\n`` 结尾）。``notes``：围栏块之外的全部说明文字，
    按原文顺序拼接，末尾附重复路径告警。
    """

    model_config = ConfigDict(extra="forbid")

    files: dict[str, str] = Field(default_factory=dict)
    notes: str = ""


#: 语法高亮语言名 → ``ArtifactKind``。键全小写（查表时统一小写化）；值必须
#: 恰好覆盖 :data:`ArtifactKind` 的 12 个取值（由测试断言，防止表悄悄失配）。
#: 找不到对应 ``ArtifactKind`` 的语言名（``ts``/``scss`` 等）**不猜**，返回
#: ``None``，由调用方按任务契约里的 ``kind`` 兜底。
LANG_TO_KIND: dict[str, ArtifactKind] = {
    # python
    "py": "python",
    "py3": "python",
    "python": "python",
    "python3": "python",
    # json
    "json": "json",
    # yaml
    "yaml": "yaml",
    "yml": "yaml",
    # toml
    "toml": "toml",
    # markdown
    "markdown": "markdown",
    "md": "markdown",
    # text
    "text": "text",
    "txt": "text",
    "plaintext": "text",
    # html
    "html": "html",
    "htm": "html",
    # css
    "css": "css",
    # js
    "js": "js",
    "javascript": "js",
    "mjs": "js",
    "cjs": "js",
    # dockerfile
    "dockerfile": "dockerfile",
    # sql
    "sql": "sql",
    # shell
    "shell": "shell",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
}


def normalize_lang(lang: str) -> ArtifactKind | None:
    """把围栏语言名归一化为 :data:`ArtifactKind`；未知语言返回 ``None``。

    大小写不敏感、忽略首尾空白。返回 ``None`` 表示「这个名字说明不了产物
    类型」，调用方改用任务契约里的 ``kind`` 兜底。
    """
    return LANG_TO_KIND.get(lang.strip().lower())


def parse_fenced_blocks(raw: str) -> ParsedOutput:
    """解析模型回复里的 ``file=`` 围栏块。

    只有带非空 ``file=`` 的块进入 ``files``；无 ``file=`` 的块与所有块外文字
    按原文顺序进 ``notes``，重复路径的告警追加在末尾。
    """
    lines = _split_lines(raw)
    files: dict[str, str] = {}
    note_lines: list[str] = []
    duplicated: list[str] = []
    warned: set[str] = set()
    index = 0
    while index < len(lines):
        match = _START_FENCE_RE.match(lines[index])
        if match is None:
            note_lines.append(lines[index])
            index += 1
            continue
        body, close_index = _read_block(
            lines, start=index, fence_length=len(match.group("fence"))
        )
        index = close_index + 1
        path = _normalize_path(match.group("path"))
        if not path:
            note_lines.extend(body)
            continue
        if path in files and path not in warned:
            warned.add(path)
            duplicated.append(path)
        files[path] = _join_body(body)
    note_lines.extend(_duplicate_note(path) for path in duplicated)
    return ParsedOutput(files=files, notes="\n".join(note_lines))


def _split_lines(raw: str) -> list[str]:
    """按行尾切分，接受 ``\\n`` / ``\\r\\n`` / 孤立 ``\\r`` 三种写法。

    不用 :meth:`str.splitlines`：它还会在 ``\\x0c``、``\\u2028`` 等处断行，
    会把块正文里的这些字符悄悄改成换行 —— 正文必须忠实保留。末尾的换行不算
    一行（``"a\\n"`` 与 ``"a"`` 结果相同），空输入得到空列表。
    """
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _closing_fence_length(line: str) -> int | None:
    """该行作为闭合围栏的反引号数；不是闭合围栏则返回 ``None``。

    闭合围栏必须**只有**反引号：带语言名或 ``file=`` 的行永远不闭合，畸形输入
    因此构造不出嵌套围栏。长度够不够由调用方按开栏长度判定 —— 少于 3 个反引号
    的行在这里就会返回 2 / 1，而开栏至少是 3，永远判不闭合。
    """
    stripped = line.strip()
    if stripped and stripped.strip("`") == "":
        return len(stripped)
    return None


def _read_block(
    lines: list[str], start: int, *, fence_length: int
) -> tuple[list[str], int]:
    """从起始围栏行读到闭合围栏，返回 ``(正文行, 闭合行下标)``。

    闭合条件（CommonMark）：该行去掉首尾空白后只有反引号，且**长度 ≥ 开栏
    长度**。因此 4 反引号的块能安全内嵌 3 反引号的代码示例。
    读到结尾仍未闭合 → :class:`ArtifactParseError`，行号 1 起计。
    """
    body: list[str] = []
    for index in range(start + 1, len(lines)):
        length = _closing_fence_length(lines[index])
        if length is not None and length >= fence_length:
            return body, index
        body.append(lines[index])
    raise ArtifactParseError(f"未闭合的代码块，起始行 {start + 1}")


def _normalize_path(raw: str | None) -> str:
    """``file=`` 的值：剥离首尾空白并把 ``\\`` 统一成 ``/``（内部空格保留）。

    只做归一化，不做安全校验 —— 那是 ``ArtifactStore`` 的职责。
    """
    return (raw or "").strip().replace("\\", "/")


def _join_body(body: list[str]) -> str:
    """正文每行补 ``\\n``：写出的文件总以换行结尾，空正文得到空串。"""
    return "".join(line + "\n" for line in body)


def _duplicate_note(path: str) -> str:
    """同一路径重复出现时的说明行（每个路径最多一条）。"""
    return f"警告：路径 {path} 重复出现，已采用最后一次的围栏块内容"

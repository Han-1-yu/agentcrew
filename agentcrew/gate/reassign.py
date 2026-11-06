"""R10：主管驳回后「换个人做」。

主管说这活不行，最自然的反应是「换个人来做」——但 ``Gatekeeper`` 原来的驳回只有
两条路：退回原执行者（``REWORK``）或额度用尽判 ``FAILED``。主管对「这个人做不好」
的判断没有出口。

这一层加一条：主管驳回且还有重派额度时，把 ``task.role`` 换成配置的备选角色，然后
**照常走 REWORK**。这样不用新增 ``REWORK → READY`` 跃迁（那会动 10 态契约），下一
次派发自然找新角色。

三条刻意的边界：

1. **必须有显式的备选映射**（``role_fallback``）。没有「谁能替谁」的知识就不换人——
   自动挑一个顺眼的角色比不换更糟。
2. **换人也消耗返工额度**。同一份额度换一只手试，不是免费加次数；额度用尽照样
   ``FAILED``，所以不存在「换了人就能无限重试」。
3. **不换给主管自己**。派活的人转身去执行，既是职责冲突，也会让下轮验收无人可做。
"""

from __future__ import annotations

from agentcrew.roles.catalog import RoleCatalog
from agentcrew.task.task import Task


class ReassignPolicy:
    """按 ``role_fallback`` 挑选替代执行角色。"""

    def __init__(
        self,
        catalog: RoleCatalog,
        fallback: dict[str, list[str]] | None = None,
        *,
        max_reassignments: int = 1,
    ) -> None:
        """``fallback`` 形状：``{"backend_dev": ["senior_backend", "architect"]}``。

        按声明顺序取第一个**可用**的：真实存在于角色目录、不等于当前角色、也不
        等于主管本人。任一不满足就继续看下一个，全都不行就不换人。
        """
        self._catalog = catalog
        self._fallback = dict(fallback or {})
        self._max_reassignments = max(0, int(max_reassignments))

    def pick(self, task: Task, *, supervisor: str = "") -> str | None:
        """返回替代角色 key；不该换 / 换不了时返回 ``None``。"""
        if self._max_reassignments <= 0:
            return None
        if task.reassign_count >= self._max_reassignments:
            return None
        for raw in self._fallback.get(task.role) or []:
            candidate = (raw or "").strip()
            if not candidate:
                continue
            if candidate == task.role or candidate == supervisor:
                continue
            if not self._catalog.has(candidate):
                continue
            return candidate
        return None

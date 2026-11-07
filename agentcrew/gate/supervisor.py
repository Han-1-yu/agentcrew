"""R7/R9：层级监督——谁派的活谁验收，以及经理对规划期任务的验收。

R6 的四道闸只回答「谁能派」，没回答「派出去的活谁验收」。R7 先把**动态委派任务**
接上主管审核：`delegated_by` 是谁，就由谁验收。但规划阶段由 PM 拆出来的任务没有
`delegated_by`——它们的主管理应也是 PM，R7 却完全没覆盖。

R9 补上这一半：``supervisor_scope="all"`` 时，没有 ``delegated_by`` 的任务回落到
配置的主管角色（默认 ``project_manager``）。

四条刻意的边界：

1. **默认仍只覆盖委派任务**（``scope="delegated"``）。规划期任务多一次评审就是多
   一次真实调用，不该在升级时静默涨价。
2. **主管同时是 G2 评审人时跳过**。``architect`` 的任务 G2 本来就是
   ``project_manager`` 在审——再让同一个人以「主管」身份审一遍只是重复烧钱。
   判断依据是 ``catalog.reviewer_for(task.role)``，不是猜。
3. **自己审自己不做**。「同一个人换个身份再看一遍」只会白烧一次调用，还会让返工
   额度被无意义地消耗。
4. **主管驳回不受 score / 收敛旁路保护**。那两个旁路是对机器评审意见的容错，派活
   的人说不行就是不行——插入点在 ``Gatekeeper`` 里的旁路**之后**。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from agentcrew.gate.reviewer import Reviewer
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task

#: 主管审核的覆盖范围：``delegated`` = 只审动态委派任务；``all`` = 也审规划期任务。
SupervisorScope = Literal["delegated", "all"]


class SupervisorReviewer:
    """按「派活的人」（或配置的主管角色）评审任务产出。"""

    def __init__(
        self,
        reviewer: Reviewer,
        catalog: RoleCatalog,
        ledger: Ledger,
        *,
        scope: SupervisorScope = "delegated",
        fallback_role: str = "project_manager",
        escalation: Sequence[str] = (),
    ) -> None:
        """复用同一个 :class:`Reviewer` 跑调用与聚合，只是换了评审人来源。

        独立成一个薄类而不是往 ``Reviewer`` 里加分支：``Reviewer`` 的职责是
        「按目录评审」，主管是「按任务实例评审」，两者的评审人来源不同，混在
        一起会让 ``reviewer_for`` 的静态语义变得可疑。
        """
        self._reviewer = reviewer
        self._catalog = catalog
        self._ledger = ledger
        self._scope = scope
        self._fallback_role = fallback_role.strip()
        #: R33：直接主管之上的逐级复核链（空 = 单层，行为与 R33 之前一致）。
        self._escalation = [item.strip() for item in escalation if item.strip()]

    def _candidate_chain(self, task: Task) -> list[str]:
        """直接主管 + 逐级上级；无法确定主管时返回空列表（并留痕）。

        主管来源与 R7/R9 完全一致（``delegated_by`` 优先，``scope="all"`` 时回落
        配置角色），只是末尾追加 ``escalation``。去重留到 :meth:`review` 里做——
        那里还要判断「与执行者同角色」「已在 G2」等情况，统一在一处跳过更清楚。
        """
        supervisor = (task.delegated_by or "").strip()
        if not supervisor:
            if self._scope != "all":
                return []
            supervisor = self._fallback_role
            if not supervisor:
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor="",
                    reason="no_supervisor_configured",
                )
                return []
        return [supervisor, *self._escalation]

    async def review(self, task: Task, *, round_no: int) -> ReviewRecord | None:
        """按链逐级复核；任一级驳回立即短路（返回那条驳回记录）。

        返回 ``None`` 的各种情况都记一条 ``supervisor_review_skipped``——「为什么
        这个任务没有被主管验收」必须能从审计里回答，否则开关打开后看到的「全部
        通过」会让人误以为主管真的看过了。

        R33：链上每一级都留一条 ``supervisor_level``（层级、角色、裁决），
        这样「谁在第几层放了行」在 ledger 里是显式的；全部放行时返回**最后一层**
        的记录（越靠上越有决定权，人看记录时的直觉也是这个）。
        """
        chain = self._candidate_chain(task)
        if not chain:
            return None
        seen: set[str] = set()
        approved: ReviewRecord | None = None
        for level, role in enumerate(chain, start=1):
            role = role.strip()
            if not role:
                continue
            if role in seen:
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor=role,
                    reason="duplicate_level",
                    level=level,
                )
                continue
            seen.add(role)
            if role == task.role:
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor=role,
                    reason="self_delegated",
                    level=level,
                )
                continue
            if not self._catalog.has(role):
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor=role,
                    reason="unknown_supervisor",
                    level=level,
                )
                continue
            # 主管已经在 G2 评审人里：同一个角色以两种身份审两遍只是重复烧钱。
            if role in self._catalog.reviewer_for(task.role):
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor=role,
                    reason="already_in_g2",
                    level=level,
                )
                continue
            record = await self._reviewer.review(
                task,
                round_no=round_no,
                reviewer_keys=[role],
                layer="supervisor",
            )
            self._ledger.emit(
                "supervisor_level",
                task_id=task.id,
                level=level,
                levels=len(chain),
                supervisor=role,
                verdict=record.verdict,
            )
            if record.verdict != "approve":
                # 逐级短路：上级没点头就不再往上递，返工理由取这一级的意见。
                return record
            approved = record
        return approved

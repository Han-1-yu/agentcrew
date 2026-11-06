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

    async def review(self, task: Task, *, round_no: int) -> ReviewRecord | None:
        """审一次；不该审 / 审不了时返回 ``None``（调用方据此跳过）。

        返回 ``None`` 的各种情况都记一条 ``supervisor_review_skipped``——「为什么
        这个任务没有被主管验收」必须能从审计里回答，否则开关打开后看到的「全部
        通过」会让人误以为主管真的看过了。
        """
        supervisor = (task.delegated_by or "").strip()
        if not supervisor:
            if self._scope != "all":
                return None
            supervisor = self._fallback_role
            if not supervisor:
                self._ledger.emit(
                    "supervisor_review_skipped",
                    task_id=task.id,
                    supervisor="",
                    reason="no_supervisor_configured",
                )
                return None
        if supervisor == task.role:
            self._ledger.emit(
                "supervisor_review_skipped",
                task_id=task.id,
                supervisor=supervisor,
                reason="self_delegated",
            )
            return None
        if not self._catalog.has(supervisor):
            self._ledger.emit(
                "supervisor_review_skipped",
                task_id=task.id,
                supervisor=supervisor,
                reason="unknown_supervisor",
            )
            return None
        # 主管已经在 G2 评审人里：同一个角色以两种身份审两遍只是重复烧钱。
        if supervisor in self._catalog.reviewer_for(task.role):
            self._ledger.emit(
                "supervisor_review_skipped",
                task_id=task.id,
                supervisor=supervisor,
                reason="already_in_g2",
            )
            return None
        return await self._reviewer.review(
            task,
            round_no=round_no,
            reviewer_keys=[supervisor],
            layer="supervisor",
        )

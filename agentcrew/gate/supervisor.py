"""R7：谁派的活谁验收——hierarchical 监督回路。

R6 给动态委派加了四道闸，但只回答了「谁能派」，没回答「派出去的活谁验收」。
`delegation_mode=manager_only` 模式下经理可以把活派给下属，下属的产出仍然只由
目录里静态配置的 G2 评审人把关——经理本人对结果没有任何发言权，这不是 hierarchical。

这一层补的就是那个缺口：**动态委派任务的产出，先由派活的人过一遍**。

三条刻意的边界：

1. **只对委派任务生效**（``task.delegated_by`` 非空）。规划阶段的任务没有主管，
   行为与 R7 之前逐字一致——这是天然的作用域限制，不需要额外开关去圈。
2. **自己派给自己不审**。「同一个人换个身份再看一遍」只会白烧一次调用，还会
   让返工额度被无意义地消耗。
3. **主管驳回不受 score / 收敛旁路保护**。那两个旁路是对「机器评审意见」的容错
   （分数够高 / 意见收敛就放行），派活的人说不行就是不行——在 ``Gatekeeper``
   里的插入点因此放在旁路**之后**。
"""

from __future__ import annotations

from agentcrew.gate.reviewer import Reviewer
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.ledger import Ledger
from agentcrew.task.task import ReviewRecord, Task


class SupervisorReviewer:
    """按「派活的人」评审动态委派任务的产出。"""

    def __init__(
        self,
        reviewer: Reviewer,
        catalog: RoleCatalog,
        ledger: Ledger,
    ) -> None:
        """复用同一个 :class:`Reviewer` 跑调用与聚合，只是换了评审人来源。

        独立成一个薄类而不是往 ``Reviewer`` 里加分支：``Reviewer`` 的职责是
        「按目录评审」，主管是「按任务实例评审」，两者的评审人来源不同，混在
        一起会让 ``reviewer_for`` 的静态语义变得可疑。
        """
        self._reviewer = reviewer
        self._catalog = catalog
        self._ledger = ledger

    async def review(self, task: Task, *, round_no: int) -> ReviewRecord | None:
        """审一次；不该审 / 审不了时返回 ``None``（调用方据此跳过）。

        返回 ``None`` 的三种情况都记一条 ``supervisor_review_skipped``——「为什么
        这个委派任务没有被主管验收」必须能从审计里回答，否则开关打开后看到的
        「全部通过」会让人误以为主管真的看过了。
        """
        supervisor = (task.delegated_by or "").strip()
        if not supervisor:
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
        return await self._reviewer.review(
            task,
            round_no=round_no,
            reviewer_keys=[supervisor],
            layer="supervisor",
        )

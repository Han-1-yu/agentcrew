"""R5：跨角色咨询路由——把 ``<message to="…">`` 接成真正的 Agent 间协作。

改造前 ``Crew._route_message`` 只回一句「来自某角色的说明：<goal>」，模型发问
得到的是角色职责描述而不是答案，等于假协作。这里的 :class:`PeerRouter` 让
发问**真的触发目标角色的一次模型调用**（:meth:`~agentcrew.agents.base_agent.
BaseAgent.consult`），同时守住三条边界，全部以「返回可继续的文案」收场而不是
抛错——路由失败不该把在跑的任务打崩：

1. **未知角色 / 问自己**：目录里没有这个 key、或目标就是自己 → 直接给占位说明，
   不花一次调用（拼错角色名是最常见的浪费）；
2. **配额**：一次 run 最多 ``max_messages`` 次真实咨询（默认 8）。跨角色问答是
   指数级膨胀的入口，没有上限时一组互相求证的角色能把预算烧光；
3. **去重**：同一 ``(提问方, 目标, 问题摘要)`` 只真问一次，第二次直接复用答案。
   实测模型会在拿到答复后原样再问一遍，不去重就是纯粹的重复计费。

递归天然只有一层：:meth:`consult` 走的是**单次调用**，不解析 ``<message>``
围栏，所以不会出现 A 问 B、B 又问 A 的无限套娃。
"""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from agentcrew.runtime.ledger import Ledger

#: 问题摘要的截断长度：去重键不需要全文，200 字足以区分「同一个问题」。
_QUESTION_KEY_LIMIT = 200

#: R6：提问方当前任务的背景（标题 / 目标 / 交付契约），由 ``BaseAgent`` 在发问
#: 前后设置与还原。用 contextvar 而不是参数，是为了不破坏 ``message_router``
#: 既有的 ``(目标角色, 问题)`` 二参签名——同步夹具一行都不用改。
#:
#: 没有它时队友只能看见一句孤立的问题，长对话里会反复问「你们在做什么、
#: 要交什么文件」；带上背景后第一次就能问到点上。
CONSULT_CONTEXT: ContextVar[str] = ContextVar("agentcrew_consult_context", default="")


class PeerRouter:
    """把 ``(提问方, 目标角色, 问题)`` 变成目标角色的一次真实作答。"""

    def __init__(
        self,
        factory: Any,
        ledger: Ledger,
        *,
        max_messages: int = 8,
        on_consult: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        """``factory`` 需提供 ``has_role`` / ``consult``（见 ``AgentFactory``）。

        用 ``Any`` 而不是 ``AgentFactory`` 标注是为了不把 ``agents.registry``
        拖进本模块的导入链——测试里塞一个两方法的假工厂就能验全部边界。

        ``on_consult``（R6）在每次**真实**咨询成功后调用，签名
        ``(提问方, 目标角色, 问题, 答复)``。它存在的唯一原因是分层：``agents``
        层被架构约束禁止 import ``knowledge``，所以「把问答沉淀进经验库」必须
        由上层（Crew）以回调形式注入。
        """
        self._factory = factory
        self._ledger = ledger
        self._max_messages = max(0, int(max_messages))
        self._on_consult = on_consult
        self._used = 0
        self._cache: dict[tuple[str, str, str], str] = {}

    # ── 观测 ──────────────────────────────────────────────────────────────

    @property
    def used(self) -> int:
        """已消耗的真实咨询次数（不含被去重/被拒绝的）。"""
        return self._used

    @property
    def remaining(self) -> int:
        """剩余配额（``0`` 表示后续提问只会拿到「配额已用尽」文案）。"""
        return max(0, self._max_messages - self._used)

    # ── 入口 ──────────────────────────────────────────────────────────────

    def bind(self, from_role: str) -> Any:
        """按提问方绑定成 ``(目标角色, 问题) -> 协程`` 的二参回调（R5）。

        ``BaseAgent`` 只知道「我要问谁」，不知道自己的身份该由谁补——路由是
        整个 run 共享的一个对象，身份必须在这里按角色绑定，否则所有 Agent 发问
        都会被记到同一个 ``from_role`` 上。
        """
        async def router(to_role: str, content: str) -> str:
            return await self.ask(from_role, to_role, content)

        return router

    async def ask(self, from_role: str, to_role: str, content: str) -> str:
        """回答 ``from_role`` 向 ``to_role`` 提出的问题（可能是一次真实调用）。"""
        question = (content or "").strip()
        target = (to_role or "").strip()

        if not target or target == from_role:
            return "（不能向自己提问：请直接按你的判断继续完成本任务。）"

        if not self._factory.has_role(target):
            return (
                f"（团队里没有 {target!r} 这个角色："
                f"请按通用做法继续，不要再次向它提问。）"
            )

        key = (from_role, target, question[:_QUESTION_KEY_LIMIT])
        cached = self._cache.get(key)
        if cached is not None:
            self._ledger.emit(
                "agent_message_deduped",
                from_role=from_role,
                to_role=target,
            )
            return cached

        if self._used >= self._max_messages:
            self._ledger.emit(
                "agent_message_exhausted",
                from_role=from_role,
                to_role=target,
                max_messages=self._max_messages,
            )
            return (
                "（本次 run 的跨角色提问配额已用尽："
                "请依据已掌握的信息继续完成任务。）"
            )

        self._used += 1
        reply = await self._factory.consult(
            target, question, from_role=from_role,
            context=CONSULT_CONTEXT.get(),
        )
        self._cache[key] = reply
        if self._on_consult is not None:
            try:
                self._on_consult(from_role, target, question, reply)
            except Exception as exc:  # noqa: BLE001 —— 沉淀失败不该影响这次咨询
                self._ledger.emit(
                    "consult_capture_failed",
                    from_role=from_role,
                    to_role=target,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                )
        return reply

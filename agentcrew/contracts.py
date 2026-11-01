"""跨层结构契约（Protocol）：**零依赖**，可被任何包 import。

本模块只声明「形状」，不含实现、不 import 项目内任何模块。它存在的原因是
避免下层包反向依赖上层实现：

- ``models`` 是基础设施层（Provider 适配），却需要「记审计事件」。若直接
  ``from agentcrew.runtime.ledger import Ledger``，基础设施就反向依赖了运行时
  编排层 —— ledger 一旦改动，模型层跟着改。
- ``artifacts`` / ``models.complexity`` 只需要任务对象上的几个只读字段，若直接
  import ``agentcrew.task.task.Task``，产物仓库与模型层就绑死在任务领域模型上。

``typing.Protocol`` 是**结构化**契约：任何满足形状的对象都自动适配，无需注册、
无需继承。这与 ``artifacts/store.py`` 早已采用的 ``LedgerLike`` 是同一个思路，
现在把它提升为全项目共用的一份，避免每个包各写一个私有 Protocol 再各自漂移。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["EventSink", "TaskLike"]


@runtime_checkable
class EventSink(Protocol):
    """审计事件出口的结构化子集。

    :class:`agentcrew.runtime.ledger.Ledger` 是它的唯一生产实现；测试可用任意
    记录器（只实现 ``emit``）替代。返回值不在契约内 —— ``Ledger.emit`` 返回
    刚写入的事件对象，而调用方从不使用它。
    """

    def emit(self, event: str, **data: Any) -> object:
        """记录一次事件。"""
        ...


@runtime_checkable
class TaskLike(Protocol):
    """任务对象上「下游只读消费」的字段子集。

    ``models.complexity`` 按认知复杂度打分、``models.factory.alias_for_task``
    按角色挑模型、``artifacts.store`` 按契约比对产物 —— 三者都只需要读几个
    字段。用 Protocol 表达后，它们不再依赖 :class:`agentcrew.task.task.Task`
    的具体类，也不再随任务领域模型的演进被动改动。

    ``deliverables`` 的元素只要求有 ``path``（:class:`ArtifactSpec` 的形状），
    因为消费方也只用这个字段。
    """

    @property
    def id(self) -> str:
        """任务 id（``T-001``，run 内唯一）。"""
        ...

    @property
    def role(self) -> str:
        """执行角色 key（``RoleSpec.key``）。"""
        ...

    @property
    def description(self) -> str:
        """发给 Agent 的任务描述。"""
        ...

    @property
    def acceptance_criteria(self) -> list[str]:
        """验收标准列表。"""
        ...

    @property
    def dependencies(self) -> list[str]:
        """前置任务 id 列表。"""
        ...

    @property
    def deliverables(self) -> list[Any]:
        """交付物契约列表（元素至少有 ``path`` 属性）。"""
        ...

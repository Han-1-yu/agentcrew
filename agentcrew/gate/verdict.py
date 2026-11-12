"""评审裁决的数据契约与派生规则（G2 门禁与角色内复核共用）。

这套结构原本只属于 ``gate/reviewer.py``，但"复核该不该驳回"这件事在两个地方发生：

- **G2 门禁**：对整份产物按验收标准逐条判定；
- **角色内复核**（``agents/team.py``）：主笔产出后由另一个执行体复核，再决定是否修订。

两处若各写一份，迟早分叉——事实上分叉已经发生过一次：角色内复核一度是自由文本 +
子串匹配「未发现问题」，模型换个说法就会被当成"有问题"，触发无意义的主笔重跑。
所以裁决结构、派生规则、格式校验统一放在这里，**只此一份**。

设计要点（来自 G2 的实测教训，对复核同样成立）：

- **不要开放式结论**。让模型直接判 approve/reject，一个认真的评审员总能挑够毛病，
  任何非平凡任务都注定失败（实测问题数 7→12→8 不收敛）。
- **只问逐条判定**，裁决由 :func:`derive_review` 推导：有一条既定标准不满足才驳回。
- **「还能更好」不是驳回理由**，进 ``suggestions``，不参与裁决、不触发返工。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from agentcrew.task.task import Task


class CriterionJudgement(BaseModel):
    """对**一条**验收标准的判定。"""

    model_config = ConfigDict(extra="forbid")

    criterion_index: int
    """该判定对应 ``acceptance_criteria`` 里的第几条（**从 0 起**）。

    用序号而非让模型逐字复述标准：改写一个字就会让文本比对失败，进而触发
    「格式错误 → 重试 → 放行」，等于把评审悄悄关掉。序号是精确且稳健的。
    """

    satisfied: bool
    evidence: str
    """产物中支持该判定的具体依据（文件名 + 关键内容）。

    ``satisfied=False`` 时必须写成「产物里缺什么、要改成什么」—— 它会直接成为
    返工方看到的 ``fix_hints``，含糊的「不够好」等于没给。
    """


class ReviewVerdict(BaseModel):
    """一次 LLM 评审的原始裁决（``complete_structured`` 的目标 schema）。

    **刻意没有 ``verdict`` 字段。** 过去直接向模型要 approve/reject，等于让它对
    「这份产出好不好」下开放式结论 —— 一个认真的评审员总能在两轮内挑够毛病，
    于是**任何非平凡任务都注定 FAILED**（实测 run6：问题数 7→12→8 不收敛，
    返工额度烧光、下游 5 个任务阻塞）。

    现在只问它**逐条判定既定标准**，裁决由 :func:`derive_review` 推导：
    有一条不满足才 reject，且必须逐条覆盖、不能多也不能少。改进意见单独放
    ``suggestions``，**不构成不满足**、不引发返工 —— 「还能更好」不是驳回理由。
    """

    model_config = ConfigDict(extra="forbid")

    judgements: list[CriterionJudgement]
    suggestions: list[str] = []
    score: float = 0.0


#: 与 ``ReviewVerdict`` 派生自同一处定义，供需要裸 schema 的调用方使用
#: （实际调用由降级链按级别注入或经 ``response_format`` 携带，不重复内嵌）。
REVIEW_OUTPUT_SCHEMA: dict[str, object] = ReviewVerdict.model_json_schema()


def derive_review(verdict: ReviewVerdict, task: Task) -> tuple[bool, list[str], list[str]]:
    """由逐条判定推导裁决，返回 ``(approved, issues, fix_hints)``。

    **有一条验收标准不满足才 reject。** 这是把评审从「开放式代码评审」拉回
    「按验收标准评审」的那一步：评审员只能通过把某条**既定标准**判为
    不满足来驳回，而「还能更好」进 ``suggestions``、不参与裁决。

    标准原文由本函数按序号回填，不采信模型复述的文本 —— 引用的是契约里真正
    写下的那一条，返工方看到的和 PM 写下的必然一致。
    """
    criteria = task.acceptance_criteria
    unmet: list[CriterionJudgement] = []
    for judgement in verdict.judgements:
        if judgement.satisfied:
            continue
        unmet.append(judgement)
    issues = [
        f"未满足验收标准：{criteria[j.criterion_index]} —— {j.evidence.strip()}"
        for j in unmet
        if 0 <= j.criterion_index < len(criteria)
    ]
    hints = [j.evidence.strip() for j in unmet if 0 <= j.criterion_index < len(criteria)]
    return (not unmet, issues, hints)


def is_malformed(verdict: ReviewVerdict, task: Task) -> bool:
    """判定是否**不可用**，两种情况：

    1. **覆盖率不达标**：必须逐条判定 ``acceptance_criteria``，序号集合恰好等于
       ``0..N-1``。漏一条 = 逃避判据；多一条（或越界）= 引入了标准之外的驳回
       理由 —— 两者都会让裁决失去边界，正是过去不收敛的来源。
    2. **判了不满足却不给依据**：返工方无从下手，等同于旧版的「reject 无 issues」。
    """
    expected = set(range(len(task.acceptance_criteria)))
    got = {j.criterion_index for j in verdict.judgements}
    if got != expected or len(verdict.judgements) != len(expected):
        return True
    return any(not j.satisfied and not j.evidence.strip() for j in verdict.judgements)

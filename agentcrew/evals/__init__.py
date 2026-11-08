"""P2-5：evals —— 离线可跑的确定性回归 + ``pass^k`` 指标。

为什么需要它：前面几项改造（原生工具调用、guardrails、super-step）都在改**行为
面**。改完只看「测试还是绿的」不够——单测钉的是函数，不是「一次 run 出来的东西
还对不对」。evals 补的就是这一层：**拿固定的输入跑完整 run，断言产物内容**，
于是「改了提示词 / 换了模型 / 调了上限，效果变好还是变坏」变成可复现的数字。

三条设计约束（都来自本仓的既有教训）：

1. **离线、确定性**：case 绑定 mock 夹具（``examples/samples``），不碰网络、不烧
   token，CI 里能天天跑。真实模型的非确定性留给单独的「人工评审」通道，不进这道门。
2. **断言可读**：一条 case 的 checks 是「产物里有这个字符串 / 有这么多行 / 这个
   事件出现过」这种一句话能讲清的东西。不做 LLM-as-judge——那是另一回事，且失败
   时无法归因。
3. **指标别自欺**：一次通过说明不了稳定。所以有 ``repeat`` 与 ``pass^k``
   （同一 case 连跑 k 次**全过**才算过），以及每次都单独报 ``pass@1``。
   只报 ``pass@1`` 的评测，在真实模型上会给出虚高的结论。

用法：

```bash
python main.py eval examples/evals/smoke.jsonl            # 跑一遍
python main.py eval examples/evals/smoke.jsonl -k 3       # 每个 case 跑 3 次算 pass^3
python main.py eval examples/evals/smoke.jsonl --json     # 给 CI 解析
```
"""

from agentcrew.evals.case import (
    CaseOutput,
    CaseResult,
    CheckResult,
    DatasetError,
    EvalCase,
    load_dataset,
)
from agentcrew.evals.checks import evaluate_checks
from agentcrew.evals.runner import (
    EvalReport,
    compute_pass_pow_k,
    run_case,
    run_dataset,
)

__all__ = [
    "CaseOutput",
    "CaseResult",
    "CheckResult",
    "DatasetError",
    "EvalReport",
    "EvalCase",
    "compute_pass_pow_k",
    "evaluate_checks",
    "load_dataset",
    "run_case",
    "run_dataset",
]

"""成功经验沉淀与跨 run 知识复用（P3-5 / P3-7）。

一次 run 成功完成后，把「任务描述 → 解决方案 / 关键决策 / 产物」的映射从本 run
提取出来，追加到 ``output_dir/knowledge/experiences.jsonl`` —— 它**不**在任何单个
run 目录之下，而是全局共享：下一次 run 的规划阶段可以据此检索相关历史经验并注入
上下文，把「上次怎么做成的」变成「这次开局就知道」。
"""

from agentcrew.knowledge.experience import Experience, ExperienceStore

__all__ = ["Experience", "ExperienceStore"]

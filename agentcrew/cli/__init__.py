"""CLI 子命令实现包。

``main.py`` 是入口与**出口层组合根**：编码安全、日志、分派与退出码映射，以及
依赖 api 层的命令（serve / worker / mcp-serve / keys）——分层契约禁止 cli↔api
相互依赖，组合只能发生在包外的 main.py。参数解析在 :mod:`agentcrew.cli.parser`，
其余命令按域拆分在本包（run / status / catalog / providers / config_cmd /
eval_cmd / diagnostics），共享辅助在 :mod:`agentcrew.cli.common`。doctor / diff
的纯函数逻辑早在本包（R66f 前）。

历史调用点仍可从 ``main`` 取这些符号——``main.py`` 保留兼容再导出。
"""

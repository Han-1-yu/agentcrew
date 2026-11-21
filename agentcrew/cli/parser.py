"""CLI 参数解析：``build_parser`` 与 run 系开关（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """六个子命令（spec §15.1）。

    ``run`` 的 ``--goal`` / ``--demo`` **不用** argparse 的互斥组：互斥组在两者
    同时给出时会以退出码 2 直接结束进程，而这里要求「都没给 / 都给」一律报错并
    返回退出码 3（:data:`EXIT_CODES` 的 ``config``）。
    """
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="AgentCrew：8 角色多 Agent 协作平台（规划 → 执行 → 三层门禁 → 交付）",
    )
    sub = parser.add_subparsers(
        dest="command",
        metavar="{run,resume,rewind,status,approvals,approve,roles,agents,switch,"
                "clean,cancel,delete,serve,doctor,diff,schedule}")

    run = sub.add_parser("run", help="执行一次完整 run（规划 → 执行 → 交付）")
    run.add_argument("--goal", metavar="TEXT", help="项目目标（与 --demo 二选一）")
    run.add_argument(
        "--demo", action="store_true",
        help="使用 examples/todo_web_project.py 的内置 Todo Web 目标（与 --goal 二选一）")
    run.add_argument(
        "--flow", metavar="NAME|FILE",
        help="R28：用流程定义起跑（output/_flows 里的名字，或 .yaml 文件路径）——"
             "跳过 PM 规划，直接按画布/文件里的任务表执行；缺省目标取流程描述")
    _add_run_flags(run)

    resume = sub.add_parser("resume", help="续跑一个中断或未完成的 run")
    resume.add_argument("run_id", help="run 目录名（见 status / clean 的输出）")
    resume.add_argument(
        "--force", action="store_true", help="配置快照与当前配置不一致时仍继续")
    _add_run_flags(resume)

    rewind = sub.add_parser(
        "rewind", help="R62：回滚到某个任务并重跑（时间旅行）")
    rewind.add_argument("run_id", help="run 目录名")
    rewind.add_argument(
        "--task", required=True, metavar="ID",
        help="从哪个任务开始重做（该任务与其全部下游、T-FINAL 一并重置）")
    rewind.add_argument(
        "--instruction", default="", metavar="TEXT",
        help="重做时要额外满足的要求（写入该任务的描述与验收标准，G2 会据此评审）")
    rewind.add_argument(
        "--dry-run", action="store_true", help="只计算影响集，不写盘")
    rewind.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    rewind.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    approvals = sub.add_parser(
        "approvals", help="R4：列出一个 run 的审批单（含待裁决）")
    approvals.add_argument("run_id", help="run 目录名")
    approvals.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    approve = sub.add_parser(
        "approve", help="R4：对审批单写裁决（可在另一个进程里执行）")
    approve.add_argument("run_id", help="run 目录名")
    approve.add_argument("request_id", help="审批单 id（见 approvals 输出）")
    approve.add_argument(
        "--decision", required=True,
        choices=("approve", "cancel", "save_exit", "accept", "abort", "rework",
                 "step", "run"),
        help="裁决动作；plan/delivery/step 卡点各只接受自己的子集")
    approve.add_argument("--task-ids", nargs="*", default=[], metavar="ID",
                         help="rework 时要打回的任务 id")
    approve.add_argument("--by", default="cli", metavar="NAME",
                         help="裁决人标识（写进审批单留痕）")
    approve.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    status = sub.add_parser("status", help="打印一个 run 的记录与任务表")
    status.add_argument("run_id")
    status.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    guide = sub.add_parser(
        "guide", help="C10：给正在跑的 run 留言指引（下次任务派发时吸收）")
    guide.add_argument("run_id", help="run 目录名")
    guide.add_argument(
        "text", nargs="?", help="一句话指引（如「把登录页改成绿色」）")
    guide.add_argument(
        "--author", default="cli", metavar="NAME",
        help="指引人标识（写进 guidance.jsonl 留痕）")
    guide.add_argument(
        "--target", metavar="TASK_ID",
        help="C10 切片二：只把这条指引喂给指定任务（默认下一个派发的任务）")
    guide.add_argument(
        "--expires-in", type=int, metavar="SECONDS",
        help="C10 切片二：相对过期秒数（1..86400）")
    guide.add_argument(
        "--withdraw", metavar="GUIDANCE_ID",
        help="C10 切片二：撤回一条尚未被吸收的指引（不删除历史）")
    guide.add_argument(
        "--compact", action="store_true",
        help="C10 切片二：压缩指引历史（仅对已停止的 run 使用）")
    guide.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    guide.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")

    sub.add_parser("roles", help="列出 8 个内置角色（职责 / 边界 / 产出）")
    p_tools = sub.add_parser(
        "tools", help="R24：列出内置工具（名称 / 类别 / 入参 schema）")
    p_tools.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_tool = sub.add_parser(
        "tool", help="R24：单工具试跑（沙箱目录隔离，需工具执行已开启）")
    p_tool.add_argument("name", help="工具名（见 tools 命令）")
    p_tool.add_argument(
        "--args", default="{}", metavar="JSON",
        help='入参 JSON 对象，如 \'{"path": "a.txt"}\'')
    p_tool.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_tool.add_argument("--output-dir", metavar="DIR", help="试跑沙箱根目录")
    p_keys = sub.add_parser(
        "keys", help="R25：管理 API 密钥（多用户 RBAC：角色 / 作用域 / 归属）")
    keys_sub = p_keys.add_subparsers(dest="keys_action", required=True)
    p_keys_add = keys_sub.add_parser("add", help="新建密钥（明文只打印一次）")
    p_keys_add.add_argument("--name", required=True, help="身份名（审计与归属用）")
    p_keys_add.add_argument(
        "--role", default="operator",
        choices=("admin", "operator", "viewer", "custom"),
        help="角色（默认 operator；custom 需配 --scope）")
    p_keys_add.add_argument(
        "--scope", action="append", default=[], metavar="SCOPE",
        help="附加作用域，可多次：runs:read / runs:write / runs:admin / "
             "approvals:write / tools:read / tools:test / config:read / "
             "keys:read / keys:write")
    p_keys_add.add_argument(
        "--runs", default="", metavar="R1,R2",
        help="可见 run 白名单（逗号分隔）；缺省=只看自己创建的 run")
    p_keys_add.add_argument(
        "--expires", default=None, metavar="ISO8601",
        help="过期时间，如 2026-12-31T00:00:00")
    p_keys_add.add_argument("--note", default="", help="备注")
    p_keys_list = keys_sub.add_parser("list", help="列出密钥（不含哈希）")
    p_keys_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_keys_revoke = keys_sub.add_parser("revoke", help="吊销密钥（id 或 name）")
    p_keys_revoke.add_argument("ident", help="密钥 id 或 name")
    for _keys_parser in (p_keys_add, p_keys_list, p_keys_revoke):
        _keys_parser.add_argument(
            "--output-dir", metavar="DIR", help="output 目录（定位 _auth/keys.json）")
    p_flow = sub.add_parser(
        "flow", help="R26：可视化编排——校验 / 列表 / 导出示例流程 YAML")
    flow_sub = p_flow.add_subparsers(dest="flow_action", required=True)
    p_flow_v = flow_sub.add_parser("validate", help="校验一个流程 YAML 文件")
    p_flow_v.add_argument("file", help="流程文件路径（- 表示 stdin）")
    p_flow_l = flow_sub.add_parser("list", help="列出已保存流程")
    p_flow_l.add_argument("--json", action="store_true", help="输出 JSON")
    flow_sub.add_parser("sample", help="打印示例流程 YAML（可重定向成文件）")
    p_recall = sub.add_parser(
        "recall", help="R27：按查询词检索历史经验（验证 BM25 / 向量混合排序）")
    p_recall.add_argument("query", help="查询词 / 任务描述")
    p_recall.add_argument("--role", default=None, help="只检索该角色的经验")
    p_recall.add_argument("--limit", type=int, default=5, help="返回条数（默认 5）")
    p_recall.add_argument("--json", action="store_true", help="输出 JSON")
    for _flow_parser in (p_flow_v, p_flow_l):
        _flow_parser.add_argument(
            "--output-dir", metavar="DIR", help="output 目录（定位 _flows/）")
    p_agents = sub.add_parser(
        "agents", help="执行体档案：浏览 / 保存 / 删除 / 诊断 / 用量（--json 供 Studio 读取）")
    p_agents.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p_agents.add_argument("--yaml", action="store_true",
                          help="导出可复用的 agentcrew.yaml 片段（agents.profiles + roles 绑定）")
    p_agents.add_argument("--key", action="append", default=None, metavar="KEY",
                          help="只处理指定档案（可重复；配合 --yaml / 列表）")
    p_agents.add_argument("--save", metavar="FILE",
                          help="从 YAML 文件新建/更新档案（- = 从 stdin 读取；支持批量）")
    p_agents.add_argument("--delete", metavar="KEY", dest="delete",
                          help="删除 agents.d 里的档案（移入 .trash，可恢复）")
    p_agents.add_argument("--check", action="store_true",
                          help="诊断配置：错误 / 警告 / 提示逐条列出")
    p_agents.add_argument("--usage", action="store_true",
                          help="按执行体汇总最近 run 的调用量、成本与最近使用")
    p_agents.add_argument("--limit", type=int, default=30, metavar="N",
                          help="--usage 扫描的 run 数量上限（默认 30）")
    p_agents.add_argument("--force", action="store_true",
                          help="覆盖同名档案 / 删除仍被绑定的档案")
    p_agents.add_argument("--dry-run", action="store_true",
                          help="配合 --save：只校验并报告，不写盘")
    p_agents.add_argument("--output-dir", default=None,
                          help="output 目录（--usage 扫描 ledger 时使用）")
    p_switch = sub.add_parser("switch", help="给一个 run 写入执行体切换（下次 resume 生效）")
    p_switch.add_argument("run_id")
    p_switch.add_argument("role")
    p_switch.add_argument("agent", help="目标 agent profile（须已在 RunPlan 中）")
    p_switch.add_argument("--output-dir", default=None)
    p_switch.add_argument("--task", default="", metavar="TASK_ID",
                          help="只给这一个任务换（写 task_overrides.json）")

    # C5：文档 RAG——团队文档（knowledge/docs/）的索引管理与检索演示。
    p_docs = sub.add_parser(
        "docs", help="C5：团队文档 RAG——列出/检索 knowledge/docs/ 下的文档索引")
    docs_sub = p_docs.add_subparsers(dest="docs_action", required=True)
    p_docs_list = docs_sub.add_parser(
        "list", help="列出已索引的文档与块数（触发一次增量摄取）")
    p_docs_list.add_argument("--output-dir", default=None)
    p_docs_search = docs_sub.add_parser(
        "search", help="检索演示：按查询词返回最相关的文档片段")
    p_docs_search.add_argument("query", metavar="QUERY")
    p_docs_search.add_argument("--limit", type=int, default=4)
    p_docs_search.add_argument("--output-dir", default=None)

    p_schedule = sub.add_parser(
        "schedule", help="定时任务（Automations）：一次性 / 周期日程的增删查与到期触发")
    schedule_sub = p_schedule.add_subparsers(dest="schedule_action", required=True)
    p_sched_add = schedule_sub.add_parser("add", help="新建日程")
    p_sched_add.add_argument("--goal", required=True, metavar="TEXT", help="到期要跑的目标")
    p_sched_add.add_argument("--at", metavar="ISO8601", help="一次性：触发时间（如 2026-12-01T09:00:00）")
    p_sched_add.add_argument("--daily", metavar="HH:MM", help="周期：每天触发时间（如 09:00）")
    p_sched_add.add_argument("--weekdays", default="MO,TU,WE,TH,FR", metavar="MO,WE",
                             help="周期：命中星期（逗号分隔 MO..SU，默认工作日）")
    p_sched_add.add_argument("--mode", default="agent", choices=("agent", "plan", "ask"),
                             help="执行模式：agent=自动执行 / plan=计划审查 / ask=只读规划")
    p_sched_add.add_argument("--mock", action="store_true", help="用 mock provider 跑")
    p_sched_list = schedule_sub.add_parser("list", help="列出日程")
    p_sched_list.add_argument("--json", action="store_true", help="输出 JSON")
    p_sched_rm = schedule_sub.add_parser("remove", help="删除日程")
    p_sched_rm.add_argument("schedule_id", help="日程 id（见 list）")
    p_sched_on = schedule_sub.add_parser(
        "enable", help="启用日程（重算下次触发时间）")
    p_sched_on.add_argument("schedule_id", help="日程 id（见 list）")
    p_sched_on.add_argument("--json", action="store_true", help="输出 JSON")
    p_sched_off = schedule_sub.add_parser("disable", help="停用日程（保留在列表里）")
    p_sched_off.add_argument("schedule_id", help="日程 id（见 list）")
    p_sched_off.add_argument("--json", action="store_true", help="输出 JSON")
    p_sched_tick = schedule_sub.add_parser("tick", help="到期检查 + 触发（幂等，可高频调用）")
    p_sched_tick.add_argument("--json", action="store_true", help="输出本轮触发 JSON")
    for _sched_parser in (p_sched_add, p_sched_list, p_sched_rm, p_sched_on,
                          p_sched_off, p_sched_tick):
        _sched_parser.add_argument("--output-dir", metavar="DIR", help="output 目录（定位 _schedules/）")

    samples = sub.add_parser(
        "samples", help="列出预设 Crew 样例（串行 / 层级 / 混合并发）")
    samples.add_argument("name", nargs="?", help="只显示指定样例的详情")
    samples.add_argument("--json", action="store_true",
                         help="以 JSON 输出（便于脚本解析）")

    p_eval = sub.add_parser(
        "eval", help="P2-5：跑离线评测集（产物断言 + pass^k）")
    p_eval.add_argument("dataset", help="JSONL 数据集路径（一行一条 case）")
    p_eval.add_argument(
        "--repeat", type=int, default=1, metavar="K",
        help="每个 case 跑 K 次，用 pass^K 衡量稳定性（默认 1）")
    p_eval.add_argument("--json", action="store_true",
                        help="以 JSON 输出（便于 CI 解析）")
    p_eval.add_argument(
        "--output-dir", default=None, metavar="DIR",
        help="把每次 run 的目录留在这里便于排查（默认用临时目录并清理）")
    p_eval.add_argument(
        "--variant", action="append", default=None, metavar="NAME=FILE",
        help="R45：配置变体（可重复）。给了就进入 A/B 矩阵模式：同一批 case 分别用"
             "各变体跑一遍，第一个变体作基线。FILE 是 Settings 字段的 YAML/JSON"
             "（只写要覆盖的字段，如 agent_profiles / role_agents）")
    p_eval.add_argument(
        "--matrix-out", default=None, metavar="PATH",
        help="R45：把矩阵结果写成 JSON（默认不落盘，只在终端打印）")
    p_eval.add_argument(
        "--live", action="store_true",
        help="C11：走**真实模型**跑（默认 mock）。真实能力证据只能来自这里；"
             "建议配合 --max-cost 设硬上限，夜间限额跑")
    p_eval.add_argument(
        "--max-cost", type=float, metavar="USD",
        help="C11：本次评测的成本硬上限（真实通道必备护栏；超预算 run 会失败）")

    clean_cmd = sub.add_parser(
        "clean", help="保留最近 N 个 run，删除更旧的（默认 N=5）")
    clean_cmd.add_argument("--keep", type=int, default=5, metavar="N",
                           help="保留的 run 个数（默认 5）")
    clean_cmd.add_argument("--dry-run", action="store_true",
                           help="只列出将删除的 run，不真删")
    clean_cmd.add_argument(
        "--protect-running", action="store_true",
        help="跳过正在运行且有心跳的 run（生产环境建议开启；僵尸 run 仍可清理）")
    clean_cmd.add_argument("--output-dir", metavar="DIR")

    cancel = sub.add_parser(
        "cancel", help="给离线 run 打 cancelled 标记（下次 resume 直接收尾）")
    cancel.add_argument("run_id")
    cancel.add_argument("--output-dir", metavar="DIR")

    delete_cmd = sub.add_parser(
        "delete", help="删除单个 run（不可恢复；running 状态拒绝）")
    delete_cmd.add_argument("run_id", help="要删除的 run 目录名")
    delete_cmd.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    delete_cmd.add_argument("--force", action="store_true",
                            help="跳过确认提示（非交互环境/脚本用）")

    serve = sub.add_parser(
        "serve", help="启动 HTTP API + Web UI 面板（V2）")
    serve.add_argument(
        "--host", default="127.0.0.1", metavar="HOST",
        help="监听地址（默认只绑回环）。绑非回环地址时必须配置 API Key，"
             "否则拒绝启动")
    serve.add_argument("--port", type=int, default=8765, metavar="PORT")
    serve.add_argument(
        "--api-key", metavar="KEY",
        help="E-3：快速设置单个 API Key（等价 admin 角色；与 AGENTCREW_API_KEYS 合并）")
    # C14：队列模式——API 只入队、不本地执行；worker 在别处（可多机）消费。
    serve.add_argument(
        "--queue-dir", metavar="DIR", default=None,
        help="C14：启用队列模式（API 只入队，不本地跑 run）；"
             "默认 <output_dir>/_queue，多机时指向共享卷")
    serve.add_argument(
        "--lease-seconds", type=float, default=60.0,
        help="C14：worker 租约秒数（worker 侧同样参数；API 只用于建队列）")

    doctor = sub.add_parser(
        "doctor", help="诊断配置 / 环境 / 依赖 / Provider 连通性，输出健康报告")
    doctor.add_argument("--config", metavar="PATH",
                        help="指定 agentcrew.yaml 路径（缺省读工作目录下的同名文件）")
    doctor.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    doctor.add_argument("--json", action="store_true",
                        help="以 JSON 输出报告（便于脚本解析）")

    diff = sub.add_parser(
        "diff", help="对比两个 run 的配置 / 任务 / 产物 / 事件统计差异")
    diff.add_argument("run_id_a", help="第一个 run ID（基线）")
    diff.add_argument("run_id_b", help="第二个 run ID")
    diff.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    diff.add_argument("--json", action="store_true",
                      help="以 JSON 输出差异（便于脚本解析）")

    providers = sub.add_parser(
        "providers", help="列出合并后的 provider 目录（内置 + 自定义）")
    providers.add_argument("name", nargs="?", help="只显示指定 provider 的详情")
    providers.add_argument("--category",
                           choices=("aggregator", "national", "international",
                                     "local", "specialized"),
                           help="按业务分类过滤（aggregator/national/international/local/specialized）")
    providers.add_argument("--search", metavar="KEYWORD",
                           help="按 name / search_aliases 子串搜索（大小写不敏感）")
    providers.add_argument("--json", action="store_true",
                           help="以 JSON 输出目录（便于脚本解析）")

    models = sub.add_parser(
        "models", help="按需拉取指定 provider 的模型列表")
    models.add_argument("provider", help="provider 名（如 openai / openrouter）")
    models.add_argument("--verbose", "-v", action="store_true",
                        help="显示端点候选尝试过程详情")
    models.add_argument("--json", action="store_true",
                        help="以 JSON 输出模型列表（便于脚本解析）")

    config = sub.add_parser("config", help="配置导入导出")
    config_sub = config.add_subparsers(dest="config_action")

    # C7：把 AgentCrew 的编排能力（派活/看进度/裁决审批）经 MCP 暴露给
    # Claude Desktop / Cursor 类客户端。stdio 传输；工具调用走本机 HTTP API，
    # RBAC 与审计由 API 侧执行——MCP 入口不产生第二条权限语义。
    p_mcp = sub.add_parser(
        "mcp-serve", help="C7：以 MCP server（stdio）暴露编排能力，"
                          "供 Claude Desktop / Cursor 等客户端调用")
    p_mcp.add_argument("--api-url", default="http://127.0.0.1:8765",
                       metavar="URL", help="AgentCrew API 地址（默认本机 8765）")
    p_mcp.add_argument("--api-key", default=None, metavar="KEY",
                       help="调用 API 用的密钥（默认读 AGENTCREW_MCP_API_KEY，"
                            "再退到 AGENTCREW_API_KEY）；密钥不落盘")
    # C14：分布式执行 worker——从文件系统队列领取 run，多进程/多机水平扩展。
    # 队列与产物放同一块共享卷即可跨机，不需要 Redis / RabbitMQ。
    p_worker = sub.add_parser(
        "worker", help="C14：从共享队列领取 run 并执行（可多进程/多机）")
    p_worker.add_argument("--queue-dir", default=None, metavar="DIR",
                          help="队列根目录（默认 <output_dir>/_queue）")
    p_worker.add_argument("--output-dir", default=None, metavar="DIR",
                          help="产物目录（默认配置值；多机时指向共享卷）")
    p_worker.add_argument("--concurrency", type=int, default=4,
                          help="并发 run 数（默认 4）")
    p_worker.add_argument("--lease-seconds", type=float, default=60.0,
                          help="租约秒数（默认 60；崩溃后约这么久被回收）")
    p_worker.add_argument("--poll-seconds", type=float, default=2.0,
                          help="空闲轮询间隔（默认 2 秒）")
    p_worker.add_argument("--once", action="store_true",
                          help="处理完当前队列即退出（烟测/CI）")
    p_worker.add_argument("--mock", action="store_true",
                          help="强制 mock provider（离线压测，无需 API Key）")
    p_worker.add_argument("--auto", action="store_true",
                          help="等价 gate=none：允许无人值守跑完（gate 含人工"
                               "卡点时默认拒绝启动）")

    export_p = config_sub.add_parser("export", help="导出当前合并配置为 YAML")
    export_p.add_argument("--output", "-o", metavar="FILE",
                          help="写入文件而非 stdout")
    import_p = config_sub.add_parser("import", help="从 YAML 文件导入配置")
    import_p.add_argument("file", help="要导入的 YAML 文件路径")

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    """``run`` / ``resume`` 共用的配置与看板开关。"""
    parser.add_argument(
        "--sample", metavar="NAME",
        help="使用预设样例（见 `samples` 子命令）：同时设置目标与 mock 夹具，"
             "并自动开启 --mock。run 时与 --goal / --demo 互斥。")
    parser.add_argument("--mock", action="store_true",
                        help="强制 mock provider（覆盖所有角色绑定，无需 API Key）")
    # C4：随目标附带的图片（多模态输入）。可重复（最多 4 张）；单张 ≤2.6 MB，
    # png/jpg/webp/gif。是否真发给模型由 provider 的 supports_vision 能力位决定，
    # 不支持时降级为纯文本并落 vision_unsupported 事件（降级可见，不静默）。
    parser.add_argument(
        "--image", action="append", default=None, metavar="PATH",
        help="随目标附带图片（可重复，最多 4 张；单张 ≤2.6 MB，png/jpg/webp/gif）。"
             "仅在 provider 开启 capabilities.supports_vision 时真正生效")
    parser.add_argument("--auto", action="store_true",
                        help="等价于 --gate none（全自动，跳过人工闸门）")
    parser.add_argument("--plan-only", action="store_true",
                        help="Ask 模式：只生成计划并落盘（不派发执行），可用 resume 续跑；"
                             "与 --auto / --gate 互斥")
    parser.add_argument("--debug-step", action="store_true",
                        help="R20：单步断点——每个任务派发前挂起为 step 审批单，"
                             "用 approve --decision step|run|abort 逐个放行")
    parser.add_argument("--gate", choices=("both", "plan", "delivery", "none"),
                        help="人工闸门模式；含人工卡点但无 TTY 时降级为 none")
    parser.add_argument("--max-concurrency", type=int, metavar="N",
                        help="全局并发上限（含 LLM 调用）")
    parser.add_argument("--default-model", metavar="ALIAS",
                        help="默认模型别名，形如 provider:model")
    parser.add_argument("--output-dir", metavar="DIR", help="run 输出根目录")
    parser.add_argument("--context-budget", type=int, metavar="N",
                        help="上下文预算（tokens）")
    parser.add_argument("--max-cost", type=float, metavar="USD",
                        help="累计 LLM 成本硬上限（USD），超限优雅收尾（P0-3）")
    parser.add_argument("--max-llm-calls", type=int, metavar="N",
                        help="累计 LLM 调用次数硬上限，超限优雅收尾（P0-3）")
    parser.add_argument(
        "--approval-mode", choices=("interactive", "deferred"),
        help="R4：人工卡点实现方式。interactive（默认）= TTY 同步问答；"
             "deferred = 审批单落盘后 run 挂起，用 approve 命令在另一个进程裁决再 resume")
    parser.add_argument("--no-live", action="store_true",
                        help="关闭实时看板，只保留结束报告（CI 友好）")
    parser.add_argument("--trigger-source", metavar="SOURCE", default=None,
                        help="谁发起的这次 run（如 schedule:sched_xxx），落进 ledger 的 "
                             "run_triggered 事件；定时任务的历史据此关联")


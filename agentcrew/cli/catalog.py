"""角色/工具/密钥/流程/调度/咨询等目录类命令（R66f 自 main.py 拆出）。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

from agentcrew.cli.common import _fail, _out, _run_dir, _settings_from
from agentcrew.config import Settings
from agentcrew.crew.crew import (
    EXIT_CODES,
)
from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.runtime.guidance import GuidanceStore
from agentcrew.utils.clock import format_local, parse_stamp, utc_now


def _cmd_guide(args: argparse.Namespace) -> int:
    """C10：在另一个进程里对正在跑的 run 说一句话（切片二：定向 / 过期 / 撤回 / 压缩）。

    与 HTTP ``POST /runs/{id}/guidance`` 写同一个 store。CLI **不**校验 run 状态：
    给已结束的 run 留言会落盘但永不吸收（``GET`` / ``status`` 仍可读），这是审计
    用途；限定「只在跑着时才能说」是 API 控制面的口径，不该让终端也失忆。
    是否真的被吸收取决于 ``guidance_enabled``（默认关）。

    ``--compact`` 会原子替换 guidance.jsonl，只应对**已停止**的 run 使用；
    API 侧在终态才放行，终端不做状态判断由调用方负责。
    """
    try:
        settings = _settings_from(args)
        run_dir = _run_dir(settings, args.run_id)
    except (OSError, ValueError) as exc:
        return _fail(f"无法读取 run {args.run_id}：{exc}")
    store = GuidanceStore(run_dir, secrets=tuple(settings.secret_values()))
    if args.withdraw:
        if not store.withdraw(args.withdraw):
            return _fail(f"指引 {args.withdraw} 不存在或已被吸收")
        pending = store.pending_count()
        if args.json:
            print(json.dumps({"id": args.withdraw, "withdrawn": True,
                              "pending": pending}, ensure_ascii=False))
        else:
            print(f"已撤回指引 {args.withdraw}；待吸收 {pending} 条")
        return EXIT_CODES["ok"]
    if args.compact:
        try:
            result = store.compact()
        except OSError as exc:
            return _fail(f"指引压缩失败：{exc}")
        if args.json:
            print(json.dumps({"run_id": args.run_id, **result},
                             ensure_ascii=False))
        else:
            print(f"指引历史已压缩：{result['before_lines']} 行 → "
                  f"{result['after_lines']} 行（保留 {result['kept']} 条，"
                  f"清理 {result['removed']} 条）")
        return EXIT_CODES["ok"]
    if not args.text:
        return _fail("请给出指引文本，或用 --withdraw / --compact")
    expires_at = None
    if args.expires_in is not None:
        if args.expires_in <= 0 or args.expires_in > 86400:
            return _fail("--expires-in 必须是 1..86400 之间的秒数")
        from datetime import timedelta

        expires_at = (
            utc_now() + timedelta(seconds=args.expires_in)).isoformat()
    try:
        item = store.append(
            args.text, args.author, target=args.target,
            expires_at=expires_at)
    except ValueError as exc:
        return _fail(str(exc))
    except OSError as exc:
        return _fail(f"指引写入失败：{exc}")
    pending = store.pending_count()
    if args.json:
        print(json.dumps({
            "id": item.id, "pending": pending,
            "target": item.target, "expires_at": item.expires_at,
        }, ensure_ascii=False))
        return EXIT_CODES["ok"]
    print(f"已记录指引 {item.id}（run {args.run_id}）")
    if item.target:
        print(f"定向到 {item.target}；未派发到该任务前不会被吸收")
    if item.expires_at:
        # 指引的 expires_at 是**字符串**（aware UTC，同 D-14）：先解析再按本机时区
        # 显示，否则用户读到的是一串带 +00:00 的 UTC 时刻。
        print(f"过期时刻 {format_local(parse_stamp(item.expires_at))}（到点后不再吸收）")
    print(f"待吸收 {pending} 条；引擎在下一次任务派发前读取"
          "（需 guidance_enabled=true）")
    return EXIT_CODES["ok"]


def _cmd_roles(args: argparse.Namespace) -> int:
    """列出 8 个内置角色：key / 角色 / 职责 / 边界 / 产出（Task 34 的 README 指向这里）。

    逐角色分块，而不是一张五列表：80 列的终端里五列表会把「职责/边界/产出」挤到
    每列十几个字，rich 还会把放不下的 key 截成 ``project_mana…`` —— 本命令存在的
    意义就是让人拿到完整的 key 与契约。每一行都走 ``soft_wrap``：内容只换行、不截断。
    """
    del args  # 无参数：角色表来自 BUILTIN_ROLES
    console = Console()
    from agentcrew.agents.plan import binding_for
    from agentcrew.config import Settings
    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    for spec in BUILTIN_ROLES:
        _out(console, f"[{spec.key}] {spec.title} —— {spec.goal}")
        _out(console, f"  职责  {'；'.join(spec.responsibilities)}")
        _out(console, f"  边界  {'；'.join(spec.boundaries)}")
        _out(console, f"  产出  {spec.output_contract}")
        binding = binding_for(spec, settings)
        if binding.agents:
            chain = " + ".join(f"{b.ref}:{b.duty.value}" for b in binding.agents)
            mode = binding.aggregation.mode if binding.aggregation else "single"
            _out(console, f"  执行体  {chain}（汇总：{mode}）")
        _out(console, "")
    _out(
        console,
        f"共 {len(BUILTIN_ROLES)} 个内置角色；用 --gate plan 可在执行前人工确认拆解。",
    )
    return EXIT_CODES["ok"]


def _cmd_docs(args: argparse.Namespace) -> int:
    """C5：文档 RAG 的管理入口——``docs list`` / ``docs search``。

    ``list`` 触发一次增量摄取并打印「每篇文档的块数」——这是「我的手册被
    索引了吗」的直接答案。``search`` 是检索演示：run 内注入用的同一套
    BM25 + 混合打分，给用户一个「放对目录了没有」的快速验证手段。
    """
    import asyncio as _asyncio

    from agentcrew.config import Settings as _Settings
    from agentcrew.knowledge.docs import DocStore
    from agentcrew.knowledge.embeddings import build_embedder
    from agentcrew.models.embedding import embedding_target

    settings = _Settings.load()
    if getattr(args, "output_dir", None):
        settings = settings.model_copy(
            update={"output_dir": Path(args.output_dir)})
    embedder = build_embedder(
        settings, output_dir=settings.output_dir,
        target=embedding_target(settings))
    store = DocStore(
        settings.output_dir / "knowledge" / "docs", embedder=embedder)
    result = _asyncio.run(store.ingest())
    if getattr(args, "docs_action", "") == "list":
        console = Console()
        _out(console, f"文档根目录：{store.docs_dir}")
        _out(console, f"已索引 {result.docs} 篇 / {result.chunks} 块"
                      f"（本次重建 {len(result.rebuilt)}、复用 {len(result.skipped)}）")
        by_doc: dict[str, int] = {}
        for chunk in store._chunks:  # noqa: SLF001 —— 同仓管理命令直接读
            by_doc[chunk.doc] = by_doc.get(chunk.doc, 0) + 1
        for name, count in sorted(by_doc.items()):
            _out(console, f"  {name} × {count} 块")
        return EXIT_CODES["ok"]
    query = args.query
    # query_sync 是同步方法；上面 asyncio.run 只包了 ingest（异步摄取）。
    hits = store.query_sync(
        query, limit=max(1, int(args.limit)),
        vector_weight=settings.docs_rag_vector_weight)
    console = Console()
    if not hits:
        _out(console, f"没有命中（查询：{query!r}）。检查文档是否已放进 "
                      f"{store.docs_dir} 且主题相关。")
        return EXIT_CODES["ok"]
    _out(console, f"查询：{query!r} · 命中 {len(hits)} 条：")
    for rank, hit in enumerate(hits, start=1):
        _out(console, f"{rank}. [{hit.score:.3f}] {hit.chunk.doc}"
                      f" · {hit.chunk.heading}")
        _out(console, f"   {hit.chunk.text[:160]}…")
    return EXIT_CODES["ok"]


def _cmd_tools(args: argparse.Namespace) -> int:
    """R24：列出内置工具（名称 / 类别 / 必填入参）。管理页与 API 共用同一份元数据。"""
    from agentcrew.tools.lab import describe_tools

    # P1：run_python 只在 code_exec_enabled 打开时进清单（与 run 内注册表同口径）。
    settings = Settings.load()
    tools = describe_tools(
        code_exec=settings.code_exec_enabled,
        search_provider=settings.search_provider)
    if args.json:
        print(json.dumps({"tools": tools}, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    console = Console()
    for item in tools:
        required = (item["input_schema"] or {}).get("required") or []
        params = "、".join(required) if required else "无必填"
        _out(console, f"[{item['name']}] {item['kind']} —— 必填：{params}")
        _out(console, f"  {item['description']}")
    _out(
        console,
        "单工具试跑：python main.py tool <名称> --args '{\"path\": \"a.txt\"}'"
        "（需先开启工具执行：AGENTCREW_TOOLS_ENABLED=true）",
    )
    return EXIT_CODES["ok"]


def _cmd_tool(args: argparse.Namespace) -> int:
    """R24：单工具试跑——沙箱目录隔离，与 Agent 调用同一条入参校验。

    退出码：``ok``=0；工具运行时失败=1（试跑结果本身，不是 CLI 错误）；
    未开工具执行 / 入参非法 / 未知工具=3（配置类，与其他 ``_fail`` 一致）。
    """
    from agentcrew.config import Settings
    from agentcrew.tools.lab import ToolLabError, run_trial

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if args.output_dir else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    if not settings.tools_enabled:
        return _fail(
            "工具执行未开启：设置 AGENTCREW_TOOLS_ENABLED=true（或 YAML "
            "tools_enabled: true）后重试；调试页不扩大执行面"
        )
    try:
        payload = json.loads(args.args)
    except json.JSONDecodeError as exc:
        return _fail(f"--args 不是合法 JSON：{exc}")
    if not isinstance(payload, dict):
        return _fail("--args 必须是 JSON 对象")
    from agentcrew.tools.lab import build_backend_from_config

    try:
        backend = build_backend_from_config(
            kind=settings.sandbox_backend,
            image=settings.sandbox_image,
            memory_mb=settings.sandbox_memory_mb,
            cpus=settings.sandbox_cpus,
            allow_network=settings.sandbox_allow_network,
        )
    except Exception as exc:  # noqa: BLE001 —— 配置类错误统一退 3
        return _fail(f"执行后端不可用：{exc}")
    try:
        result = asyncio.run(
            run_trial(args.name, payload, output_dir=settings.output_dir,
                      code_exec=settings.code_exec_enabled,
                      search_provider=settings.search_provider,
                      backend=backend))
    except ToolLabError as exc:
        return _fail(str(exc))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        console = Console()
        state = "成功" if result["ok"] else "失败"
        _out(
            console,
            f"{result['tool']}  {state}  {result['duration_ms']}ms  "
            f"沙箱 {result['sandbox']}",
        )
        if result["result"]:
            _out(console, result["result"])
        if result["error"]:
            _out(console, f"错误：{result['error']}")
    return EXIT_CODES["ok"] if result["ok"] else EXIT_CODES["failed"]


def _cmd_flow(args: argparse.Namespace) -> int:
    """R26：流程 YAML 的校验 / 列表 / 示例输出。

    退出码：``ok``=0；流程不合法=2（``plan``——能改的东西，不是配置错误）；
    文件读不了=3。
    """
    from agentcrew.flow.spec import (
        FlowError,
        dump_flow_yaml,
        list_flows,
        parse_flow_yaml,
        sample_flow,
        validate_flow,
    )

    action = args.flow_action
    console = Console()
    if action == "sample":
        _out(console, dump_flow_yaml(sample_flow()))
        return EXIT_CODES["ok"]

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if getattr(args, "output_dir", None) else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    if action == "list":
        items = list_flows(settings.output_dir)
        if getattr(args, "json", False):
            print(json.dumps({"flows": items}, ensure_ascii=False, indent=2))
            return EXIT_CODES["ok"]
        if not items:
            _out(console, f"没有已保存的流程：{settings.output_dir / '_flows'}")
            _out(console, "示例：python main.py flow sample > my-flow.yaml")
            return EXIT_CODES["ok"]
        for item in items:
            state = "合法" if item["errors"] == 0 else f"{item['errors']} 处错误"
            _out(console, f"[{item['name']}] {item['tasks']} 个任务 · {state}")
            if item.get("error"):
                _out(console, f"  {item['error']}")
        return EXIT_CODES["ok"]

    # validate
    try:
        text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(
            encoding="utf-8")
    except OSError as exc:
        return _fail(f"读不到流程文件：{exc}")
    try:
        spec = parse_flow_yaml(text)
    except FlowError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CODES["plan"]
    errors = validate_flow(spec)
    if errors:
        _out(console, f"流程不合法（{len(errors)} 处）：{spec.name}")
        for error in errors:
            _out(console, f"  - {error}")
        return EXIT_CODES["plan"]
    _out(console, f"流程合法：{spec.name} · {len(spec.tasks)} 个任务")
    return EXIT_CODES["ok"]


def _cmd_schedule(args: argparse.Namespace) -> int:
    """定时任务（Automations）：一次性 / 周期日程的增删查与到期触发。

    对齐 WorkBuddy 的 Automations：``add`` 注册日程，``tick`` 由宿主（Studio
    主进程定时器 / 系统 cron）幂等调用、到期后起 ``run`` 子进程。不引入常驻
    daemon —— 谁负责「到点叫 tick」由部署方决定，CLI 本身零后台。
    """
    from agentcrew.cli.schedule import (
        add_schedule,
        load_schedules,
        remove_schedule,
        tick,
    )

    try:
        settings = (
            Settings.load(output_dir=Path(args.output_dir))
            if getattr(args, "output_dir", None) else Settings.load()
        )
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")

    action = args.schedule_action
    console = Console()

    if action == "add":
        kind = "recurring" if args.daily else "once"
        if kind == "once" and not args.at:
            return _fail("一次性日程需要 --at（ISO8601）")
        if kind == "recurring" and not args.daily:
            return _fail("周期日程需要 --daily HH:MM")
        weekdays = [w.strip().upper() for w in args.weekdays.split(",") if w.strip()]
        try:
            sched = add_schedule(
                settings.output_dir,
                goal=args.goal,
                mock=args.mock,
                mode=args.mode,
                kind=kind,
                at=args.at if kind == "once" else None,
                weekdays=weekdays if kind == "recurring" else [],
                time=args.daily if kind == "recurring" else None,
            )
        except ValueError as exc:
            return _fail(str(exc))
        _out(console, f"已注册日程 {sched.id}（{sched.kind}/{sched.mode}），"
                      f"下次触发 {sched.next_run}")
        _out(console, f"  goal: {sched.goal}")
        return EXIT_CODES["ok"]

    if action == "list":
        schedules = load_schedules(settings.output_dir)
        if args.json:
            print(json.dumps(
                {"schedules": [s.model_dump(mode="json") for s in schedules]},
                ensure_ascii=False, indent=2))
            return EXIT_CODES["ok"]
        if not schedules:
            _out(console, f"没有日程：{settings.output_dir / '_schedules'}")
            _out(console, "示例：python main.py schedule add --goal '写周报' "
                          "--daily 09:00 --mode ask")
            return EXIT_CODES["ok"]
        for s in schedules:
            state = "启用" if s.enabled else "已触发/停用"
            when = s.next_run or "—"
            _out(console, f"[{s.id}] {state} · {s.kind}/{s.mode} · 下次 {when}")
            _out(console, f"  {s.goal}")
        return EXIT_CODES["ok"]

    if action == "remove":
        ok = remove_schedule(settings.output_dir, args.schedule_id)
        if not ok:
            return _fail(f"找不到日程：{args.schedule_id}")
        _out(console, f"已删除日程 {args.schedule_id}")
        return EXIT_CODES["ok"]

    # tick
    fired = tick(settings.output_dir)
    if args.json:
        print(json.dumps({"fired": fired}, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    if not fired:
        _out(console, "没有到期日程。")
    else:
        _out(console, f"本轮触发 {len(fired)} 个日程：")
        for item in fired:
            _out(console, f"  [{item['id']}] {item['kind']}/{item['mode']} — {item['goal']}")
    return EXIT_CODES["ok"]


def _cmd_recall(args: argparse.Namespace) -> int:
    """R27：检索历史经验，打印排序结果——手工核对 BM25 / 混合打分的入口。

    ``embedding_enabled`` 时用与 run 内相同的混合打分（同一 ``build_embedder``
    与权重）；未启用或端点失败时自动退回纯 BM25，并在输出里写明用了哪条通道。
    """
    from agentcrew.knowledge.embeddings import EmbeddingError, build_embedder
    from agentcrew.knowledge.experience import ExperienceStore

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    store = ExperienceStore(settings.output_dir)
    experiences = store.all()
    if not experiences:
        _out(Console(), f"经验库为空：{settings.output_dir / 'knowledge'}")
        return EXIT_CODES["ok"]

    doc_vectors = query_vector = None
    channel = "bm25"
    from agentcrew.models.embedding import embedding_target

    embedder = build_embedder(settings, output_dir=settings.output_dir,
                              target=embedding_target(settings))
    if embedder is not None:
        try:
            doc_vectors = asyncio.run(
                embedder.embed([store.haystack(exp) for exp in experiences]))
            query_vector = asyncio.run(embedder.embed([args.query]))[0]
            channel = embedder.name
        except EmbeddingError as exc:
            print(f"embedding 降级为 BM25：{exc}", file=sys.stderr)

    matches = store.query(
        args.query, role=args.role, limit=max(1, args.limit),
        doc_vectors=doc_vectors, query_vector=query_vector,
        vector_weight=settings.retrieval_vector_weight)
    if args.json:
        print(json.dumps({
            "channel": channel, "query": args.query,
            "results": [{"task": exp.task_description, "role": exp.role,
                         "solution": exp.solution_summary} for exp in matches],
        }, ensure_ascii=False, indent=2))
        return EXIT_CODES["ok"]
    console = Console()
    _out(console, f"检索「{args.query}」（通道 {channel} · 权重 "
                  f"{settings.retrieval_vector_weight}）：{len(matches)} 条")
    for index, exp in enumerate(matches, start=1):
        _out(console, f"{index}. [{exp.role}] {exp.task_description}")
        _out(console, f"   {exp.solution_summary}")
    return EXIT_CODES["ok"]


def _cmd_switch(args: argparse.Namespace) -> int:
    """把「某角色用哪个执行体」写进 run 目录，resume 时自动恢复。

    进程外的 CLI 改不了正在跑的进程：这里落盘的是**下一次 resume** 的绑定。
    进程内切换由 ``Crew.switch_agent`` 提供（效果相同，且立即对下一次派发生效）。
    """
    import json

    from agentcrew.agents.plan import ResolvedAgent, resolve_plan
    from agentcrew.config import Settings
    from agentcrew.crew.crew import build_catalog
    from agentcrew.roles.binding import AgentProfile, Duty

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    if args.output_dir:
        settings = settings.model_copy(update={"output_dir": Path(args.output_dir).resolve()})
    # 与 status / resume / cancel / delete / diff 同一条边界：switch 会往 run 目录
    # 写两个 JSON，run_id 必须先收进 output_dir 内，否则 ``../`` 会写到别处。
    try:
        run_dir = _run_dir(settings, args.run_id)
    except ValueError as exc:
        return _fail(f"非法 run_id：{exc}")
    if not run_dir.is_dir():
        print(f"run 目录不存在：{run_dir}", file=sys.stderr)
        return EXIT_CODES["config"]

    catalog = build_catalog(settings)
    plan = resolve_plan(settings, catalog, args.run_id)
    members = plan.for_role(args.role)
    target: ResolvedAgent | AgentProfile | None = next(
        (a for a in members if a.profile == args.agent), None)
    if target is None and args.agent in settings.agent_profiles:
        # 与引擎一致：允许切到"已定义但当前没绑"的执行体
        target = settings.agent_profiles[args.agent]
    if target is None:
        options = ", ".join(sorted(set(list(settings.agent_profiles) +
                                       [a.profile for a in members]))) or "无"
        print(f"角色 {args.role} 没有可用的 agent：{args.agent}（可选：{options}）",
              file=sys.stderr)
        return EXIT_CODES["config"]

    # 覆盖文件损坏时**不能**当成空对象再写回：那会静默抹掉用户已有的全部覆盖，
    # 且没有任何提示。fail loud，让用户先修复或自行删除。
    def _read_overrides(path: Path) -> dict:
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"覆盖文件损坏：{path}（{exc}）；请修复或删除后再 switch") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"覆盖文件格式错误：{path} 必须是 JSON 对象")
        return payload

    if args.task:
        # 任务级覆盖：只影响这一次任务的派发，与角色绑定互不干扰
        path = run_dir / "task_overrides.json"
        try:
            payload = _read_overrides(path)
        except ValueError as exc:
            return _fail(str(exc))
        payload[args.task] = {"role": args.role, "profile": args.agent}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {path}：{args.task}（{args.role}）→ {args.agent}（下次 resume 生效）")
        return EXIT_CODES["ok"]

    path = run_dir / "binding_overrides.json"
    try:
        payload = _read_overrides(path)
    except ValueError as exc:
        return _fail(str(exc))
    kept = [a for a in members
            if a.duty is not Duty.PRIMARY and a.profile != args.agent]
    payload[args.role] = {
        "agents": [{"ref": a.profile, "duty": a.duty.value, "weight": a.weight} for a in kept]
                  + [{"ref": args.agent, "duty": "primary", "weight": 1.0}],
        "aggregation": ({"mode": "primary_with_review", "quorum": 1}
                        if any(a.duty is Duty.REVIEW for a in kept) else None),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {path}：{args.role} → {args.agent}（下次 resume 生效）")
    return EXIT_CODES["ok"]


def _cmd_agents(args: argparse.Namespace) -> int:
    """列出 agent 档案与角色绑定；``--json`` 给 Studio 的绑定矩阵用。

    数据来自「Settings + RoleCatalog + RunPlan」三层，和真正跑任务时的解析
    完全同源——UI 看到的编排必须就是执行时会用的编排。
    """
    from agentcrew.agents.plan import binding_for, resolve_plan
    from agentcrew.config import Settings
    from agentcrew.crew.crew import build_catalog

    try:
        settings = Settings.load()
    except (OSError, ValueError) as exc:
        return _fail(f"配置错误：{exc}")
    catalog = build_catalog(settings)
    plan = resolve_plan(settings, catalog, "preview")

    roles: list[dict[str, Any]] = []
    for spec in catalog.all():
        binding = binding_for(spec, settings)
        roles.append({
            "role": spec.key,
            "title": spec.title,
            "agents": [{"ref": b.ref, "duty": b.duty.value, "weight": b.weight}
                       for b in binding.agents],
            "aggregation": (binding.aggregation.model_dump(mode="json")
                            if binding.aggregation else None),
            "resolved": [a.model_dump(mode="json") for a in plan.for_role(spec.key)],
        })
    payload = {
        "profiles": [p.model_dump(mode="json") for p in settings.agent_profiles.values()],
        "roles": roles,
    }
    if args.json:
        import json as _json
        print(_json.dumps(payload, ensure_ascii=False))
        return EXIT_CODES["ok"]

    # 导出：生成可直接粘贴进 agentcrew.yaml 的 agents/roles 片段（复用 yaml 依赖）
    if getattr(args, "yaml", False):
        import yaml as _yaml
        export = {
            "agents": {
                "profiles": {
                    key: {k: v for k, v in p.model_dump(mode="json").items() if k != "key" and v not in (None, "", [])}
                    for key, p in settings.agent_profiles.items()
                }
            }
        }
        if settings.role_agents:
            export["roles"] = {
                role: {k: v for k, v in ra.model_dump(mode="json", exclude_none=True).items() if k != "aggregation" or v is not None}
                for role, ra in settings.role_agents.items()
            }
        print(_yaml.safe_dump(export, allow_unicode=True, sort_keys=False))
        return EXIT_CODES["ok"]

    console = Console()
    profiles = payload["profiles"]
    _out(console, f"执行体档案 {len(profiles)} 个：" +
         ("、".join(p["key"] for p in profiles) if profiles else "（未配置）"))
    for item in roles:
        if not item["agents"]:
            continue
        chain = " + ".join(f"{a['ref']}:{a['duty']}" for a in item["agents"])
        mode = (item["aggregation"] or {}).get("mode", "single")
        _out(console, f"  {item['role']:<18} {chain}（汇总：{mode}）")
    return EXIT_CODES["ok"]


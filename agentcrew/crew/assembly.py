"""Crew 装配层：run 启动期的组件构建与知识/经验/图片/工具接线（R66h 拆出）。

这些方法只依赖 Crew 的少量实例属性（下方 TYPE_CHECKING 之外的类级注解声明），
不调用 Crew 的编排方法——是一个自包含闭包，故以 mixin 形式回挂 :class:`Crew`，
方法体与注释逐字搬运，行为不变。纯函数（经验提取/咨询沉淀/URL 脱敏）随迁，
``crew.crew`` 保留兼容再导出。
"""

from __future__ import annotations

import base64
import contextlib
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.context.context_bus import ContextBus
from agentcrew.gate.execute_verify import ExecutionVerifier
from agentcrew.gate.gatekeeper import Gatekeeper, Prompter, Reviewer
from agentcrew.gate.reassign import ReassignPolicy
from agentcrew.gate.supervisor import SupervisorReviewer
from agentcrew.knowledge.entity import EntityMemoryStore, EntityRecord, entities_from_experience
from agentcrew.knowledge.experience import Experience, ExperienceStore
from agentcrew.mcp.adapter import adapt_tools
from agentcrew.mcp.client import MCPClient
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.guidance import GuidanceStore
from agentcrew.runtime.ledger import Ledger
from agentcrew.runtime.webhook import WebhookConfig, WebhookDispatcher, WebhookTarget
from agentcrew.task.task import StatusChange, Task, TaskStatus
from agentcrew.task.task_board import TaskBoard
from agentcrew.tools import (
    GenerateImageTool,
    GrepTool,
    ListFilesTool,
    ReadFileTool,
    RunPythonTool,
    RunTestsTool,
    WebFetchTool,
    WebSearchTool,
    WriteFileTool,
    build_image_provider,
)
from agentcrew.tools.registry import ToolRegistry
from agentcrew.tools.sandbox import ExecutionBackend, build_backend
from agentcrew.tools.search import build_search_provider
from agentcrew.utils.clock import utc_now

if TYPE_CHECKING:
    from agentcrew.crew.crew import _Run

#: 与宿主模块同一日志通道（拆分别改日志路由）。
_logger = logging.getLogger("agentcrew.crew")

class CrewAssembly:
    """装配 mixin：宿主 :class:`agentcrew.crew.crew.Crew` 在 ``__init__`` 里
    赋值这些属性；此处仅声明类型，不产生类级默认值。"""

    _settings: Settings
    _catalog: RoleCatalog
    _prompter: Prompter | None
    _capture_experiences_enabled: bool
    _reuse_knowledge_enabled: bool
    _entity_memory_enabled: bool
    _tool_registry_override: ToolRegistry | None
    _sandbox_backend: ExecutionBackend | None
    _mcp_clients: list[MCPClient]
    _images: list[dict[str, Any]]
    _docs_store: Any | None
    _docs_vector_weight: float

    def _guidance_provider(self, run: _Run) -> Callable[[str], list[str]] | None:
        """C10：把「用户中途指引」接到上下文包组装（派发前吸收，at-most-once）。

        关闭（``guidance_enabled=False``，默认）时返回 ``None``——``ContextBus``
        不持有 provider、``build()`` 连新分支都不进、``render_pack`` 不添段，
        关闭态与改造前逐字节一致。开启时返回闭包：读 pending → 标记消费 →
        记事件 → 返回文本。

        切片二：闭包接收 ``task_id``，只取「未定向 + 定向到本任务」的指引；
        撤回与过期项由存储层在读取时排除，不占配额也不会被注入。过期的 id
        每个 run 实例只补一次 ``guidance_expired`` 事件（读时判定不重写盘）。

        存储绑定 ``run.checkpoint.run_dir``，不依赖内存状态，所以 resume（含
        重新构造 ``Crew`` 的新进程）同样生效；``consumed_at`` 已落盘的指引
        不会被二次注入。
        """
        if not self._settings.guidance_enabled:
            return None
        store = GuidanceStore(
            run.checkpoint.run_dir,
            secrets=tuple(self._settings.secret_values()),
        )
        notified_expired: set[str] = set()

        def _take(task_id: str) -> list[str]:
            expired = store.expired()
            fresh = [item.id for item in expired
                     if item.id not in notified_expired]
            if fresh:
                notified_expired.update(fresh)
                run.ledger.emit(
                    "guidance_expired", ids=fresh, count=len(fresh))
            pending = store.pending(
                limit=self._settings.max_guidance_items,
                max_chars=self._settings.max_guidance_chars,
                task_id=task_id or None,
            )
            if not pending:
                return []
            ids = [item.id for item in pending]
            texts = [item.text for item in pending]
            # at-most-once：只有「这批 id 全部标记成功」才注入。并发下另一个任务
            # 抢先把它们消费掉（返回 0）时本次不注入——宁可少喂一次，也不能
            # 让同一条指引被两个任务各吃一遍。
            consumed = store.consume(ids)
            if consumed != len(ids):
                _logger.warning(
                    "指引消费不完整（%d/%d），本次不注入：%s",
                    consumed, len(ids), ids,
                )
                return []
            run.ledger.emit(
                "guidance_injected",
                ids=ids,
                count=consumed,
                chars=sum(len(text) for text in texts),
                task_id=task_id,
                targets=[item.target for item in pending],
            )
            truncated = [item.id for item in pending if item.truncated]
            if truncated:
                run.ledger.emit(
                    "guidance_truncated",
                    ids=truncated,
                    max_chars=self._settings.max_guidance_chars,
                )
            return texts

        return _take

    def _build_board(self, run: _Run, tasks: Sequence[Task]) -> TaskBoard:
        """建面板并把每次状态跃迁落盘：``tasks.json`` + ``MANIFEST.json``。

        spec §13.2 要求状态跃迁后立即落盘。MANIFEST 也在这里刷新，而不是只在收尾
        写一次：硬杀（Ctrl-C／断电）之后 resume 靠 ``load_manifest`` 重建产物记录，
        只在收尾写的话，被杀那一刻盘上有文件、清单里一条记录都没有 —— 重建恰好
        在最需要它的时候失明，§9.2 的跨任务冲突检测与返工清理跟着一起失效。
        """
        board: TaskBoard | None = None

        def _persist() -> None:
            """A-19：非跃迁写入（评审记录/返工计数/执行结果）的落盘出口。

            只写 tasks.json，不重写 MANIFEST——清单与产物相关，计数变化不改变它，
            每次派发都重建清单会把写放大到不可接受。
            """
            if board is not None:
                run.checkpoint.save_tasks(board)

        def _on_change(_task: Task, _change: StatusChange) -> None:
            if board is not None:
                run.checkpoint.save_tasks(board)
                run.store.write_manifest()

        board = TaskBoard(tasks, on_change=_on_change, on_persist=_persist)
        return board

    def _build_gatekeeper(
        self, run: _Run, board: TaskBoard, bus: ContextBus | None
    ) -> Gatekeeper:
        """每轮按面板重装门禁编排器；``prompter`` **原样透传**（可为 ``None``）。

        Task 27 的守卫在这里必须原样生效：``gate`` 含人工卡点却没给 prompter 时
        ``Gatekeeper`` 构造期抛 ``ValueError``，Crew **不替调用方兜底**。静默塞一个
        ``NullPrompter`` 会把「忘了接线」重新变成「自动放行」——正是那个守卫要拦的
        事，且在离错误更远的一层，更难看见。两条合法出口都是显式声明：``gate="none"``
        （全自动路径，即 ``--auto``）或显式传入 ``NullPrompter()``。
        """
        reviewer = Reviewer(run.models, self._catalog, run.store, run.ledger)
        # R7：主管审核（谁派的活谁验收）。默认关闭；离线 mock 通道没有
        # supervisor 夹具，强行启用只会让每次主管评审都撞 StructuredOutputError
        # 再被当成格式错误放行——不如明确跳过并留一条可追溯的原因。
        supervisor = None
        reassign = None
        if self._settings.supervisor_review_enabled:
            if self._settings.mock:
                run.ledger.emit(
                    "supervisor_review_skipped",
                    run_id=run.record.run_id,
                    reason="mock_channel",
                )
            else:
                supervisor = SupervisorReviewer(
                    reviewer, self._catalog, run.ledger,
                    # R9：scope=all 时规划期任务也由 supervisor_role 验收。
                    scope=self._settings.supervisor_scope,
                    fallback_role=self._settings.supervisor_role,
                    # R33：直接主管之上的逐级复核链（默认空 = 单层）。
                    escalation=self._settings.supervisor_chain,
                )
                # R10：只有配了备选映射才接线——空映射时 pick 恒为 None，
                # 不构造也就少一个对象。
                if self._settings.role_fallback:
                    reassign = ReassignPolicy(
                        self._catalog, self._settings.role_fallback,
                        max_reassignments=(
                            self._settings.max_supervisor_reassignments),
                    )
        # C3：G1 执行校验档——默认关，``gate_config.exec_verify=true`` 才装配。
        # ``mock`` 通道显式跳过并留痕：夹具里的产物是跨任务引用的（T-003 的
        # test_*.py 常常 import T-002 的模块），只铺单个任务的产物必然 import
        # 失败；那不是产物的问题，是夹具的形态。真实验证要跑真实模型端到端。
        exec_verifier = None
        if bool(self._settings.gate_config.get("exec_verify", False)):
            if self._settings.mock:
                run.ledger.emit(
                    "g1_exec_check_skipped",
                    run_id=run.record.run_id,
                    reason="mock_channel",
                )
            else:
                exec_verifier = ExecutionVerifier(
                    backend=self._execution_backend(),
                    # 校验目录与 artifacts/ 平级：都在 run 目录内，随 run 一起
                    # 清理；不写进 artifacts/ 是为了不污染 MANIFEST 与产物清单。
                    workspace_root=run.store.root.parent / "verify",
                    ledger=run.ledger,
                    timeout=float(self._settings.gate_config.get(
                        "exec_verify_timeout_seconds", 120)),
                )
        return Gatekeeper(
            reviewer,
            run.store,
            board,
            gate=self._settings.gate,
            # P2-5：门禁规则必须真正接线。此前 Crew 装配时漏传，导致
            # gate_config 里的 g1/g2 开关、评审人数、收敛阈值在真实 run 中
            # 全部回落硬编码默认值（配置形同虚设）。
            gate_config=self._settings.gate_config,
            ledger=run.ledger,
            prompter=self._prompter,
            context_bus=bus,
            review_score_threshold=self._settings.review_score_threshold,
            supervisor=supervisor,
            reassign=reassign,
            exec_verifier=exec_verifier,
        )

    def _build_webhook(
        self, ledger: Ledger, *, spool_dir: Path | None = None,
        dedup_dir: Path | None = None,
    ) -> WebhookDispatcher | None:
        """按配置构造分发器；URL 为空时返回 ``None``（默认关闭）。

        启用时记一条 ``webhook_enabled``，但**只记打码后的 URL**：查询串里可能
        带 token，整条 URL 写进 ledger 就等于泄露。``spool_dir`` 由 ``_start``
        传入（R15）；库层直接调用不传时零落盘，行为与 R14 一致。
        """
        # 只看旧字段是不够的：只配 ``webhooks``（多接收端）时 ``webhook_url`` 是空的，
        # 早退会让多接收端完全失效——实测踩过一次。
        if not self._settings.webhook_url and not self._settings.webhooks:
            return None
        config = WebhookConfig(
            url=self._settings.webhook_url,
            secret=self._settings.webhook_secret,
            events=tuple(self._settings.webhook_events),
            timeout_seconds=self._settings.webhook_timeout_seconds,
            max_retries=self._settings.webhook_max_retries,
            # C4：单接收端的事件级过滤（多接收端在每条 target 里各配 filter）。
            filter=self._settings.webhook_filter,
            # R14：多接收端。``webhooks`` 非空时 ``WebhookConfig.resolved()`` 以它
            # 为准，旧的单接收端字段自动让位（避免同一地址被推两遍）。
            targets=tuple(
                WebhookTarget(
                    name=str(raw.get("name") or ""),
                    url=str(raw.get("url") or ""),
                    secret=str(raw.get("secret") or ""),
                    events=tuple(
                        str(item) for item in (raw.get("events") or [])),
                    only_failures=bool(raw.get("only_failures", False)),
                    filter=str(raw.get("filter") or ""),
                )
                for raw in self._settings.webhooks
                if str(raw.get("url") or "").strip()
            ),
        )
        dispatcher = WebhookDispatcher(
            config, sink=self._webhook_sink(ledger),
            spool_dir=spool_dir,
            dedup_dir=dedup_dir,
            # R15：payload 落盘前按同一套密钥表脱敏，不绕过 Ledger 的防护。
            secrets=tuple(self._settings.secret_values()),
        )
        ledger.emit(
            "webhook_enabled",
            # 逐接收端记录，URL 一律打码：查询串里可能有 token。
            targets=[
                {
                    "name": target.name,
                    "url": _redact_url(target.url),
                    "events": list(target.events),
                    "signed": bool(target.secret),
                    "only_failures": target.only_failures,
                    "filter": target.filter,
                }
                for target in config.resolved()
            ],
            spool_enabled=spool_dir is not None,
            dedup_enabled=dedup_dir is not None,
        )
        return dispatcher

    async def _attach_mcp_tools(
        self, registry: ToolRegistry | None, ledger: Ledger
    ) -> None:
        """R13：连接 ``mcp_servers`` 里的每个服务器，把工具登记进注册表。

        只在 ``tools_enabled``（``registry`` 非空）时装配——MCP 工具也是工具，没有
        注册表就无处安放；这也保证了默认配置下**零子进程、零网络**。

        单个服务器失败（命令不存在、握手超时、配置形状不对）只记一条 ``mcp_failed``
        并跳过，不影响其它服务器与本 run：MCP 是外挂能力，一个外挂坏了不该让整支
        团队停摆。``CancelledError`` 不在此列（它是 ``BaseException``），取消照常穿透。
        """
        if registry is None or not self._settings.mcp_servers:
            return
        # G-1：不可信输入预设下不装配任何 MCP 工具——外部子进程谈不上只读，
        # 挂上去等于在「只读模式」里开一个后门。
        if self._settings.untrusted_inputs:
            ledger.emit(
                "mcp_skipped_untrusted",
                servers=sorted(self._settings.mcp_servers),
            )
            return
        for server, raw in self._settings.mcp_servers.items():
            if not isinstance(raw, Mapping):
                ledger.emit(
                    "mcp_failed", server=str(server),
                    error="mcp_servers 的每个条目都必须是映射")
                continue
            client: MCPClient | None = None
            try:
                client = MCPClient.from_config(dict(raw))
                await client.start()
                specs = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 —— 第三方进程什么都可能抛
                if client is not None:
                    # 关闭失败不掩盖原始错误。
                    with contextlib.suppress(Exception):
                        await client.stop()
                ledger.emit(
                    "mcp_failed", server=str(server),
                    error=f"{type(exc).__name__}: {exc}"[:300])
                continue
            tools = adapt_tools(str(server), client, specs)
            for tool in tools:
                registry.register(tool)
            self._mcp_clients.append(client)
            ledger.emit(
                "mcp_connected",
                server=str(server),
                tools=[tool.name for tool in tools],
            )

    def _webhook_sink(
        self, ledger: Ledger
    ) -> Callable[[str, dict[str, Any]], None]:
        """投递结果的落账口；ledger 已关时静默丢弃。

        收尾顺序里 ledger 可能先关（``_finalize`` 尾部），而队列里还剩几条结果。
        那些结果记不上就记不上——绝不能因为「一条通知的日志」在收尾阶段抛异常。
        """
        def sink(name: str, data: dict[str, Any]) -> None:
            # RuntimeError：ledger 已关（收尾顺序差异）。
            # TypeError：data 的键与 emit 的位置参数撞名——实测过一次
            # （`event`），这类错误绝不该让 run 挂掉，所以一并兜住。
            with contextlib.suppress(RuntimeError, TypeError):
                ledger.emit(name, **data)

        return sink

    async def _ingest_docs(self, ledger: Ledger) -> None:
        """C5：摄取团队文档（``docs_rag_enabled`` 开启时）。

        文档在 ``<output_dir>/knowledge/docs/``，由用户自己放置；摄取失败只记
        warning——文档是上下文增强，不能让它把 run 拖死。留痕事件让「这次 run
        有多少文档参与检索」能从审计读出来。
        """
        if not self._settings.docs_rag_enabled:
            return
        from agentcrew.knowledge.docs import DocStore
        from agentcrew.knowledge.embeddings import build_embedder
        from agentcrew.models.embedding import embedding_target

        try:
            embedder = build_embedder(
                self._settings,
                output_dir=self._settings.output_dir,
                target=embedding_target(self._settings),
            )
            store = DocStore(
                self._settings.output_dir / "knowledge" / "docs",
                embedder=embedder,
            )
            result = await store.ingest()
        except Exception as exc:  # noqa: BLE001 —— 检索增强失败不拖死 run
            _logger.warning("文档摄取失败: %s", exc)
            ledger.emit("docs_ingest_failed",
                        error=f"{type(exc).__name__}: {exc}"[:200])
            return
        self._docs_store = store
        self._docs_vector_weight = self._docs_effective_weight(embedder, ledger)
        ledger.emit("docs_ingested", docs=result.docs, chunks=result.chunks,
                    rebuilt=result.rebuilt, skipped=result.skipped)
        # C6：真语义通道只在**异步**路径生效（规划期 / 经验库 / 实体记忆）；
        # 执行期的 ContextBus 回调是同步的，DocStore.query_sync 明确不算语义
        # 向量。与其让用户以为全链路都用上了，不如把这条限制写进审计。
        from agentcrew.knowledge.embeddings import is_semantic_embedder

        if is_semantic_embedder(embedder):
            ledger.emit(
                "docs_sync_path_bm25_only",
                reason="context_bus_retriever_is_sync",
                effective_weight=self._docs_vector_weight,
            )

    def _docs_effective_weight(self, embedder: Any, ledger: Ledger) -> float:
        """C5：这次 run 文档检索**实际**用的向量权重。

        配置默认 0.5 是按「有真实语义向量」写的。只有本地哈希兜底时，0.5 会把
        命中率拉低（486 条查询实测 hit@1 84.4% → 82.9%，权重 1.0 更是 53.3%），
        因此这里压到 0（纯 BM25），并留一条事件——降级可见，不静默。
        """
        weight = float(self._settings.docs_rag_vector_weight)
        from agentcrew.knowledge.embeddings import is_semantic_embedder

        if weight > 0 and not is_semantic_embedder(embedder):
            ledger.emit("docs_vector_degraded",
                        configured_weight=weight, effective_weight=0.0,
                        reason="hash_embedder_is_not_semantic")
            return 0.0
        return weight

    async def _docs_query_async(self, query: str) -> list[Any]:
        """规划期文档检索：用**生效**权重，而不是配置原值。

        此前这里直接读 ``settings.docs_rag_vector_weight``，绕过了
        ``_docs_effective_weight`` 的降级——哈希兜底时规划期仍按 0.5 混入非语义
        向量，与 ``docs_vector_degraded`` 的承诺相反（B5 实测 0.5 反而掉命中率）。
        抽成方法而不是内联，是为了让「兜底时权重必须是 0」可被单测直接钉住。
        """
        if self._docs_store is None or self._settings.docs_rag_limit <= 0:
            return []
        return await self._docs_store.query(
            query, limit=self._settings.docs_rag_limit,
            vector_weight=self._docs_vector_weight)

    def _persist_images(self, run_dir: Path, ledger: Ledger) -> None:
        """C4：把附带图片写成 ``<run_dir>/context/images/NN.<ext>``（输入证据）。

        每个文件独立落盘、失败只记 warning 不炸 run——图片是输入增强，不是
        交付路径上的必需品；写盘失败时模型看不到图，``vision_unsupported``
        的降级语义仍然成立。
        """
        if not self._images:
            return
        image_dir = run_dir / "context" / "images"
        extensions = {"image/png": "png", "image/jpeg": "jpg",
                      "image/webp": "webp", "image/gif": "gif"}
        saved: list[str] = []
        total_bytes = 0
        for index, image in enumerate(self._images):
            extension = extensions.get(str(image.get("mime", "")), "png")
            try:
                image_dir.mkdir(parents=True, exist_ok=True)
                path = image_dir / f"{index:02d}.{extension}"
                path.write_bytes(base64.b64decode(image.get("data", "")))
                saved.append(path.name)
                total_bytes += path.stat().st_size
            except (OSError, ValueError) as exc:
                _logger.warning("图片落盘失败 #%s: %s", index, exc)
        if saved:
            ledger.emit(
                "images_attached",
                count=len(saved),
                total_bytes=total_bytes,
                files=saved,
            )

    def _execution_backend(self) -> ExecutionBackend:
        """C2：按配置构造执行后端（local 默认；docker 可选，缺失即报错）。

        惰性构造且只构造一次：``build_backend`` 在 docker 模式下会探测 ``docker``
        可执行文件，gatekeeper 每轮重建、工具注册表每次 run 重建，不该反复探测。
        ``SandboxError`` 原样冒泡——「选了 docker 却没有 docker」是运维配置错误，
        必须让 run 失败并说清原因，不能静默退回不隔离的 local。
        """
        if self._sandbox_backend is None:
            settings = self._settings
            self._sandbox_backend = build_backend(
                settings.sandbox_backend,
                image=settings.sandbox_image,
                memory_mb=settings.sandbox_memory_mb,
                cpus=settings.sandbox_cpus,
                allow_network=settings.sandbox_allow_network,
            )
        return self._sandbox_backend

    def _build_tool_registry(
        self, checkpoint: CheckpointStore, ledger: Ledger
    ) -> ToolRegistry | None:
        """R3：装配 run 级工具注册表；默认关闭时返回 ``None``（行为逐字不变）。

        这是把「库能力」接进生产 run 的**唯一装配点**：此前 ``ToolRegistry`` 只
        在单测里被实例化，``tool_permissions`` / ``mcp_servers`` 因此是死配置，
        Agent 永远走单次调用。现在 ``settings.tools_enabled`` 开启时，为本次 run
        登记内置工具（默认 5 个；``code_exec_enabled`` 打开时含 ``run_python``，
        见 P1 的默认关闭决定）并把 ``tool_permissions`` 下发成执行期白名单。

        边界：读工具以 ``run_dir`` 为根（能看上游 ``artifacts/`` 与 ``context/``），
        写工具只以 ``run_dir/workspace`` 为根（中间草稿与交付产物物理隔离，不进
        ``MANIFEST.json``）；``mcp_servers`` 需要异步连接与生命周期管理，仍走库层
        显式装配，不在这里半接线（见 ``PROJECT-STATE.md`` §7）。
        """
        override = self._tool_registry_override
        if override is not None:
            override.bind_ledger(ledger)
            return override
        if not self._settings.tools_enabled:
            return None

        run_dir = checkpoint.run_dir
        workspace = run_dir / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        registry = ToolRegistry(ledger=ledger)
        tools = [
            ReadFileTool(workdir=run_dir),
            ListFilesTool(workdir=run_dir),
            GrepTool(workdir=run_dir),
        ]
        # G-1 不可信输入预设：只给只读工具。写工具（write_file / run_python /
        # web_fetch）一律不下发，code_exec_enabled 在这里也不管用——处理不可信
        # 第三方文档时，「能读不能写」是提示词注入面前唯一可靠的边界。
        if not self._settings.untrusted_inputs:
            # P1：``run_python`` 默认不进注册表。它有进程内隔离（``-I`` / 裁剪
            # 环境 / cwd 限制 / 超时杀进程），但没有网络与系统调用沙箱——默认
            # 打开等于把「配置写错」升级成「远程执行面」。需要时用
            # AGENTCREW_CODE_EXEC_ENABLED 显式打开，边界写在 docs/OPERATIONS.md。
            if self._settings.code_exec_enabled:
                # C2：同一份后端给 run_python 与 run_tests——「换后端」只改配置，
                # 不改工具；``local`` 时行为与引入前逐字一致。
                backend = self._execution_backend()
                tools.append(RunPythonTool(workdir=run_dir, backend=backend))
                # C3：执行验证——写完代码能当场跑，而不是等门禁判决。
                tools.append(RunTestsTool(workdir=run_dir, backend=backend))
            # C1：``web_search`` 只在配置了提供方时注册。没配就是没有这个工具，
            # 而不是「有但一调就报错」——后者会让模型在提示词里被承诺一个假能力。
            if self._settings.search_provider.strip():
                tools.append(WebSearchTool(
                    provider=build_search_provider(self._settings.search_provider),
                    max_results=self._settings.search_max_results,
                    timeout_secs=self._settings.search_timeout_seconds,
                ))
            # R61：``generate_image`` 只在配置了提供方时注册（默认关闭，同 C1）。
            # 图片字节写进工作区，再经二进制产物通道成为受契约保护的交付物。
            if self._settings.image_provider.strip():
                tools.append(GenerateImageTool(
                    provider=build_image_provider(self._settings.image_provider),
                    workdir=workspace,
                    timeout_secs=self._settings.image_timeout_seconds,
                    ledger=ledger,
                ))
            tools.extend((WriteFileTool(workdir=workspace), WebFetchTool()))
        for tool in tools:
            registry.register(tool)
        permissioned: list[str] = []
        for role, allowed in self._settings.tool_permissions.items():
            if isinstance(allowed, (list, tuple)):
                registry.set_role_permissions(
                    str(role), [str(name) for name in allowed]
                )
                permissioned.append(str(role))
        ledger.emit(
            "tool_registry_ready",
            run_id=run_dir.name,
            tools=[tool.name for tool in registry.list_tools()],
            workspace=workspace.name,
            permissioned_roles=sorted(permissioned),
            untrusted_inputs=bool(self._settings.untrusted_inputs),
            # C2：让审计能回答「这次的代码在哪跑的」——local 与 docker 的责任边界
            # 完全不同，只记工具名不记后端等于没记。
            sandbox_backend=(
                self._settings.sandbox_backend
                if self._settings.code_exec_enabled else ""),
        )
        return registry

    async def _prepare_knowledge_context(self, run: _Run) -> str:
        """P3-7 + R3 + R27：规划前检索历史经验与实体记忆，拼成 PM 的 ``context_hint``。

        两块各自有独立开关与独立留痕。``entity_memory_enabled`` 默认关闭，此时
        返回值与改造前**逐字一致**（有经验命中就是经验块本身，无命中就是空串）——
        不会多一个换行、也不会多发一个事件。

        R27：``embedding_enabled`` 时把经验库与查询词一起算成向量，走混合打分；
        embedding 端点失败**不炸 run**——降级回纯 BM25 并留一条
        ``embedding_fallback`` 事件（失败要可见，但不能让检索把整场 run 拖死）。
        """
        blocks: list[str] = []
        if self._reuse_knowledge_enabled:
            store = ExperienceStore(self._settings.output_dir)
            vectors = await self._retrieval_vectors(run, store)
            matched = store.query(run.goal, limit=3,
                                  doc_vectors=vectors[0], query_vector=vectors[1],
                                  vector_weight=self._settings.retrieval_vector_weight)
            if matched:
                run.ledger.emit(
                    "knowledge_reused",
                    goal=run.goal,
                    matched_experiences=len(matched),
                )
                blocks.append(store.inject_context(
                    run.goal, limit=3,
                    doc_vectors=vectors[0], query_vector=vectors[1],
                    vector_weight=self._settings.retrieval_vector_weight))
        if self._entity_memory_enabled:
            entities = EntityMemoryStore(self._settings.output_dir)
            entity_vectors = await self._retrieval_vectors(
                run, entities, kind="entity")
            matched_entities = entities.query(
                run.goal, limit=8, doc_vectors=entity_vectors[0],
                query_vector=entity_vectors[1],
                vector_weight=self._settings.retrieval_vector_weight)
            if matched_entities:
                run.ledger.emit(
                    "entity_memory_reused",
                    goal=run.goal,
                    matched_entities=len(matched_entities),
                )
                blocks.append(entities.inject_context(
                    run.goal, limit=8, doc_vectors=entity_vectors[0],
                    query_vector=entity_vectors[1],
                    vector_weight=self._settings.retrieval_vector_weight))
        # C5：团队文档——PM 拆解也要看到手册与规范（默认关，零开销）。
        if self._docs_store is not None and self._settings.docs_rag_limit > 0:
            hits = await self._docs_query_async(run.goal)
            if hits:
                run.ledger.emit(
                    "docs_context_used",
                    target="planning",
                    hits=len(hits),
                )
                lines = ["## 团队文档（knowledge/docs/ 检索命中）"]
                lines.extend(
                    f"### {hit.chunk.doc} · {hit.chunk.heading}\n{hit.chunk.text}"
                    for hit in hits)
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

    async def _retrieval_vectors(
        self, run: _Run, store: object, *, kind: str = "experience"
    ) -> tuple[list[list[float]] | None, list[float] | None]:
        """R27：为经验库与当前目标准备向量；不可用返回 ``(None, None)``。

        端点失败 / 未配置都走降级：检索退回纯 BM25，并留一条
        ``embedding_fallback`` 事件说明原因——静默降级会让人以为用了向量。
        """
        if not self._settings.embedding_enabled:
            return None, None
        from agentcrew.knowledge.embeddings import EmbeddingError, build_embedder
        from agentcrew.models.embedding import embedding_target

        embedder = build_embedder(
            self._settings, output_dir=self._settings.output_dir,
            target=embedding_target(self._settings))
        if embedder is None:
            return None, None
        try:
            documents = store.all()          # type: ignore[attr-defined]
            if not documents:
                return None, None
            doc_vectors = await embedder.embed(
                [store.haystack(item) for item in documents])  # type: ignore[attr-defined]
            query_vectors = await embedder.embed([run.goal])
        except EmbeddingError as exc:
            run.ledger.emit("embedding_fallback",
                            reason=str(exc), embedder=embedder.name, kind=kind)
            return None, None
        run.ledger.emit("embedding_used", embedder=embedder.name, kind=kind,
                        documents=len(doc_vectors),
                        dim=len(doc_vectors[0]) if doc_vectors else 0,
                        vector_weight=self._settings.retrieval_vector_weight)
        return doc_vectors, query_vectors[0]

    def _record_consult_experience(
        self,
        run: _Run,
        from_role: str,
        to_role: str,
        question: str,
        answer: str,
    ) -> None:
        """R6：咨询问答 → 一条 ``Experience``。

        字段映射刻意「可检索优先」：``task_description`` 写「谁问谁问了什么」，
        这样下次同类问题走 :meth:`ExperienceStore.query` 的关键词匹配能命中；
        ``solution_summary`` 存答复本身。``artifacts`` / ``key_decisions`` 留空
        ——咨询不产物、不做决策，填进去会让经验库的语义变浑。
        """
        experience = _consult_experience(
            from_role, to_role, question, answer,
            run_id=run.record.run_id,
        )
        if experience is None:
            # 占位文案不配进经验库：它描述的是「模型没答」，不是一条知识。
            return
        ExperienceStore(self._settings.output_dir).add(experience)
        run.ledger.emit(
            "consult_experience_captured",
            from_role=from_role,
            to_role=to_role,
            experience_id=experience.id,
        )

    def _capture_experiences(
        self, run: _Run, board: TaskBoard, bus: ContextBus
    ) -> None:
        """P3-5：run 成功（COMPLETED）后把成功任务沉淀进全局经验库。

        只沉淀 ``SUCCEEDED`` 任务——失败 / 取消 / 被跳过的任务不进经验库（它们的
        「解决方案」不成立）。每条经验落盘后记一条 ``experience_captured`` 事件
        （含 task_id / experience_id），与 Ledger 的「诚实留痕」一致。开关关闭时
        （``capture_experiences=False``）整个方法零副作用。
        """
        if not self._capture_experiences_enabled:
            return
        extracted = _extract_experiences(
            board, bus, store=run.store, run_id=run.record.run_id
        )
        if not extracted:
            return
        knowledge = ExperienceStore(self._settings.output_dir)
        for task_id, exp in extracted:
            knowledge.add(exp)
            run.ledger.emit(
                "experience_captured",
                task_id=task_id,
                experience_id=exp.id,
                role=exp.role,
            )
        if self._entity_memory_enabled:
            self._capture_entities(run, extracted)

    def _capture_entities(
        self, run: _Run, extracted: list[tuple[str, Experience]]
    ) -> None:
        """R3：把这次沉淀的经验里的命名事实追加进全局实体库。

        实体来自同一批成功经验（因此依赖 ``capture_experiences`` 开启；两者任一
        关闭都不会写盘）。追加而非覆写，读取时按名字归并，与经验库同一口径。
        """
        records: list[EntityRecord] = []
        for _, exp in extracted:
            records.extend(
                entities_from_experience(exp, run_id=run.record.run_id)
            )
        if not records:
            return
        EntityMemoryStore(self._settings.output_dir).append(records)
        run.ledger.emit(
            "entity_memory_updated",
            run_id=run.record.run_id,
            entities=len(records),
            distinct=len({record.name.casefold() for record in records}),
        )


# ── 模块级辅助（可单测的纯函数，随装配层迁居）─────────────────────

#: R6：凡是以这些文案开头的「答复」都不是知识，是降级说明——不进经验库。
_PLACEHOLDER_ANSWERS = (
    "（离线演示",
    "（对方未能作答",
    "（对方未给出有效答复",
    "（该角色暂不支持咨询",
)


def _consult_experience(
    from_role: str,
    to_role: str,
    question: str,
    answer: str,
    *,
    run_id: str,
) -> Experience | None:
    """把一次咨询的问答对转成 ``Experience``；占位答复返回 ``None``（R6 纯逻辑）。

    独立成模块级函数是为了可单测：``Crew`` 那层只负责「有值就落盘 + 记事件」。
    """
    text = (answer or "").strip()
    if not text or text.startswith(_PLACEHOLDER_ANSWERS):
        return None
    return Experience(
        id=uuid4().hex,
        task_description=f"{from_role} 向 {to_role} 提问：{(question or '').strip()[:200]}",
        role=to_role,
        solution_summary=text[:400],
        artifacts=[],
        key_decisions=[],
        success=True,
        run_id=run_id,
        created_at=utc_now(),
    )


def _extract_experiences(
    board: TaskBoard,
    bus: ContextBus,
    *,
    store: ArtifactStore,
    run_id: str,
) -> list[tuple[str, Experience]]:
    """从面板里提取成功任务的经验（P3-5 的纯逻辑，可单测）。

    只收 ``SUCCEEDED`` 任务；``solution_summary`` 取 Agent 自述 summary，缺则回落
    notes 前 200 字；``artifacts`` 取该任务实际落盘的产物路径；``key_decisions``
    取 ContextBus 里 task_id 命中的决策文本。返回 ``(task_id, Experience)`` 对——
    task_id 不在 :class:`Experience` 的字段里（它描述的是「任务」而非 run 内定位），
    但 ledger 事件要它，故成对返回。
    """
    decisions_by_task: dict[str, list[str]] = {}
    for record in bus.decisions():
        decisions_by_task.setdefault(record.task_id, []).append(record.decision)
    extracted: list[tuple[str, Experience]] = []
    for task in board.all():
        if task.status is not TaskStatus.SUCCEEDED:
            continue
        result = task.result
        summary = (result.summary or result.notes)[:200] if result is not None else ""
        artifacts = sorted(record.path for record in store.by_task(task.id))
        exp = Experience(
            id=uuid4().hex,
            task_description=task.description,
            role=task.role,
            solution_summary=summary,
            artifacts=artifacts,
            key_decisions=decisions_by_task.get(task.id, []),
            success=True,
            run_id=run_id,
            created_at=utc_now(),
        )
        extracted.append((task.id, exp))
    return extracted


def _redact_url(url: str) -> str:
    """只保留 ``scheme://host/path``，丢掉查询串与片段。

    查询串里经常带 ``?token=…``：把整条 URL 写进 ledger 等于把凭证抄进审计文件。
    启用 Webhook 时记的就是这个函数的结果。
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"

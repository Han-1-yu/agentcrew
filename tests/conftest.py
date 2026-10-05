"""跨文件共享的测试夹具（Task 33 的离线端到端套件）。

``test_end_to_end_mock.py`` 与 ``test_mock_no_bypass.py`` 共用 ``offline_run``：
**模块内只跑一次**完整 mock run，该模块的全部断言读同一份结局、同一批调用计数
与同一批 ``ArtifactStore`` 记录。单次 run 约 0.9 秒（ledger 每写一行 fsync 一次），
按用例各跑一次会给套件加上数秒，因此按模块共享；两个文件各自触发一次实例。

本文件只在模块内没有同名定义时才生效：``test_model_factory.py`` 的 ``settings``
夹具定义在模块层，优先于这里，行为不受影响。
"""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentcrew.artifacts.store import ArtifactStore
from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.runtime.checkpoint import RunRecord
from agentcrew.runtime.ledger import Ledger

#: 仓库内的演示夹具根目录（``examples/fixtures``，spec §16.1）。
DEMO_FIXTURES_DIR: Path = (
    Path(__file__).resolve().parents[1] / "examples" / "fixtures")

#: 演示目标（与 ``examples/todo_web_project.py`` 同一句话）。
DEMO_GOAL = "开发一个 Todo List Web 应用：用户可在浏览器中增删改查待办事项。"

#: spec §17.2 列出的全部 API Key 环境变量；「无密钥可跑」的断言要逐个删干净。
API_KEY_ENVS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY",
    "ARK_API_KEY",
    "QIANFAN_API_KEY",
    "DEEPSEEK_API_KEY",
)


@dataclass(frozen=True)
class OfflineRun:
    """一次已完成的离线 mock run，以及断言它所需的全部材料。"""

    settings: Settings
    record: RunRecord
    #: run 目录（``output/<run_id>/``）。
    run_dir: Path
    #: 产物根目录（``run_dir/artifacts``）。
    artifacts: Path
    #: 已关闭但可读的 ledger（读取不受 ``close()`` 影响）。
    ledger: Ledger
    #: 五个调用计数器：``commit`` / ``resolve`` / ``static`` / ``parse`` /
    #: ``review``（G2 LLM 评审层）。
    counters: dict[str, int]
    #: run 期间 ``ArtifactStore.commit`` 的接收者，按实例去重（无旁路断言的权威来源）。
    stores: tuple[ArtifactStore, ...]
    #: run 启动那一刻各 API Key 环境变量的值（无密钥断言在此取证）。
    keys_at_run_time: dict[str, str | None]


@pytest.fixture(scope="module")
def offline_run(tmp_path_factory) -> OfflineRun:
    """跑一次完整的离线 mock run，并挂上四个调用计数器。

    与 brief 样例的三处刻意差异，每一处都有理由：

    1. **模块级共享一次 run**：单次约 0.9 秒，逐用例重跑会把套件拖慢数秒（§5）。
    2. **全程删除全部 API Key 且停用 dotenv**：本文件与无旁路文件的每条断言都
       因此顺带证明「无密钥可跑」；``keys_at_run_time`` 把那一刻的环境原样留证。
       停用 ``load_dotenv`` 是必要的：否则开发者 ``.env`` 里的真 key 会在
       ``Settings.load()`` 里被重新读回，断言取证的就成了假象。
    3. **五个计数器在这里统一挂钩**（只计数、不改行为）：无旁路断言与结局断言
       读同一个 run，避免为「证明走了通路」再跑一遍。其中 ``review`` 数的是
       G2 评审层：它是唯一一个「不跑也能让整条 run 变绿」的门禁层。

    ``run_static`` 必须**同时**替换两个绑定：``gatekeeper`` 用
    ``from agentcrew.gate.static_checks import run_static`` 把函数绑进了自己的
    模块全局，只改源模块属性时（收集期已全部导入）调用方仍走原函数，计数恒为 0。

    全部夹具都在 ``tmp_path_factory`` 的临时目录里，不读仓库的 ``output/``，
    也不读仓库根的 ``agentcrew.yaml``（``AGENTCREW_CONFIG`` 指向不存在的路径）。
    """
    monkeypatch = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("offline-run")
    monkeypatch.setenv("AGENTCREW_MOCK", "true")
    monkeypatch.setenv("AGENTCREW_GATE", "none")
    monkeypatch.setenv("AGENTCREW_OUTPUT_DIR", str(tmp / "o"))
    monkeypatch.setenv("AGENTCREW_CONFIG", str(tmp / "absent-agentcrew.yaml"))
    for name in API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("agentcrew.config.load_dotenv", lambda *a, **kw: False)
    monkeypatch.setattr(
        "agentcrew.models.factory.DEFAULT_FIXTURES_DIR", DEMO_FIXTURES_DIR)

    counters = {"commit": 0, "resolve": 0, "static": 0, "parse": 0, "review": 0}
    stores: dict[int, ArtifactStore] = {}

    original_commit = ArtifactStore.commit

    def spy_commit(self, rel, content, **kw):
        counters["commit"] += 1
        stores[id(self)] = self
        return original_commit(self, rel, content, **kw)

    original_resolve = ArtifactStore.resolve

    def spy_resolve(self, rel):
        counters["resolve"] += 1
        return original_resolve(self, rel)

    monkeypatch.setattr(ArtifactStore, "commit", spy_commit)
    monkeypatch.setattr(ArtifactStore, "resolve", spy_resolve)

    import agentcrew.gate.gatekeeper as gatekeeper
    import agentcrew.gate.static_checks as static_checks

    original_static = static_checks.run_static

    def spy_static(*args, **kw):
        counters["static"] += 1
        return original_static(*args, **kw)

    monkeypatch.setattr(static_checks, "run_static", spy_static)
    monkeypatch.setattr(gatekeeper, "run_static", spy_static)

    import agentcrew.agents.base_agent as base_agent
    import agentcrew.artifacts.parser as parser

    original_parse = parser.parse_fenced_blocks

    def spy_parse(raw):
        counters["parse"] += 1
        return original_parse(raw)

    monkeypatch.setattr(parser, "parse_fenced_blocks", spy_parse)
    monkeypatch.setattr(base_agent, "parse_fenced_blocks", spy_parse)

    # G2（LLM 评审）层单独计数：G1 就能让 8/9 个任务通过，一个「悄悄不再调用
    # 评审器」的实现照样能跑绿整条 run —— 只有直接数 Reviewer.review 才拦得住。
    import agentcrew.gate.reviewer as reviewer_module

    original_review = reviewer_module.Reviewer.review

    async def spy_review(self, task, **kw):
        counters["review"] += 1
        return await original_review(self, task, **kw)

    monkeypatch.setattr(reviewer_module.Reviewer, "review", spy_review)

    try:
        settings = Settings.load()
        keys_at_run_time = {name: os.environ.get(name) for name in API_KEY_ENVS}
        record = asyncio.run(Crew(settings).run(DEMO_GOAL))
        run_dir = settings.output_dir / record.run_id
        yield OfflineRun(
            settings=settings,
            record=record,
            run_dir=run_dir,
            artifacts=run_dir / "artifacts",
            ledger=Ledger(
                run_dir / "ledger.jsonl",
                run_id=record.run_id,
                secrets=settings.secret_values(),
            ),
            counters=counters,
            stores=tuple(stores.values()),
            keys_at_run_time=keys_at_run_time,
        )
    finally:
        monkeypatch.undo()

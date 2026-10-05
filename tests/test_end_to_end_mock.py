"""Task 33 · 端到端：整条流水线在离线夹具上跑通（spec §16.1）。

断言的是**结局**而不是内部实现：run 状态与统计、`MANIFEST.json` 与磁盘的一致
性、生成的 Python/JSON/YAML 真的可解析、ledger 里的事件序列完整，以及 T-005
的「驳回 → 返工 → 通过」闭环真的发生过。

全部用例读 ``offline_run``（conftest，模块级共享）里那一次 run。该 run 在
**删光全部 API Key 且不加载 ``.env``** 的前提下执行，因此本文件的每条断言都
顺带证明了「无密钥可跑」——单次 run 约 0.9 秒，逐用例重跑会把套件拖慢数秒。
"""

import ast
import json

import yaml

from agentcrew.runtime.checkpoint import RunStatus

#: 计划夹具里的任务数（``examples/fixtures/plan_todo.json``）。
PLANNED_TASKS = 8


def test_full_run_completes(offline_run):
    rec = offline_run.record
    assert rec.status is RunStatus.COMPLETED
    assert rec.stats.failed == 0 and rec.stats.blocked == 0
    assert rec.stats.total_tasks == PLANNED_TASKS + 1  # 8 个计划任务 + T-FINAL
    assert rec.stats.succeeded == PLANNED_TASKS + 1


def test_artifacts_are_real_files(offline_run):
    """清单声明的每个产物都必须真的在盘上，且字节数对得上。"""
    art = offline_run.artifacts
    manifest = json.loads((art / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"]
    for entry in manifest["entries"]:
        p = art / entry["path"]
        assert p.is_file(), f"清单声明了 {entry['path']} 但文件不存在"
        assert p.stat().st_size == entry["size_bytes"]


def test_all_generated_python_parses(offline_run):
    parsed = 0
    for p in offline_run.artifacts.rglob("*.py"):
        ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        parsed += 1
    assert parsed > 0, "一个 .py 产物都没有，上面的循环是空转，不能算通过"


def test_json_and_yaml_artifacts_parse(offline_run):
    json_files = 0
    for p in offline_run.artifacts.rglob("*.json"):
        json.loads(p.read_text(encoding="utf-8"))
        json_files += 1
    yaml_files = 0
    for p in offline_run.artifacts.rglob("*.yaml"):
        yaml.safe_load(p.read_text(encoding="utf-8"))
        yaml_files += 1
    assert json_files > 0 and yaml_files > 0, (
        f"解析覆盖为真空：json={json_files}, yaml={yaml_files}"
    )


def test_ledger_records_llm_calls_and_transitions(offline_run):
    """ledger 必须完整记下四类事件，且 ``run_finished`` 恰好一条。"""
    lg = offline_run.ledger
    assert lg.count("llm_call") > 0
    assert lg.count("task_transition") > 0
    assert lg.count("artifact_written") > 0
    assert lg.count("run_finished") == 1


def test_rework_demo_happened(offline_run):
    """T-005 的 attempt-1 不合格，必须出现一次返工并恢复。"""
    lg = offline_run.ledger
    assert lg.count("gate_result") > PLANNED_TASKS + 1  # 至少有一次额外评审

    t005 = [e for e in lg.events("gate_result") if e.data.get("task_id") == "T-005"]
    rejected = [e for e in t005 if e.data.get("verdict") == "reject"]
    assert rejected, "T-005 从未被打回，返工闭环没有发生"
    assert any(
        e.data.get("layer") == "static" and e.data.get("round") == 1
        for e in rejected
    ), "T-005 的第一次驳回必须来自 G1 静态门禁（第 1 轮）"

    recovered = [
        e
        for e in t005
        if e.data.get("verdict") == "approve" and e.data.get("round", 0) >= 2
    ]
    assert recovered, "T-005 被打回后没有在返工轮通过"


def test_no_api_keys_required(offline_run):
    """没有任何 API Key 也必须能跑完整条流水线（spec §16.1）。"""
    missing = {
        name: value
        for name, value in offline_run.keys_at_run_time.items()
        if value is not None
    }
    assert not missing, f"run 启动时这些 Key 还在环境里：{sorted(missing)}"
    assert offline_run.settings.mock is True
    assert offline_run.settings.api_key_for("openai") is None
    assert offline_run.record.status is RunStatus.COMPLETED


def test_delivery_md_mentions_completion(offline_run):
    text = (offline_run.artifacts / "DELIVERY.md").read_text(encoding="utf-8")
    assert "完成" in text

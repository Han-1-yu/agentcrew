"""S9：mock 必须走与真实模型完全相同的数据通路，任何一步都不能被跳过。

两个断言层次：

1. **被调用过**：``ArtifactStore.commit`` / ``ArtifactStore.resolve``、G1 的
   ``run_static``、G2 的 ``Reviewer.review``、围栏解析 ``parse_fenced_blocks``
   都有计数（计数器由 ``offline_run`` 挂上）。G2 必须单列：G1 一个层就能让
   8/9 个任务通过，一个「悄悄不再调用评审器」的实现照样跑得完 run。
2. **没有旁路**：``artifacts/`` 下每个文件都必须能在 ``ArtifactStore`` 自己的
   记录里找到。这一条才是关键 —— 单看调用次数抓不住「文件悄悄写进了产物目录
   却没经 store」的未来优化，逐路径比对记录才抓得住。

对 brief 样例的两处校正：

``run_static`` 必须**同时**替换 ``agentcrew.gate.static_checks.run_static`` 与
``agentcrew.gate.gatekeeper.run_static``：后者用 ``from ... import run_static``
把函数绑进了自己的模块全局，pytest 收集期所有模块都已导入，只改源模块属性时
调用仍走原函数，计数恒为 0（假的「走了旁路」）。
"""

import json

from agentcrew.runtime.checkpoint import RunStatus


def test_commit_resolve_and_gate_all_invoked(offline_run):
    counters = offline_run.counters
    assert counters["commit"] > 0, "ArtifactStore.commit 未被调用 —— mock 走了旁路"
    assert counters["resolve"] > 0, "ArtifactStore.resolve 未被调用"
    assert counters["static"] > 0, "G1 静态门禁未被调用"
    assert counters["review"] > 0, "G2 LLM 评审层未被调用 —— 门禁少了一层照样能跑绿"
    assert counters["parse"] > 0, "围栏解析未被调用"


def test_no_direct_file_writes_outside_store(offline_run):
    """``artifacts/`` 下每个文件都必须出现在 ``ArtifactStore`` 的记录里。

    比对的是 store 的**记录**（每个文件落盘时由它自己记下的路径），而不是
    「commit 被调用了几次」：一个绕过 store 的 ``open(...).write()`` 不会增加
    任何计数，只会让盘上多出一个记录里没有的文件。
    """
    # commit 被替换成 no-op 时产物必然缺失 → G1 全部驳回 → run 不可能 COMPLETED。
    # 这条前置断言让「no-op commit」这类破坏在下面之前就现形。
    assert offline_run.record.status is RunStatus.COMPLETED

    written = {
        record.path
        for store in offline_run.stores
        for record in store.records()
    }
    assert written, "ArtifactStore 一条记录都没有；下面的包含断言会真空通过"

    on_disk = {
        p.relative_to(offline_run.artifacts).as_posix()
        for p in offline_run.artifacts.rglob("*")
        if p.is_file()
    }
    on_disk.discard("MANIFEST.json")  # 由 store.write_manifest 写的元数据

    surplus = sorted(on_disk - written)
    assert not surplus, f"存在未经 ArtifactStore 写入的文件：{surplus}"

    # 交叉校验：记录与封版清单（同一批记录渲染而成）必须逐路径一致，
    # 证明上面用的这份记录就是权威集合，不是一份被裁剪过的视图。
    manifest = json.loads(
        (offline_run.artifacts / "MANIFEST.json").read_text(encoding="utf-8"))
    assert written == {entry["path"] for entry in manifest["entries"]}

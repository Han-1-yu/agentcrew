"""批次 8 回归：B-18（并发裁决）/ B-20（SDK 超时语义）。

A-23（预算检查顺序）没有单独的端到端用例——它的验证成本高于改动本身：
要构造「已超限 + observer 抛异常」的工厂状态，需要 mock 整条模型调用链。
改动只是把纯计算的 `exhaustion_error` 提到两个 emit 之前并各包 `try`，
风险面由既有 65 个预算用例覆盖。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from agentcrew_sdk.client import SSE_MIN_TIMEOUT_SECONDS, AgentCrewClient

from agentcrew.runtime.approvals import ApprovalError, ApprovalStore


class TestB18ConcurrentDecide:
    def _store(self, tmp_path: Path) -> tuple[ApprovalStore, str]:
        store = ApprovalStore(tmp_path)
        request = store.create("step", run_id="R-1")
        return store, request.id

    def test_concurrent_decide_only_one_wins(self, tmp_path):
        """B-18：8 个线程同时裁决，只能有一个生效，其余必须明确报冲突。"""
        store, request_id = self._store(tmp_path)
        barrier = threading.Barrier(8)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()
            try:
                store.decide(request_id, {"action": "run"})
                result = "ok"
            except ApprovalError:
                result = "conflict"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert outcomes.count("ok") == 1, outcomes
        assert outcomes.count("conflict") == 7, outcomes
        # 落盘只有一条 decide：并发写不能留下多条互相覆盖的裁决。
        lines = (Path(tmp_path) / "approvals.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert sum('"op": "decide"' in ln for ln in lines) == 1

    def test_sequential_double_decide_rejected(self, tmp_path):
        store, request_id = self._store(tmp_path)
        store.decide(request_id, {"action": "run"})
        with pytest.raises(ApprovalError, match="不能重复裁决"):
            store.decide(request_id, {"action": "abort"})

    def test_consume_is_idempotent(self, tmp_path):
        store, request_id = self._store(tmp_path)
        store.decide(request_id, {"action": "run"})
        store.consume(request_id)
        store.consume(request_id)   # 第二次不应重复追加
        lines = (Path(tmp_path) / "approvals.jsonl").read_text(
            encoding="utf-8").splitlines()
        assert sum('"op": "consume"' in ln for ln in lines) == 1


class TestB20SdkTimeout:
    def _client(self, timeout: float = 30.0) -> AgentCrewClient:
        return AgentCrewClient("http://127.0.0.1:1", timeout=timeout)

    def test_none_falls_back_to_client_default(self):
        assert self._client(30.0)._request_timeout(None) == 30.0

    def test_explicit_zero_means_no_timeout(self):
        """B-20：此前 `timeout or self.timeout` 把显式 0 静默换成默认值。"""
        assert self._client(30.0)._request_timeout(0) is None

    def test_negative_also_means_no_timeout(self):
        assert self._client(30.0)._request_timeout(-5) is None

    def test_explicit_value_is_respected(self):
        assert self._client(30.0)._request_timeout(7.5) == 7.5

    def test_stream_has_minimum_above_heartbeat(self):
        """SSE 超时必须明显大于服务端 30s 心跳，否则空闲期会先断开。"""
        got = self._client(30.0)._request_timeout(None, stream=True)
        assert got is not None and got >= SSE_MIN_TIMEOUT_SECONDS > 30

    def test_stream_scales_with_client_timeout(self):
        slow = self._client(120.0)._request_timeout(None, stream=True)
        assert slow is not None and slow >= 240

    def test_stream_explicit_zero_stays_unbounded(self):
        assert self._client(30.0)._request_timeout(0, stream=True) is None

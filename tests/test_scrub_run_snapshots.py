"""``scripts/scrub_run_snapshots.py``：历史 run.json 的一次性脱敏回填。

覆盖三条边界：

1. 默认 **dry-run**：报告命中但不写盘；
2. ``--yes``：标量与 ``webhooks[*].secret`` 都抹成 ``***``，空 secret 与其余
   字段原样保留；
3. 重复运行幂等，已清理的文件不再计入命中。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agentcrew.config import REDACTED

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scrub_run_snapshots.py"


@pytest.fixture(scope="module")
def scrub():
    spec = importlib.util.spec_from_file_location("scrub_run_snapshots", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_run(root: Path, run_id: str, snapshot: dict) -> Path:
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    path = run_dir / "run.json"
    path.write_text(json.dumps({
        "run_id": run_id, "config_snapshot": snapshot, "schema_version": 1,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _snapshot() -> dict:
    return {
        "webhook_secret": "scalar-secret",
        "webhook_trigger_secret": "trigger-secret",
        "webhooks": [
            {"url": "https://a.example", "secret": "receiver-secret"},
            {"url": "https://b.example", "secret": ""},
            {"url": "https://c.example"},          # 没配 secret 的条目
        ],
        "gate_mode": "both",
    }


def test_dry_run_reports_without_writing(scrub, tmp_path):
    root = tmp_path / "output"
    path = _write_run(root, "R1", _snapshot())
    before = path.read_text(encoding="utf-8")

    assert scrub.main(["--output-dir", str(root)]) == 0

    assert path.read_text(encoding="utf-8") == before
    assert "scalar-secret" in path.read_text(encoding="utf-8")


def test_yes_redacts_all_secret_fields_and_preserves_the_rest(scrub, tmp_path):
    root = tmp_path / "output"
    path = _write_run(root, "R1", _snapshot())

    assert scrub.main(["--output-dir", str(root), "--yes"]) == 0

    document = json.loads(path.read_text(encoding="utf-8"))
    snapshot = document["config_snapshot"]
    assert snapshot["webhook_secret"] == REDACTED
    assert snapshot["webhook_trigger_secret"] == REDACTED
    assert [w.get("secret") for w in snapshot["webhooks"]] == [REDACTED, "", None]
    assert snapshot["webhooks"][0]["url"] == "https://a.example"
    assert snapshot["gate_mode"] == "both"
    assert document["run_id"] == "R1" and document["schema_version"] == 1
    assert "receiver-secret" not in path.read_text(encoding="utf-8")

    # 幂等：再跑一次不再命中任何字段
    assert scrub.scrub_file(path, apply=False) == []


def test_missing_output_dir_returns_config_error(scrub, tmp_path):
    assert scrub.main(["--output-dir", str(tmp_path / "nope")]) == 2

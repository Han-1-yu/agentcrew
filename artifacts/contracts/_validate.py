"""Validate real run data against the contract schemas. Read-only."""
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(r"E:\agentcrew\artifacts\contracts\schemas")
RUN_DIR = Path(r"E:\agentcrew\output\20261006-183534-fa97")


def load(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def check(label, schema, instance):
    v = Draft202012Validator(schema)
    errs = sorted(v.iter_errors(instance), key=lambda e: e.path)
    if errs:
        print(f"[FAIL] {label}: {len(errs)} error(s)")
        for e in errs[:8]:
            loc = "/".join(str(p) for p in e.absolute_path) or "<root>"
            print(f"  - {loc}: {e.message[:200]}")
        return False
    print(f"[ OK ] {label}")
    return True


def main():
    run = json.loads((RUN_DIR / "run.json").read_text(encoding="utf-8"))
    tasks = json.loads((RUN_DIR / "tasks.json").read_text(encoding="utf-8"))
    ledger_lines = [
        json.loads(line)
        for line in (RUN_DIR / "ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    run_schema = load("run.json")
    task_schema = load("task.json")
    event_schema = load("event.json")
    gate_schema = load("gate.json")
    artifact_schema = load("artifact.json")

    ok = True
    ok &= check("run.json", run_schema, run)

    # tasks.json top-level: {"tasks": [...]}
    for i, t in enumerate(tasks["tasks"]):
        if not check(f"tasks.json[{i}] id={t.get('id')}", task_schema, t):
            ok = False

    # ledger events
    for i, e in enumerate(ledger_lines):
        if not check(f"ledger[{i}] event={e.get('event')}", event_schema, e):
            ok = False

    # gate records: validate each task's reviews against gate.json
    for t in tasks["tasks"]:
        for r in t.get("reviews", []):
            if not check(f"review {t['id']} r{r.get('round')}/{r.get('layer')}", gate_schema, r):
                ok = False

    # artifact records: validate each task.result.artifacts against artifact.json
    for t in tasks["tasks"]:
        res = t.get("result")
        if not res:
            continue
        for a in res.get("artifacts", []):
            if not check(f"artifact {t['id']} {a.get('path')}", artifact_schema, a):
                ok = False

    # MANIFEST: top-level {run_id, generated_at, entries: [ArtifactRecord, ...]}
    manifest = json.loads((RUN_DIR / "artifacts" / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest_top = {
        "type": "object",
        "required": ["run_id", "generated_at", "entries"],
        "additionalProperties": False,
        "properties": {
            "run_id": {"type": "string"},
            "generated_at": {"type": "string"},
            "entries": {"type": "array"},
        },
    }
    v = Draft202012Validator(manifest_top)
    errs = list(v.iter_errors(manifest))
    if errs:
        print(f"[FAIL] MANIFEST.json top-level: {len(errs)} error(s)")
        for e in errs[:5]:
            print("  -", e.message[:200])
        ok = False
    else:
        print("[ OK ] MANIFEST.json top-level")
    for i, entry in enumerate(manifest["entries"]):
        if not check(f"MANIFEST.entries[{i}] {entry.get('path')}", artifact_schema, entry):
            ok = False

    print()
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""清理历史 ``output/*/run.json`` 里明文落盘的 webhook 签名密钥。

背景（2026-11-18 审计）：``Settings.config_snapshot()`` 过去只脱敏标量
``webhook_secret``，R15 多接收端的 ``webhooks[*].secret`` 会原样写进快照，
随后随 ``run.json`` 落盘。代码侧已修（新 run 不再泄露），但**旧 run 的文件
不会自己变干净** —— 这个脚本负责一次性回填。

行为：

* 默认 **dry-run**：只报告「哪些文件、哪些字段会被改」，不写盘；
* ``--yes`` 才真正重写（同目录临时文件 + 原子替换，保留其余字段与缩进）；
* 只处理可识别的密钥字段：
  - ``config_snapshot.webhook_secret``
  - ``config_snapshot.webhook_trigger_secret``
  - ``config_snapshot.webhooks[*].secret``
* 已经是 ``***`` / 空串的字段不动，便于重复运行；
* 解析失败的文件跳过并计入 ``failed``，退出码 1（不中断其余文件）。

用法::

    python scripts/scrub_run_snapshots.py                 # 预演
    python scripts/scrub_run_snapshots.py --yes           # 真改
    python scripts/scrub_run_snapshots.py --output-dir D  # 指定输出目录
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agentcrew.config import REDACTED  # noqa: E402  (脚本可独立运行)

#: 快照里需要脱敏的标量字段。
_SCALAR_FIELDS = ("webhook_secret", "webhook_trigger_secret")


def scrub_snapshot(snapshot: object) -> tuple[dict, list[str]]:
    """就地清洗一份 ``config_snapshot``，返回 ``(新对象, 被改字段路径)``。

    只重建涉及的层级，其余值原样保留（不改变键顺序与类型）。
    """
    if not isinstance(snapshot, Mapping):
        return {}, []
    data = dict(snapshot)
    changed: list[str] = []

    for field in _SCALAR_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and value and value != REDACTED:
            data[field] = REDACTED
            changed.append(field)

    webhooks = data.get("webhooks")
    if isinstance(webhooks, list):
        cleaned_targets = []
        for index, target in enumerate(webhooks):
            if not isinstance(target, Mapping):
                cleaned_targets.append(target)
                continue
            target_data = dict(target)
            secret = target_data.get("secret")
            if isinstance(secret, str) and secret and secret != REDACTED:
                target_data["secret"] = REDACTED
                changed.append(f"webhooks[{index}].secret")
            cleaned_targets.append(target_data)
        data["webhooks"] = cleaned_targets
    return data, changed


def scrub_file(path: Path, *, apply: bool) -> list[str]:
    """清洗单个 ``run.json``；``apply=False`` 时只计算不写盘。"""
    raw = path.read_text(encoding="utf-8")
    document = json.loads(raw)
    if not isinstance(document, Mapping):
        raise ValueError("run.json 顶层不是 JSON 对象")
    snapshot = document.get("config_snapshot")
    if not isinstance(snapshot, Mapping):
        return []
    cleaned, changed = scrub_snapshot(snapshot)
    if not changed or not apply:
        return changed

    updated = dict(document)
    updated["config_snapshot"] = cleaned
    # 原子替换：先写同目录临时文件，再 os.replace —— 中途崩溃不会留下半截
    # run.json（它是 resume 的唯一入口）。
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(updated, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="脱敏历史 run.json 里的 webhook 签名密钥（默认预演）")
    parser.add_argument("--output-dir", default="output",
                        help="run 根目录（默认 output）")
    parser.add_argument("--yes", action="store_true",
                        help="真正写盘；不加只预演")
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        print(f"目录不存在：{output_dir}", file=sys.stderr)
        return 2

    scanned = changed_files = failed = 0
    for run_json in sorted(output_dir.glob("*/run.json")):
        scanned += 1
        try:
            changed = scrub_file(run_json, apply=args.yes)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            failed += 1
            print(f"跳过（无法解析）：{run_json}: {exc}", file=sys.stderr)
            continue
        if changed:
            changed_files += 1
            prefix = "已清理" if args.yes else "将清理"
            print(f"{prefix} {run_json}: {', '.join(changed)}")

    action = "清理" if args.yes else "预演"
    print(f"{action}完成：扫描 {scanned} 个 run，命中 {changed_files} 个，"
          f"失败 {failed} 个。")
    if not args.yes and changed_files:
        print("这是预演；确认无误后加 --yes 写盘。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

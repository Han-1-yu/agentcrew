"""C10 切片二 / R62：在**真实 serve 进程**上跑一次指引与回滚的端到端冒烟。

为什么需要第三条证据：

* ``tests/test_run_guidance.py`` 里的 ``ApiServer`` 是 pytest 进程内的内存实例；
* ``studio/tools/verify-api-bridge.js`` 用的是自带假后端；
* 两者都证明不了「另起一个 ``main.py serve`` 进程、CLI 在第三个进程写指引、
  HTTP 在第四个进程读」这条真实链路（guidance.jsonl 的跨进程追加写）。

本脚本起一个只绑回环、显式关闭认证的真实服务，依次验证：健康检查 → 带定向与
过期的写入 → 跨进程 CLI 写入后 GET 可见 → 撤回与配额释放 → 运行中压缩 409 /
终态压缩 200 → rewind dry-run 影响集 → 文件真的落盘。全部使用临时目录，
``dry_run`` 不写 run 状态，退出时回收服务进程与临时文件。

用法::

    python scripts/smoke_guidance.py            # 自动选空闲端口
    python scripts/smoke_guidance.py --port 18765
    python scripts/smoke_guidance.py --keep     # 保留临时目录供排障

退出码 0 = 全通过，1 = 有失败项，2 = 服务起不来。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 以 `python scripts/smoke_guidance.py` 直接运行时，sys.path[0] 是 scripts/，
# 不是仓库根 —— 显式补上，否则 import agentcrew 失败。
sys.path.insert(0, str(REPO))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _prepare_run(root: Path) -> tuple[Path, str]:
    """造两个最小 run：一个 running（写指引），一个 completed（压缩与 rewind）。"""
    from agentcrew.runtime.checkpoint import CheckpointStore, RunStatus
    from agentcrew.task.task import Task, TaskStatus
    from agentcrew.task.task_board import TaskBoard

    out = root / "output"
    CheckpointStore.create(out, project_goal="smoke", config_snapshot={},
                           run_id="R-live")
    done = CheckpointStore.create(out, project_goal="smoke",
                                  config_snapshot={}, run_id="R-done")
    done.save_record(done.load_record().model_copy(
        update={"status": RunStatus.COMPLETED}))
    board = TaskBoard([
        Task(id="T-001", title="上游", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[], dependencies=[],
             status=TaskStatus.SUCCEEDED),
        Task(id="T-002", title="回滚起点", description="d", role="backend_dev",
             acceptance_criteria=["a"], deliverables=[],
             dependencies=["T-001"], status=TaskStatus.SUCCEEDED),
        Task(id="T-FINAL", title="交付", description="d",
             role="project_manager", acceptance_criteria=["a"],
             deliverables=[], dependencies=["T-002"],
             status=TaskStatus.SUCCEEDED),
    ])
    done.save_tasks(board)
    return out, "T-002"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="0 = 自动选空闲端口")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()
    port = args.port or _free_port()

    root = Path(tempfile.mkdtemp(prefix="agentcrew-smoke-guidance-"))
    out, task_id = _prepare_run(root)
    env = dict(os.environ)
    # 合并顺序：代码默认 → .env → yaml → 环境变量 → CLI。环境变量压过仓库 .env。
    env["AGENTCREW_API_AUTH_ENABLED"] = "false"   # 只绑回环，匿名仅用于本机冒烟
    env["AGENTCREW_GUIDANCE_ENABLED"] = "true"
    env["AGENTCREW_OUTPUT_DIR"] = str(out)

    proc = subprocess.Popen(
        [sys.executable, "main.py", "serve", "--host", "127.0.0.1",
         "--port", str(port)],
        cwd=str(REPO), env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def call(method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", method=method,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(
                    response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(
                exc.read().decode("utf-8") or "{}")

    checks: list[tuple[str, bool, object]] = []

    def check(name: str, ok: bool, extra: object = None) -> None:
        checks.append((name, bool(ok), extra))

    healthy = False
    try:
        for _ in range(40):
            if proc.poll() is not None:
                break
            try:
                healthy = call("GET", "/health")[0] == 200
                if healthy:
                    break
            except (OSError, TimeoutError):
                time.sleep(0.25)
        if not healthy:
            output = ""
            if proc.stdout is not None:
                output = proc.stdout.read().decode("utf-8", errors="replace")
            print(f"serve 未就绪：{output[-2000:]}")
            return 2
        check("serve /health 200", True)

        status, posted = call(
            "POST", "/runs/R-live/guidance",
            {"text": "真机冒烟：把登录页改成绿色", "target": "T-003",
             "expires_in_seconds": 600})
        check("POST 指引带定向 + 过期 → 202",
              status == 202 and posted.get("target") == "T-003",
              (status, posted))
        item_id = str(posted.get("id") or "")

        cli = subprocess.run(
            [sys.executable, "main.py", "guide", "R-live",
             "另一条跨进程指引", "--output-dir", str(out), "--json"],
            cwd=str(REPO), env=env, capture_output=True, text=True,
            encoding="utf-8", check=False)
        _, listing = call("GET", "/runs/R-live/guidance")
        check("CLI 另一进程写入后 GET 可见 2 条",
              cli.returncode == 0 and len(listing.get("items", [])) == 2,
              (cli.returncode, cli.stdout, listing))
        check("GET 带 status / target / expires_at",
              all(key in listing["items"][0]
                  for key in ("status", "target", "expires_at")))

        status, _ = call("DELETE", f"/runs/R-live/guidance/{item_id}")
        _, after = call("GET", "/runs/R-live/guidance")
        statuses = [item["status"] for item in after.get("items", [])]
        check("DELETE 撤回 → 200 且状态 withdrawn",
              status == 200 and "withdrawn" in statuses, (status, statuses))
        check("撤回释放 pending 配额", after.get("pending") == 1,
              after.get("pending"))

        status, conflict = call("POST", "/runs/R-live/guidance/compact")
        check("运行中压缩 → 409", status == 409, (status, conflict))
        status, compacted = call("POST", "/runs/R-done/guidance/compact")
        check("终态压缩 → 200",
              status == 200 and compacted.get("after_lines", 9) <= 1,
              (status, compacted))

        status, rewound = call("POST", "/runs/R-done/rewind",
                               {"task": task_id, "dry_run": True})
        check("rewind dry-run → 200 带影响集",
              status == 200 and rewound.get("dry_run") is True
              and len(rewound.get("affected", [])) > 0, (status, rewound))
        check("guidance.jsonl 真落盘",
              (out / "R-live" / "guidance.jsonl").is_file())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if args.keep:
            print(f"临时目录保留：{root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    for name, ok, extra in checks:
        suffix = "" if ok else f" — {json.dumps(extra, ensure_ascii=False, default=str)}"
        print(f"{'PASS' if ok else 'FAIL'}  {name}{suffix}")
    failed = sum(1 for _, ok, _ in checks if not ok)
    print(f"真机 serve 冒烟：{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

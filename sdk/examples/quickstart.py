"""只读巡检 + 可选创建 run 的最小示例。

先装 SDK（任意一种）：

    pip install -e ./sdk
    PYTHONPATH=sdk python sdk/examples/quickstart.py

然后：

    python sdk/examples/quickstart.py                       # 只读巡检
    python sdk/examples/quickstart.py --goal "做一个 Todo 应用" --mock
    python sdk/examples/quickstart.py --base-url http://host:8765 --api-key KEY
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    from agentcrew_sdk import AgentCrewClient, AgentCrewError
except ImportError as exc:  # pragma: no cover - 只在未安装时触发
    raise SystemExit(
        "未找到 agentcrew_sdk：请先 `pip install -e ./sdk`，"
        "或用 `PYTHONPATH=sdk python sdk/examples/quickstart.py` 运行"
    ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get(
        "AGENTCREW_SDK_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--api-key", default=os.environ.get(
        "AGENTCREW_API_KEY"))
    parser.add_argument("--goal", help="给了就创建并等待一个 run；不给则只读巡检")
    parser.add_argument("--mock", action="store_true",
                        help="创建 run 时使用离线 mock 通道（无需任何 Key）")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="等待 run 进入终态的秒数")
    args = parser.parse_args()

    with AgentCrewClient(args.base_url, api_key=args.api_key) as client:
        try:
            print("health:", client.health())
        except AgentCrewError as exc:
            print(f"连不上服务：{exc}", file=sys.stderr)
            return 1

        if args.goal:
            options = {"mock": True} if args.mock else None
            created = client.create_run(args.goal, options=options)
            run_id = created["run_id"]
            print("run_id:", run_id)
            try:
                final = client.wait_for_run(run_id, timeout=args.timeout)
            except AgentCrewError as exc:
                print(f"等待失败（可能挂起等人工审批）：{exc}")
                return 2
            print("final status:", final.get("status"))
            print("stats:", json.dumps(client.get_stats(run_id),
                                      ensure_ascii=False, indent=2))
            return 0

        runs = client.list_runs()
        print(f"run 数量：{len(runs)}")
        if not runs:
            return 0
        latest = runs[0]["run_id"]
        print("最近一次 run:", latest)
        print("status:", json.dumps(client.get_status(latest),
                                   ensure_ascii=False, indent=2))
        page = client.get_ledger(latest, limit=5)
        print(f"ledger 前 {len(page.get('events', []))} 条 / 共 {page.get('total')} 条")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

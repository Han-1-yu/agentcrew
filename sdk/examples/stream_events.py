"""订阅一个 run 的 SSE 事件流，打印前 N 条后退出（Ctrl-C 可提前停）。

    pip install -e ./sdk
    python sdk/examples/stream_events.py <run_id> --max-events 30
    python sdk/examples/stream_events.py <run_id> --api-key KEY
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
        "或用 `PYTHONPATH=sdk python sdk/examples/stream_events.py` 运行"
    ) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="要订阅的 run 目录名")
    parser.add_argument("--base-url", default=os.environ.get(
        "AGENTCREW_SDK_BASE_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--api-key", default=os.environ.get(
        "AGENTCREW_API_KEY"))
    parser.add_argument("--max-events", type=int, default=20)
    args = parser.parse_args()

    with AgentCrewClient(args.base_url, api_key=args.api_key) as client:
        try:
            for index, event in enumerate(client.iter_events(args.run_id)):
                print(f"[{index}] {event.name}: "
                      f"{json.dumps(event.data, ensure_ascii=False)}")
                if index + 1 >= args.max_events:
                    break
        except KeyboardInterrupt:
            print("\n已停止订阅", file=sys.stderr)
        except AgentCrewError as exc:
            print(f"订阅失败：{exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

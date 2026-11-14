"""AgentCrew 桌面启动器：一条命令带起「后端 serve + Studio 桌面端」。

为什么要有它：Studio 只是壳，run 的规划/执行/门禁都在 `main.py serve` 里。手动开两
个终端（先 serve 再 npm start）容易漏，也容易忘了关后端。这个脚本负责：

1. 探测后端是否已在跑（在跑就复用，退出时也不去关别人的进程）；
2. 没跑就起 `main.py serve`（**只绑 127.0.0.1**），等 `/health` 通过再开窗口；
   默认不带 API Key——serve 在没有密钥时要求显式关认证，本脚本按「本机桌面」场景
   设 `AGENTCREW_API_AUTH_ENABLED=false` 并打印警告；想开认证就传 `--api-key`
   （此时需要在 Studio 设置里填同一个 Key）；
3. 首次使用时把 Studio 的 `apiBaseUrl` 预填成本机地址（只填空值，不动其他设置）；
4. Studio 退出后回收自己起的后端进程（Ctrl-C 同样回收）；
5. 全过程日志落 `logs/desktop-serve.log`，起不来时把最后几行打出来。

只依赖标准库。用法：

    python scripts/desktop.py              # 起后端 + Studio
    python scripts/desktop.py --smoke      # 只验后端能起来，然后退出（CI / 自检）
    python scripts/desktop.py --no-studio  # 只起后端，前台等 Ctrl-C
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STUDIO = ROOT / "studio"
LOG_DIR = ROOT / "logs"
SERVE_LOG = LOG_DIR / "desktop-serve.log"
HEALTH_TIMEOUT = 40.0
STUDIO_APP_DIRNAME = "AgentCrew Studio"


def _say(text: str) -> None:
    print(f"[AgentCrew] {text}", flush=True)


def _port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _health(port: int, api_key: str) -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_health(port: int, api_key: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _health(port, api_key):
            return True
        time.sleep(0.3)
    return False


def _tail(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "（日志文件还没生成）"
    return "\n".join(text.splitlines()[-lines:]) or "（日志为空）"


def _python() -> str:
    """优先用仓库自带的 .venv；没有就退回当前解释器。"""
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",   # Windows
        ROOT / ".venv" / "bin" / "python",           # macOS / Linux
    ):
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _npm() -> str | None:
    for name in ("npm.cmd", "npm") if os.name == "nt" else ("npm",):
        from shutil import which
        found = which(name)
        if found:
            return found
    return None


def _seed_studio_settings(port: int) -> None:
    """把 Studio 的 apiBaseUrl 预填成本机地址。

    只填空值：用户手动改过（比如连远端服务）就原样保留；文件不存在则创建一个最小
    的。Studio 的设置里**没有**后端密钥——密钥由 Electron safeStorage 单独加密存，
    本脚本不碰。
    """
    appdata = os.environ.get("APPDATA")
    if os.name == "nt" and appdata:
        settings_dir = Path(appdata) / STUDIO_APP_DIRNAME
    elif sys.platform == "darwin":
        settings_dir = Path.home() / "Library" / "Application Support" / STUDIO_APP_DIRNAME
    else:
        settings_dir = Path.home() / ".config" / STUDIO_APP_DIRNAME
    path = settings_dir / "settings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        _say(f"Studio 设置读不动（{path}），跳过预填")
        return
    if not isinstance(data, dict):
        return
    current = str(data.get("apiBaseUrl") or "").strip()
    if current:
        return
    data["apiBaseUrl"] = f"http://127.0.0.1:{port}"
    data.setdefault("repoRoot", str(ROOT))
    data.setdefault("outputDir", str(ROOT / "output"))
    try:
        settings_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _say(f"已把 Studio 后端地址预填为 {data['apiBaseUrl']}")
    except OSError as exc:
        _say(f"Studio 设置写入失败：{exc}")


def _start_serve(port: int, api_key: str) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = SERVE_LOG.open("a", encoding="utf-8")
    log.write(f"\n===== {datetime.now().isoformat(timespec='seconds')} serve :{port} =====\n")
    log.flush()
    command = [_python(), "main.py", "serve", "--host", "127.0.0.1", "--port", str(port)]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    if api_key:
        command += ["--api-key", api_key]
    else:
        # serve 在没有密钥时会拒绝匿名启动；本机桌面场景显式关认证（只绑回环）。
        env["AGENTCREW_API_AUTH_ENABLED"] = "false"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env, **kwargs
    )


def _stop_serve(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    _say("正在关闭后端…")
    try:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        proc.wait(timeout=8)
        return
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动 AgentCrew 桌面端（后端 + Studio）")
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGENTCREW_DESKTOP_PORT", "8765")))
    parser.add_argument("--api-key", default=os.environ.get("AGENTCREW_DESKTOP_API_KEY", ""),
                        help="给后端设 API Key 并开启认证（默认空：仅本机关闭认证；"
                             "设了则需在 Studio 设置里填同一个 Key）")
    parser.add_argument("--no-seed", action="store_true", help="不预填 Studio 的后端地址")
    parser.add_argument("--no-studio", action="store_true", help="只起后端，前台等待 Ctrl-C")
    parser.add_argument("--smoke", action="store_true", help="起后端→验 /health→关闭，退出（自检用）")
    args = parser.parse_args(argv)

    port = args.port
    api_key = args.api_key.strip()
    started: subprocess.Popen | None = None

    if not api_key:
        _say("未设 API Key：后端只监听 127.0.0.1 并关闭认证（本机桌面默认）")

    if _health(port, api_key):
        _say(f"后端已在 127.0.0.1:{port} 运行，直接复用（退出时不会关它）")
    elif _port_busy(port):
        _say(f"端口 {port} 已被其他程序占用，且 /health 不通。换一个端口：--port 8766")
        return 2
    else:
        _say(f"启动后端 127.0.0.1:{port} …（日志 logs/desktop-serve.log）")
        started = _start_serve(port, api_key)
        if not _wait_health(port, api_key, HEALTH_TIMEOUT):
            code = started.poll()
            _say(f"后端没起来（退出码 {code}）。日志最后几行：")
            print(_tail(SERVE_LOG), flush=True)
            _stop_serve(started)
            return 2
        _say("后端就绪")

    try:
        if args.smoke:
            _say("/health 通过，smoke 结束")
            return 0
        if args.no_studio:
            _say("只启后端；按 Ctrl-C 退出")
            while True:
                time.sleep(1)
                if started is not None and started.poll() is not None:
                    _say(f"后端进程已退出（{started.returncode}）")
                    return 1

        if not args.no_seed:
            _seed_studio_settings(port)

        if not STUDIO.is_dir():
            _say(f"找不到 Studio 目录：{STUDIO}")
            return 2
        npm = _npm()
        if npm is None:
            _say("没找到 npm：请先安装 Node.js 20+，或直接用 python main.py serve")
            return 2
        if not (STUDIO / "node_modules").is_dir():
            _say("首次运行，安装 Studio 依赖（npm install）…")
            installed = subprocess.run([npm, "install"], cwd=str(STUDIO))
            if installed.returncode != 0:
                _say("npm install 失败，请到 studio/ 手动执行")
                return 2

        _say("打开 Studio 窗口（关窗即退出）")
        studio = subprocess.run([npm, "start"], cwd=str(STUDIO))
        return studio.returncode
    except KeyboardInterrupt:
        _say("收到 Ctrl-C")
        return 130
    finally:
        if started is not None:
            _stop_serve(started)


if __name__ == "__main__":
    raise SystemExit(main())

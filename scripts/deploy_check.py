"""目标机上线自检：把 `docs/DEPLOY-CHECK-2026-11-13.md` §4 的判据做成一条命令。

部署侧此前拿到的是**一张表**：10 项判据、每项一句话。人照着敲，敲漏一两项很正常，
而且「敲过了」和「真的过了」在记录上没区别。这个脚本把能自动判的那部分变成
``PASS/FAIL/SKIP`` 三态，剩下的（反代配置细节、监控告警规则、密钥托管方式）
明确标成 SKIP 并打印人工判据——**不假装验证过**。

用法::

    # 只有本机文件时（数据盘 / 备份恢复 / 预算上限）
    python scripts/deploy_check.py --data-dir /var/lib/agentcrew/output

    # 服务已起（再加认证与 metrics）
    python scripts/deploy_check.py --base-url https://crew.example.com \
        --api-key "$AGENTCREW_API_KEY" --data-dir /var/lib/agentcrew/output

    # 有容器时（挂卷 + healthcheck）
    python scripts/deploy_check.py --container agentcrew --data-dir ...

退出码：有 FAIL 为 1，全 PASS/SKIP 为 0（便于接进部署流水线）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

#: 落盘日志里绝不该出现的东西（真出现就是密钥泄露，直接 FAIL）。
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{16,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{16,}"),
    re.compile(r"api[_-]?key\s*[=:]\s*['\"]?[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)


class Check:
    """一条自检结论。``detail`` 在失败时给出「为什么」与「手工怎么验」。"""

    def __init__(self, name: str, status: str, detail: str = "") -> None:
        self.name = name
        self.status = status
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _http(
    url: str, *, key: str = "", timeout: float = 10.0,
) -> tuple[int, str]:
    """发一次 GET，返回 (状态码, 正文)；网络错误统一成 (0, 原因)。"""
    request = urllib.request.Request(url)
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def check_auth(base_url: str, key: str) -> list[Check]:
    """REST 认证：带 key 200、不带 key 401（401 才说明认证真的开着）。"""
    if not base_url:
        return [Check("REST 认证", SKIP, "未提供 --base-url")]
    if not key:
        return [Check("REST 认证", SKIP, "未提供 --api-key，只能验「无 key 应 401」")]
    status_no_key, _ = _http(f"{base_url.rstrip('/')}/health")
    status_key, body = _http(f"{base_url.rstrip('/')}/health", key=key)
    if status_key == 200 and status_no_key == 401:
        return [Check("REST 认证", PASS, "带 key 200、无 key 401")]
    return [Check(
        "REST 认证", FAIL,
        f"带 key={status_key}（应为 200）、无 key={status_no_key}（应为 401）；"
        f"正文片段：{body[:120]}")]


def check_metrics(base_url: str, key: str) -> list[Check]:
    """``/metrics`` 可达且含 ``agentcrew_up``（监控抓不到就没法告警）。"""
    if not base_url or not key:
        return [Check("metrics 暴露", SKIP, "需要 --base-url 与 --api-key")]
    status, body = _http(f"{base_url.rstrip('/')}/metrics", key=key)
    if status != 200:
        return [Check("metrics 暴露", FAIL, f"GET /metrics → {status}")]
    if "agentcrew_up" not in body:
        return [Check("metrics 暴露", FAIL, "返回 200 但没有 agentcrew_up 指标")]
    return [Check("metrics 暴露", PASS, "200 且含 agentcrew_up")]


def check_tls(base_url: str) -> list[Check]:
    """TLS：https 时校验证书有效期；http 时明确 SKIP（要反代补）。"""
    if not base_url:
        return [Check("TLS / 反代", SKIP, "未提供 --base-url")]
    if not base_url.startswith("https://"):
        return [Check(
            "TLS / 反代", SKIP,
            "当前是明文 http；上线必须挂反代（并关掉 SSE 缓冲，见 OPERATIONS §8）")]
    host = urllib.request.url2pathname(base_url.split("://", 1)[1].split("/", 1)[0])
    host = host.split(":")[0]
    context = ssl.create_default_context()
    try:
        with context.wrap_socket(
            __import__("socket").create_connection((host, 443), timeout=10),
            server_hostname=host,
        ) as sock:
            cert = sock.getpeercert()
    except (OSError, ssl.SSLError) as exc:
        return [Check("TLS / 反代", FAIL, f"握手失败：{type(exc).__name__}: {exc}")]
    expires = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(
        tzinfo=timezone.utc)
    days = (expires - datetime.now(timezone.utc)).days
    if days < 30:
        return [Check("TLS / 反代", FAIL, f"证书 {days} 天后到期（<30 天）")]
    return [Check("TLS / 反代", PASS, f"证书有效，剩余 {days} 天")]


def check_separate_disk(data_dir: Path, repo_dir: Path) -> list[Check]:
    """数据盘独立：产出目录与仓库不在同一个挂载点（否则日志/产物会写爆系统盘）。"""
    if not data_dir.is_dir():
        return [Check("独立数据盘", SKIP, f"数据目录不存在：{data_dir}")]
    if os.stat(data_dir).st_dev != os.stat(repo_dir).st_dev:
        return [Check("独立数据盘", PASS, "数据目录与仓库不在同一挂载点")]
    return [Check(
        "独立数据盘", FAIL,
        f"{data_dir} 与 {repo_dir} 在同一个挂载点；建议单独挂盘并改 AGENTCREW_OUTPUT_DIR")]


def check_budget_cap() -> list[Check]:
    """成本上限：``max_cost_usd`` 必须配（不配＝跑飞了没人拦）。"""
    try:
        from agentcrew.config import Settings

        settings = Settings.load()
    except Exception as exc:  # noqa: BLE001 —— 配置读不出来本身就是问题
        return [Check("成本上限", FAIL, f"配置加载失败：{type(exc).__name__}: {exc}")]
    cap = getattr(settings, "max_cost_usd", None)
    if cap is None:
        return [Check("成本上限", FAIL, "max_cost_usd 未设置（AGENTCREW_MAX_COST_USD）")]
    if cap <= 0:
        return [Check("成本上限", FAIL, f"max_cost_usd={cap} 不是正数")]
    return [Check("成本上限", PASS, f"max_cost_usd={cap}")]


def _scan_secrets(text: str) -> list[str]:
    return [pattern.pattern for pattern in _SECRET_PATTERNS if pattern.search(text)]


def check_log_redaction(log_file: Path | None) -> list[Check]:
    """日志脱敏：抽查最近日志文件里有没有密钥形状的串。"""
    if log_file is None:
        return [Check(
            "日志脱敏", SKIP,
            "未提供 --log-file；上线前请确认 AGENTCREW_LOG_JSON=true 并抽查一条日志")]
    if not log_file.is_file():
        return [Check("日志脱敏", FAIL, f"日志文件不存在：{log_file}")]
    text = log_file.read_text(encoding="utf-8", errors="replace")
    hits = _scan_secrets(text)
    if hits:
        return [Check("日志脱敏", FAIL, f"命中疑似密钥：{', '.join(hits)}")]
    return [Check("日志脱敏", PASS, "未命中密钥形状的串")]


def _digest_files(root: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def check_backup_restore(data_dir: Path) -> list[Check]:
    """备份恢复演练：抽 3 个 run 的 run.json + ledger.jsonl，备份→异目录还原→校验。"""
    if not data_dir.is_dir():
        return [Check("备份恢复演练", SKIP, f"数据目录不存在：{data_dir}")]
    runs = sorted(
        (path for path in data_dir.iterdir()
         if path.is_dir() and (path / "run.json").is_file()),
        key=lambda path: path.name, reverse=True)[:3]
    if not runs:
        return [Check("备份恢复演练", SKIP, "数据目录里还没有 run（先跑一次冒烟）")]
    names: list[str] = []
    for run in runs:
        names.append(f"{run.name}/run.json")
        if (run / "ledger.jsonl").is_file():
            names.append(f"{run.name}/ledger.jsonl")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "backup"
            restored = Path(tmp) / "restored"
            for name in names:
                target = backup / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(data_dir / name, target)
            shutil.copytree(backup, restored)
            before = _digest_files(data_dir, names)
            after = _digest_files(restored, names)
    except OSError as exc:
        return [Check("备份恢复演练", FAIL, f"备份/还原失败：{type(exc).__name__}: {exc}")]
    if before != after:
        return [Check("备份恢复演练", FAIL, "还原后内容与源不一致")]
    return [Check(
        "备份恢复演练", PASS, f"{len(runs)} 个 run / {len(names)} 个文件 备份还原一致")]


def check_container(container: str) -> list[Check]:
    """容器：必须有数据卷挂载，且 healthcheck 存在。"""
    if not container:
        return [Check("容器挂卷/healthcheck", SKIP, "未提供 --container")]
    import subprocess

    try:
        raw = subprocess.run(
            ["docker", "inspect", container],
            capture_output=True, text=True, timeout=20, check=True,
        ).stdout
        inspected = json.loads(raw)[0]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        return [Check(
            "容器挂卷/healthcheck", FAIL, f"docker inspect 失败：{type(exc).__name__}: {exc}")]
    binds = (inspected.get("HostConfig") or {}).get("Binds") or []
    health = (inspected.get("Config") or {}).get("Healthcheck")
    missing = []
    if not binds:
        missing.append("没有数据卷挂载（重启即丢产物）")
    if not health:
        missing.append("没有 HEALTHCHECK")
    if missing:
        return [Check("容器挂卷/healthcheck", FAIL, "；".join(missing))]
    return [Check(
        "容器挂卷/healthcheck", PASS, f"{len(binds)} 个挂载 + healthcheck 已配")]


def _manual_items() -> list[Check]:
    """脚本判不了、必须人确认的项：明确列出来，别让「全绿」产生误解。"""
    return [
        Check("反代细节（SSE 不缓冲 / 超时 / 限流）", SKIP,
              "人工：nginx proxy_buffering off，读超时 ≥ SSE 心跳周期"),
        Check("监控告警规则", SKIP,
              "人工：agentcrew_up==0 与 run 失败率两条告警各触发一次"),
        Check("密钥托管", SKIP,
              "人工：密钥只走环境变量/密钥管理，不落 .env 提交、不进快照"),
        Check("webhook 接收端幂等", SKIP,
              "人工：同一 event_id 重投两次，接收端只处理一次"),
    ]


def run_checks(args: argparse.Namespace) -> list[Check]:
    data_dir = Path(args.data_dir).resolve() if args.data_dir else Path("output")
    repo_dir = Path(args.repo_dir).resolve()
    checks: list[Check] = []
    checks += check_auth(args.base_url, args.api_key)
    checks += check_metrics(args.base_url, args.api_key)
    checks += check_tls(args.base_url)
    checks += check_separate_disk(data_dir, repo_dir)
    checks += check_budget_cap()
    checks += check_log_redaction(Path(args.log_file) if args.log_file else None)
    checks += check_backup_restore(data_dir)
    checks += check_container(args.container)
    checks += _manual_items()
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default="", help="服务地址，如 https://crew.example.com")
    parser.add_argument("--api-key", default="", help="调用 API 的密钥（不落盘）")
    parser.add_argument("--data-dir", default="output", help="产出目录（默认 output）")
    parser.add_argument("--repo-dir", default=".", help="仓库目录（默认当前目录）")
    parser.add_argument("--log-file", default="", help="抽查脱敏用的日志文件")
    parser.add_argument("--container", default="", help="要 inspect 的容器名")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(argv)

    checks = run_checks(args)
    if args.json:
        print(json.dumps([item.as_dict() for item in checks],
                         ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"[{item.status:4}] {item.name} — {item.detail}")
        counts = {status: sum(1 for c in checks if c.status == status)
                  for status in (PASS, FAIL, SKIP)}
        print(f"\n合计：{counts[PASS]} PASS / {counts[FAIL]} FAIL / {counts[SKIP]} SKIP")
    return 1 if any(item.status == FAIL for item in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())

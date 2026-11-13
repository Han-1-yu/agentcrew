"""docker 后端的**真实容器**台账（默认跳过，只在有 docker 的机器上开）。

为什么单独一个文件：``tests/test_sandbox_backend.py`` 断言的是「命令行参数拼得对不对」
（纯函数、零依赖），它答不出「这些参数真进容器后是否真的隔离」。差别就在于后者需要
一台有 docker 的机器——本机没有，于是用 GitHub Actions 的 ubuntu runner 当那台机器：

```bash
docker pull python:3.12-slim
AGENTCREW_DOCKER_LIVE=1 python -m pytest tests/test_docker_isolation_live.py -q
```

CI 里由 ``.github/workflows/ci.yml`` 的 ``sandbox-docker`` job 执行；本机/无 docker 环境
一律 skip（不加这个开关就会「因为环境缺 docker 而绿」，那是假绿）。

四条用例对应 README/THREAT_MODEL 里承诺的四件事：
1. 容器里能跑 python（后端本身可用）；
2. **出不了网**（``--network none``）；
3. **看不到宿主文件**（只挂 workdir）；
4. 非 root + 内存/进程上限真的生效（``--user 65534`` / ``--memory`` / ``--pids-limit``）。
"""

from __future__ import annotations

import os

import pytest

from agentcrew.tools.sandbox import DockerBackend, docker_available

LIVE = os.environ.get("AGENTCREW_DOCKER_LIVE") == "1"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not LIVE or not docker_available(),
        reason="需要真 docker：设 AGENTCREW_DOCKER_LIVE=1 且在装有 docker 的机器上跑",
    ),
]


def _backend() -> DockerBackend:
    return DockerBackend(memory_mb=256, cpus=1.0, pids_limit=64)


async def test_container_runs_python_and_workspace_is_writable(tmp_path):
    workdir = tmp_path / "ws"
    workdir.mkdir()
    result = await _backend().run(
        [  # type: ignore[list-item]
            "python3", "-c",
            "from pathlib import Path;"
            "Path('out.txt').write_text('hello');"
            "print('ok')",
        ],
        workdir=workdir,
        timeout=120,
    )
    assert result.exit_code == 0, result.stderr
    assert "ok" in result.stdout
    assert (workdir / "out.txt").read_text() == "hello"


async def test_container_has_no_network(tmp_path):
    """``--network none``：连不出去，任何 socket 建连都必须失败。"""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), 3)\n"
        "except OSError as exc:\n"
        "    print('blocked:', type(exc).__name__)\n"
        "else:\n"
        "    print('NETWORK_REACHABLE')\n"
    )
    result = await _backend().run(["python3", "-c", code], workdir=workdir, timeout=120)
    assert "NETWORK_REACHABLE" not in result.stdout
    assert "blocked:" in result.stdout


async def test_container_cannot_read_host_files(tmp_path):
    """只挂 workdir：宿主上的普通文件在容器里必须不存在。"""
    workdir = tmp_path / "ws"
    workdir.mkdir()
    secret = tmp_path / "host-secret.txt"
    secret.write_text("host-only", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        f"p = Path({str(secret)!r})\n"
        "try:\n"
        "    print('READ:', p.read_text())\n"
        "except OSError as exc:\n"
        "    print('hidden:', type(exc).__name__)\n"
    )
    result = await _backend().run(["python3", "-c", code], workdir=workdir, timeout=120)
    assert "host-only" not in result.stdout
    assert "hidden:" in result.stdout


async def test_container_is_non_root_with_limits(tmp_path):
    """非 root、cap 全丢、内存与进程上限落到 cgroup 上。

    uid 断言的是「不是 0」而不是某个定值：后端默认用宿主 uid（这样才写得进挂载的
    workspace），值随机器变化，安全性在于非 root 而不是数字好看。
    """
    workdir = tmp_path / "ws"
    workdir.mkdir()
    code = (
        "import os\n"
        "print('uid', os.geteuid())\n"
        "for path in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/pids.max'):\n"
        "    try:\n"
        "        print(path, open(path).read().strip())\n"
        "    except OSError as exc:\n"
        "        print(path, 'unreadable', type(exc).__name__)\n"
    )
    result = await _backend().run(["python3", "-c", code], workdir=workdir, timeout=120)
    assert result.exit_code == 0, result.stderr
    uid_line = next(
        line for line in result.stdout.splitlines() if line.startswith("uid ")
    )
    assert uid_line.split()[1] != "0", uid_line
    assert "memory.max 268435456" in result.stdout, result.stdout  # 256 MiB
    assert "pids.max 64" in result.stdout, result.stdout

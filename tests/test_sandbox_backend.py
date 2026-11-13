"""C2：可插拔执行后端（local 行为不变 / docker 真隔离 / 缺失即报错）。

这一组用例钉的是「边界」而不是「实现细节」：

- ``local`` 后端与 C2 之前**逐字节一致**：同一段代码、同一份输出格式、同一套
  环境变量裁剪、同一个「超时即杀」语义；
- ``docker`` 后端的隔离参数**一个不少**（禁网 / 非 root / cap 全丢 / 资源上限 /
  只挂工作区），因为这些参数漏一个，就等于把「有沙箱」变成一句谎话；
- 选了 docker 却没有 docker 时**构造期就报错**（fail loud），不静默退回 local。

真正跑容器需要宿主机有 Docker，本机/CI 未必有 —— 因此 docker 侧断言的是
**拼出来的 argv** 与**可用性判定**，那是这一层唯一能确定的部分。
"""

from __future__ import annotations

import sys

import pytest

from agentcrew.tools.builtins import RunPythonTool
from agentcrew.tools.sandbox import (
    CONTAINER_WORKDIR,
    DockerBackend,
    ExecutionResult,
    LocalBackend,
    SandboxError,
    build_backend,
    default_container_user,
    docker_available,
)

# ── local：行为不变 ────────────────────────────────────────────────────

async def test_local_backend_runs_python(tmp_path):
    result = await LocalBackend().run_python(
        "print(1 + 1)", workdir=tmp_path, timeout=10)
    assert result.ok
    assert result.stdout.strip() == "2"
    assert result.exit_code == 0
    assert result.backend == "local"


async def test_local_backend_trims_env(tmp_path, monkeypatch):
    """P1 的裁剪口径不能被重构放宽：非白名单变量在子进程里必须不存在。"""
    monkeypatch.setenv("AGENTCREW_SECRET_FOR_TEST", "leak-me")
    result = await LocalBackend().run_python(
        "import os; print(os.environ.get('AGENTCREW_SECRET_FOR_TEST'))",
        workdir=tmp_path, timeout=10)
    assert result.stdout.strip() == "None"


async def test_local_backend_timeout_kills_process(tmp_path):
    result = await LocalBackend().run_python(
        "import time; time.sleep(5)", workdir=tmp_path, timeout=0.3)
    assert result.timed_out is True
    assert result.exit_code == -1


async def test_local_backend_reports_missing_command(tmp_path):
    result = await LocalBackend().run(
        ["definitely-not-a-real-binary-12345"], workdir=tmp_path, timeout=5)
    assert result.exit_code == -1
    assert "无法启动子进程" in result.stderr


async def test_run_python_tool_output_shape_unchanged(tmp_path):
    """``RunPythonTool`` 的回灌格式是 Agent 与测试都依赖的对外形状。"""
    tool = RunPythonTool(workdir=tmp_path)
    text = await tool.run(code="print('hi')")
    assert text == "stdout:\nhi\nexit_code=0"


async def test_run_python_tool_uses_injected_backend(tmp_path):
    class _Fake(LocalBackend):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run_python(self, code, *, workdir, timeout):  # noqa: ANN001
            self.calls.append(code)
            return ExecutionResult(exit_code=0, stdout="fake\n", backend="fake")

    fake = _Fake()
    tool = RunPythonTool(workdir=tmp_path, backend=fake)
    text = await tool.run(code="print('x')")
    assert fake.calls == ["print('x')"]
    assert "fake" in text


async def test_local_backend_retries_once_on_startup_crash(tmp_path):
    """解释器启动即崩（零输出）重试一次；真实失败不重试（否则掩盖问题）。"""

    class _Flaky(LocalBackend):
        def __init__(self) -> None:
            self.calls = 0

        async def _run_once(self, argv, *, workdir, timeout):  # noqa: ANN001
            self.calls += 1
            if self.calls == 1:
                return ExecutionResult(exit_code=0xC0000374, backend="local")
            return ExecutionResult(exit_code=0, stdout="ok", backend="local")

    flaky = _Flaky()
    result = await flaky.run(
        [sys.executable, "-c", "print(1)"], workdir=tmp_path, timeout=5)
    assert flaky.calls == 2
    assert result.exit_code == 0

    class _Failing(LocalBackend):
        def __init__(self) -> None:
            self.calls = 0

        async def _run_once(self, argv, *, workdir, timeout):  # noqa: ANN001
            self.calls += 1
            return ExecutionResult(exit_code=1, stdout="boom", backend="local")

    failing = _Failing()
    result = await failing.run(["x"], workdir=tmp_path, timeout=5)
    assert failing.calls == 1
    assert result.exit_code == 1


# ── docker：隔离参数 ──────────────────────────────────────────────────

def test_docker_argv_has_every_isolation_flag(tmp_path):
    backend = DockerBackend(image="agentcrew-sandbox:test")
    argv = backend.build_argv(
        ["python3", "-c", "print(1)"], workdir=tmp_path, name="c1")
    joined = " ".join(argv)
    assert argv[:3] == [backend.binary, "run", "--rm"]
    # 禁网：默认 --network none
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    # 非 root + 不提权。uid 具体值随宿主变化（POSIX 上传宿主 uid 才能写挂载的
    # workspace，Windows 上退回 nobody），断言的是「不是 root」这个性质。
    user = argv[argv.index("--user") + 1]
    assert user.split(":", 1)[0] not in {"0", "root"}, user
    assert ":" in user, user
    assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in joined
    # 只读根 + /tmp 可写
    assert "--read-only" in argv
    assert any(item.startswith("/tmp:rw") for item in argv)
    # 资源上限
    assert argv[argv.index("--memory") + 1] == "512m"
    assert argv[argv.index("--cpus") + 1] == "1"
    assert argv[argv.index("--pids-limit") + 1] == "256"
    # 只挂工作区
    mount = argv[argv.index("-v") + 1]
    assert mount.endswith(f":{CONTAINER_WORKDIR}:rw")
    assert str(tmp_path.resolve()).replace("\\", "/") in mount
    assert argv[argv.index("-w") + 1] == CONTAINER_WORKDIR
    # 镜像与容器内命令
    assert argv[argv.index("agentcrew-sandbox:test") + 1] == "python3"


def test_docker_argv_network_can_be_enabled_explicitly(tmp_path):
    backend = DockerBackend(allow_network=True)
    argv = backend.build_argv(["true"], workdir=tmp_path, name="c2")
    assert argv[argv.index("--network") + 1] == "bridge"


def test_docker_backend_rejects_bad_limits():
    with pytest.raises(SandboxError):
        DockerBackend(image="")
    with pytest.raises(SandboxError):
        DockerBackend(memory_mb=32)
    with pytest.raises(SandboxError):
        DockerBackend(cpus=0)


def test_docker_backend_refuses_root_user():
    """``--user 0`` 会让沙箱形同虚设，构造期就拒（fail loud）。"""
    with pytest.raises(SandboxError):
        DockerBackend(user="0:0")
    with pytest.raises(SandboxError):
        DockerBackend(user="root:root")


def test_docker_backend_user_defaults_to_host_uid(tmp_path):
    """默认 uid 必须跟宿主一致，否则容器写不进挂进来的 workspace（真容器实测）。

    Windows 没有 uid 概念，退回 nobody —— 两种结果都非 root。
    """
    backend = DockerBackend()
    assert backend.user == default_container_user()
    assert backend.user.split(":", 1)[0] != "0"
    argv = backend.build_argv(["true"], workdir=tmp_path, name="c3")
    assert argv[argv.index("--user") + 1] == backend.user


def test_docker_backend_python_is_container_interpreter():
    assert DockerBackend().python == "python3"


async def test_docker_backend_without_binary_fails_loud(tmp_path):
    backend = DockerBackend(which=lambda _name: "/usr/bin/docker")
    backend.binary = "definitely-not-a-real-docker-12345"
    with pytest.raises(SandboxError):
        await backend.run(["python3", "-c", "print(1)"], workdir=tmp_path,
                          timeout=5)


# ── 工厂：选型与失败模式 ──────────────────────────────────────────────

def test_build_backend_local_by_default():
    assert isinstance(build_backend("local"), LocalBackend)
    assert isinstance(build_backend(""), LocalBackend)


def test_build_backend_unknown_kind_raises():
    with pytest.raises(SandboxError):
        build_backend("firecracker")


def test_build_backend_docker_without_docker_raises():
    with pytest.raises(SandboxError) as excinfo:
        build_backend("docker", which=lambda _name: None)
    assert "docker" in str(excinfo.value)


def test_docker_available_uses_injected_which():
    assert docker_available(lambda name: "/usr/bin/docker") is True
    assert docker_available(lambda name: None) is False


def test_local_python_is_current_interpreter():
    assert LocalBackend().python == sys.executable

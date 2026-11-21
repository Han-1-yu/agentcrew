"""执行体档案的存储与服务层（R69）。

覆盖四层：
1. 解析：单档案 / 映射 / ``agents.profiles`` 包装 / 非法字段与 key 不一致；
2. 存储：原子写、同名保护、覆盖备份、删除回收站、路径安全；
3. 加载：``agents.d`` 覆盖 ``agentcrew.yaml``、坏文件 fail-loud；
4. 服务：保存前绑定校验、诊断分级、跨 run 用量聚合。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agentcrew.agents.plan import resolve_plan
from agentcrew.config import Settings, load_yaml_config
from agentcrew.profiles.service import (
    delete_profile,
    diagnose,
    export_yaml,
    profile_payload,
    save_profiles,
    usage_report,
)
from agentcrew.profiles.store import (
    ProfileStoreError,
    delete_profile_file,
    load_profiles_dir,
    parse_profile_document,
    save_profile_file,
    scan_profiles_dir,
)
from agentcrew.roles.binding import AgentProfile, BindingError
from agentcrew.roles.catalog import RoleCatalog
from agentcrew.roles.spec import RoleSpec

ROOT = Path(__file__).resolve().parents[1]


def _spec(key: str = "backend_dev", *, tools: list[str] | None = None) -> RoleSpec:
    return RoleSpec(
        key=key, title="后端开发工程师", goal="写 API", backstory="老兵",
        responsibilities=["产出代码"], boundaries=["不改前端"],
        output_contract="按契约输出文件", system_prompt="你是后端工程师",
        allowed_tools=tools if tools is not None else ["*"],
    )


def _settings(tmp_path: Path, *, profiles: dict | None = None,
              bindings: dict | None = None) -> Settings:
    payload: dict = {"config_version": 1}
    if profiles:
        payload["agents"] = {"profiles": profiles}
    if bindings:
        payload["roles"] = bindings
    import yaml
    path = tmp_path / "agentcrew.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return _load(tmp_path)


def _load(tmp_path: Path) -> Settings:
    values = load_yaml_config(tmp_path / "agentcrew.yaml")
    values["config_path"] = (tmp_path / "agentcrew.yaml").resolve()
    values["profiles_dir"] = str(tmp_path / "agents.d")
    return Settings.model_validate(values)


# ── 1. 解析 ────────────────────────────────────────────────────────────────

def test_parse_single_profile_with_key():
    profiles = parse_profile_document(
        "key: fast-writer\ntitle: 快速写手\nmodel: deepseek:deepseek-chat\n")
    assert [p.key for p in profiles] == ["fast-writer"]
    assert profiles[0].model == "deepseek:deepseek-chat"


def test_parse_map_and_wrapped_section():
    text = """
agents:
  profiles:
    writer:
      description: 写文档
    reviewer:
      tools: [read_file]
"""
    profiles = parse_profile_document(text)
    assert {p.key for p in profiles} == {"writer", "reviewer"}
    assert next(p for p in profiles if p.key == "reviewer").tools == ["read_file"]


def test_parse_allows_empty_profile_short_form():
    profiles = parse_profile_document("writer:\n")
    assert profiles[0].key == "writer"
    assert profiles[0].tools is None


def test_parse_requires_key_for_single_profile_shape():
    with pytest.raises(ProfileStoreError, match="缺少 key"):
        parse_profile_document("title: 快速写手\nmodel: deepseek:deepseek-chat\n")


def test_parse_rejects_key_mismatch():
    with pytest.raises(ProfileStoreError, match="与文件名 / 映射键"):
        parse_profile_document("writer:\n  key: other\n  title: 不一致\n")


def test_parse_rejects_typo_field_with_location():
    with pytest.raises(ProfileStoreError) as exc:
        parse_profile_document("writer:\n  tittle: 拼错的字段\n")
    assert "writer" in str(exc.value)
    assert "Extra inputs" in str(exc.value)


def test_parse_rejects_bad_key_and_empty_document():
    with pytest.raises(ProfileStoreError, match="key 不合法"):
        parse_profile_document("BadKey:\n  title: x\n")
    with pytest.raises(ProfileStoreError, match="顶层必须是映射"):
        parse_profile_document("[]\n")
    with pytest.raises(ProfileStoreError, match="为空，没有可保存"):
        parse_profile_document("")


# ── 2. 存储 ────────────────────────────────────────────────────────────────

def test_save_and_reload_roundtrip(tmp_path: Path):
    profile = AgentProfile(
        key="fast-writer", title="快速写手", description="短文档",
        capabilities=["text"], model="deepseek:deepseek-chat",
        cost_ceiling_usd=0.05)
    path = save_profile_file(tmp_path, profile)
    assert path.name == "fast-writer.yaml"
    assert "# AgentCrew 执行体档案" in path.read_text(encoding="utf-8")
    loaded = load_profiles_dir(tmp_path)
    assert loaded["fast-writer"].title == "快速写手"
    assert loaded["fast-writer"].cost_ceiling_usd == 0.05


def test_save_refuses_silent_overwrite_and_backs_up(tmp_path: Path):
    first = AgentProfile(key="writer", title="第一版")
    save_profile_file(tmp_path, first)
    with pytest.raises(ProfileStoreError, match="已存在"):
        save_profile_file(tmp_path, AgentProfile(key="writer", title="第二版"))

    save_profile_file(
        tmp_path, AgentProfile(key="writer", title="第二版"), overwrite=True)
    assert load_profiles_dir(tmp_path)["writer"].title == "第二版"
    backup = tmp_path / "writer.yaml.bak"
    assert backup.is_file()
    assert "第一版" in backup.read_text(encoding="utf-8")


def test_delete_moves_file_to_trash(tmp_path: Path):
    save_profile_file(tmp_path, AgentProfile(key="writer"))
    trash = delete_profile_file(tmp_path, "writer")
    assert trash.is_file()
    assert trash.parent.name == ".trash"
    assert not (tmp_path / "writer.yaml").exists()
    with pytest.raises(ProfileStoreError, match="没有档案"):
        delete_profile_file(tmp_path, "writer")


def test_path_traversal_key_is_rejected(tmp_path: Path):
    with pytest.raises(ProfileStoreError, match="key 不合法"):
        save_profile_file(tmp_path, AgentProfile(key="../evil"))
    assert not (tmp_path.parent / "evil.yaml").exists()


def test_scan_reports_bad_file_without_hiding_good_ones(tmp_path: Path):
    save_profile_file(tmp_path, AgentProfile(key="writer"))
    (tmp_path / "broken.yaml").write_text("key: broken\ntittle: x\n", encoding="utf-8")
    entries, issues = scan_profiles_dir(tmp_path)
    assert set(entries) == {"writer"}
    assert len(issues) == 1
    assert "Extra inputs" in issues[0].message
    with pytest.raises(ProfileStoreError, match="无法加载"):
        load_profiles_dir(tmp_path)


# ── 3. 配置加载与优先级 ────────────────────────────────────────────────────

def test_agents_dir_overrides_yaml_and_records_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config = tmp_path / "agentcrew.yaml"
    config.write_text(
        "agents:\n  profiles:\n    writer:\n      title: YAML 版\n"
        "roles:\n  backend_dev:\n    agents:\n      - ref: writer\n",
        encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(config))
    directory = tmp_path / "agents.d"
    save_profile_file(
        directory, AgentProfile(key="writer", title="文件版"), overwrite=True)

    settings = Settings.load()
    assert settings.agent_profiles["writer"].title == "文件版"
    assert settings.profiles_dir == directory.resolve()

    catalog = RoleCatalog([_spec()])
    payload = profile_payload(settings, catalog)
    item = payload["profiles"][0]
    assert item["source"] == "agents.d"
    assert item["shadowed"] is True
    assert item["used_by"] == ["backend_dev"]

    report = diagnose(settings, catalog)
    assert report["summary"]["errors"] == 0
    assert any(item["code"] == "profile.shadowed" for item in report["warnings"])


def test_broken_bound_profile_names_the_file_at_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config = tmp_path / "agentcrew.yaml"
    config.write_text(
        "config_version: 1\nroles:\n  backend_dev:\n    agents:\n"
        "      - ref: broken\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(config))
    directory = tmp_path / "agents.d"
    directory.mkdir()
    (directory / "broken.yaml").write_text("key: broken\ntittle: x\n",
                                           encoding="utf-8")

    settings = Settings.load()
    with pytest.raises(BindingError, match="加载失败.*broken.yaml"):
        resolve_plan(settings, RoleCatalog([_spec()]), "R")


def test_broken_profile_file_is_reported_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    config = tmp_path / "agentcrew.yaml"
    config.write_text("config_version: 1\n", encoding="utf-8")
    monkeypatch.setenv("AGENTCREW_CONFIG", str(config))
    directory = tmp_path / "agents.d"
    directory.mkdir()
    (directory / "bad.yaml").write_text("key: bad\nnope: 1\n", encoding="utf-8")
    save_profile_file(directory, AgentProfile(key="good", title="可用"))
    settings = Settings.load()

    # 好档案照常生效；坏文件被记录下来，不阻断整个配置
    assert set(settings.agent_profiles) == {"good"}
    assert len(settings.profile_errors) == 1
    assert settings.profile_errors[0]["key"] == "bad"

    report = diagnose(settings, RoleCatalog([_spec()]))
    assert report["summary"]["errors"] == 1
    assert report["errors"][0]["code"] == "store.invalid_file"
    assert "bad.yaml" in report["errors"][0]["message"]


# ── 4. 服务层 ──────────────────────────────────────────────────────────────

def test_save_refuses_change_that_breaks_binding(tmp_path: Path):
    settings = _settings(
        tmp_path,
        profiles={"narrow": {"tools": ["read_file"]}},
        bindings={"backend_dev": {"agents": [{"ref": "narrow"}]}})
    catalog = RoleCatalog([_spec(tools=["read_file"])])
    # 先确认基线可启动
    save_profiles(
        "narrow:\n  tools: [read_file]\n", settings, catalog,
        force=True, dry_run=True)

    with pytest.raises(ProfileStoreError, match="无法启动"):
        save_profiles(
            "narrow:\n  tools: [read_file, run_python]\n", settings, catalog,
            force=True, dry_run=True)


def test_save_rejects_unparseable_model_alias(tmp_path: Path):
    settings = _settings(
        tmp_path,
        profiles={"writer": {"title": "写手"}},
        bindings={"backend_dev": {"agents": [{"ref": "writer"}]}})
    catalog = RoleCatalog([_spec()])
    with pytest.raises(ProfileStoreError, match="模型别名无法解析"):
        save_profiles(
            "writer:\n  model: deepseek-chat\n", settings, catalog,
            force=True, dry_run=True)


def test_save_is_all_or_nothing_and_reports_conflicts(tmp_path: Path):
    settings = _settings(tmp_path)
    catalog = RoleCatalog([_spec()])
    directory = Path(settings.profiles_dir)
    save_profile_file(directory, AgentProfile(key="writer"))

    with pytest.raises(ProfileStoreError, match="已存在"):
        save_profiles(
            "writer:\n  title: 覆盖\nreviewer:\n  title: 复核\n",
            settings, catalog)
    assert not (directory / "reviewer.yaml").exists()

    result = save_profiles(
        "writer:\n  title: 覆盖\nreviewer:\n  title: 复核\n",
        settings, catalog, force=True)
    assert result["updated"] == ["writer"]
    assert result["created"] == ["reviewer"]
    assert set(load_profiles_dir(directory)) == {"writer", "reviewer"}


def test_batch_save_rolls_back_when_a_write_fails(tmp_path: Path, monkeypatch):
    from agentcrew.profiles import service as profile_service

    settings = _settings(tmp_path)
    catalog = RoleCatalog([_spec()])
    directory = Path(settings.profiles_dir)
    real_save = profile_service.save_profile_file

    def flaky(directory, profile, *, overwrite=False):
        if profile.key == "second":
            raise OSError("disk full")
        return real_save(directory, profile, overwrite=overwrite)

    monkeypatch.setattr(profile_service, "save_profile_file", flaky)
    with pytest.raises(ProfileStoreError, match="已回滚"):
        save_profiles(
            "first:\n  title: A\nsecond:\n  title: B\n", settings, catalog)
    assert not (directory / "first.yaml").exists()


def test_delete_refuses_while_bound_then_allows_force(tmp_path: Path):
    settings = _settings(
        tmp_path,
        profiles={"writer": {"title": "写手"}},
        bindings={"backend_dev": {"agents": [{"ref": "writer"}]}})
    catalog = RoleCatalog([_spec()])
    directory = Path(settings.profiles_dir)
    save_profile_file(directory, AgentProfile(key="writer"))
    settings = _load(tmp_path)

    with pytest.raises(ProfileStoreError, match="仍被角色绑定"):
        delete_profile("writer", settings, catalog)
    result = delete_profile("writer", settings, catalog, force=True)
    assert result["bound_roles"] == ["backend_dev"]
    assert Path(result["trash"]).is_file()
    # agents.d 删掉后，agentcrew.yaml 里的同名定义重新生效
    assert result["revealed"] is True
    assert _load(tmp_path).agent_profiles["writer"].title == "写手"


def test_delete_yaml_only_profile_is_refused(tmp_path: Path):
    settings = _settings(tmp_path, profiles={"writer": {"title": "YAML"}})
    with pytest.raises(ProfileStoreError, match="定义在 agentcrew.yaml"):
        delete_profile("writer", settings, RoleCatalog([_spec()]))


def test_export_single_profile_does_not_carry_bindings(tmp_path: Path):
    settings = _settings(
        tmp_path,
        profiles={"writer": {"title": "写手"}},
        bindings={"backend_dev": {"agents": [{"ref": "writer"}]}})
    text = export_yaml(settings, keys=["writer"])
    assert "agents:" in text and "profiles:" in text
    assert "roles:" not in text


def test_usage_report_aggregates_per_profile(tmp_path: Path):
    for index, profile in enumerate(["writer", "writer", "default"]):
        run = tmp_path / f"2026112{index}-000000-aaaa"
        run.mkdir()
        events = [
            {"ts": f"2026-11-2{index} 10:00:00+00:00", "run_id": run.name,
             "event": "llm_call",
             "data": {"profile": profile, "role": "backend_dev",
                      "model": "deepseek-chat", "prompt_tokens": 10,
                      "completion_tokens": 5, "estimated_cost_usd": 0.01,
                      "latency_ms": 100}},
            {"ts": "2026-11-20 10:00:01+00:00", "run_id": run.name,
             "event": "task_started", "data": {}},
        ]
        (run / "ledger.jsonl").write_text(
            "\n".join(json.dumps(item) for item in events), encoding="utf-8")

    report = usage_report(tmp_path)
    writer = report["profiles"]["writer"]
    assert writer["calls"] == 2
    assert writer["runs"] == 2
    assert writer["cost_usd"] == 0.02
    assert writer["total_tokens"] == 30
    assert report["profiles"]["default"]["calls"] == 1
    assert report["truncated"] is False


# ── 5. CLI 端到端 ─────────────────────────────────────────────────────────

def _run_cli(args: list[str], config: Path, **kwargs) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["AGENTCREW_CONFIG"] = str(config)
    env.pop("AGENTCREW_OUTPUT_DIR", None)
    return subprocess.run(
        [sys.executable, str(ROOT / "main.py"), *args],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", timeout=120, **kwargs)


def test_cli_save_check_usage_delete(tmp_path: Path):
    config = tmp_path / "agentcrew.yaml"
    config.write_text("config_version: 1\n", encoding="utf-8")
    saved = _run_cli(
        ["agents", "--save", "-", "--json"],
        config,
        input=json.dumps({
            "key": "fast-writer", "title": "快速写手",
            "description": "短文档", "model": "deepseek:deepseek-chat"}))
    assert saved.returncode == 0, saved.stderr
    payload = json.loads(saved.stdout)
    assert payload["saved"] == ["fast-writer"]
    assert (tmp_path / "agents.d" / "fast-writer.yaml").is_file()

    listed = _run_cli(["agents", "--json"], config)
    assert listed.returncode == 0, listed.stderr
    data = json.loads(listed.stdout)
    assert data["profiles"][0]["source"] == "agents.d"
    assert data["storage"]["profiles_dir"].endswith("agents.d")

    checked = _run_cli(["agents", "--check", "--json"], config)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["summary"]["errors"] == 0

    removed = _run_cli(["agents", "--delete", "fast-writer", "--json"], config)
    assert removed.returncode == 0, removed.stderr
    assert Path(json.loads(removed.stdout)["trash"]).is_file()

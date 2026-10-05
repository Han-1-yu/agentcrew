"""角色目录测试：8 个内置角色的完整性、评审人映射与 YAML 加载（spec §8.2）。"""

import pytest
from pydantic import ValidationError

from agentcrew.roles.builtin import BUILTIN_ROLES
from agentcrew.roles.catalog import DuplicateRoleError, RoleCatalog, UnknownRoleError
from agentcrew.roles.spec import RoleSpec


def spec(key, reviewer_for=()):
    return RoleSpec(key=key, title=key, goal="g", backstory="b",
                    responsibilities=["r"], boundaries=["x"],
                    output_contract="o", system_prompt="p",
                    reviewer_for=list(reviewer_for))


# ── 内置角色完整性（S8）──
def test_exactly_eight_builtin_roles():
    assert len(BUILTIN_ROLES) == 8


def test_builtin_role_keys_match_spec():
    assert {r.key for r in BUILTIN_ROLES} == {
        "project_manager", "product_manager", "architect", "backend_dev",
        "frontend_dev", "qa_engineer", "devops_engineer", "technical_writer"}


@pytest.mark.parametrize("idx", range(8))
def test_every_builtin_role_is_fully_specified(idx):
    r = BUILTIN_ROLES[idx]
    assert r.title and r.goal and r.backstory
    assert r.responsibilities and r.boundaries and r.output_contract
    assert len(r.system_prompt) > 80          # 提示词必须有实质内容
    assert r.temperature >= 0 and r.max_output_tokens > 0


def test_builtin_roles_have_unique_keys():
    keys = [r.key for r in BUILTIN_ROLES]
    assert len(keys) == len(set(keys))


# ── 目录行为 ──
def test_register_and_get():
    c = RoleCatalog()
    c.register(spec("x"))
    assert c.get("x").key == "x" and c.has("x")


def test_duplicate_key_rejected():
    c = RoleCatalog([spec("x")])
    with pytest.raises(DuplicateRoleError):
        c.register(spec("x"))


def test_unknown_key_raises():
    with pytest.raises(UnknownRoleError):
        RoleCatalog().get("nope")


def test_reviewer_for_uses_declaration():
    c = RoleCatalog([spec("a", reviewer_for=["architect"]),
                     spec("architect", reviewer_for=["project_manager"]),
                     spec("project_manager", reviewer_for=["architect", "product_manager"]),
                     spec("product_manager", reviewer_for=["project_manager"])])
    assert c.reviewer_for("a") == ["architect"]


def test_reviewer_for_falls_back_to_project_manager():
    c = RoleCatalog([spec("lonely"), spec("project_manager", reviewer_for=["architect"]),
                     spec("architect", reviewer_for=["project_manager"])])
    assert c.reviewer_for("lonely") == ["project_manager"]


def test_reviewer_for_wildcard():
    c = RoleCatalog([spec("boss", reviewer_for=["*"]), spec("worker")])
    assert c.reviewer_for("worker") == ["boss"]


def test_no_role_is_its_own_reviewer():
    c = RoleCatalog(BUILTIN_ROLES)
    for r in BUILTIN_ROLES:
        assert r.key not in c.reviewer_for(r.key), f"{r.key} 自评审会死锁"


def test_all_reviewers_exist_in_catalog():
    c = RoleCatalog(BUILTIN_ROLES)
    for r in BUILTIN_ROLES:
        for rev in c.reviewer_for(r.key):
            assert c.has(rev), f"{r.key} 的评审人 {rev} 不在目录中"


def test_load_dir_reads_yaml(tmp_path):
    d = tmp_path / "roles.d"
    d.mkdir()
    (d / "wizard.yaml").write_text(
        "key: wizard\ntitle: 魔法师\ngoal: g\nbackstory: b\n"
        "responsibilities: [施法]\nboundaries: [不写代码]\n"
        "output_contract: o\nsystem_prompt: 你是一位魔法师。\n", encoding="utf-8")
    c = RoleCatalog()
    assert c.load_dir(d) == 1
    assert c.get("wizard").title == "魔法师"


def test_keys_and_all_preserve_registration_order():
    c = RoleCatalog([spec("b"), spec("a")])
    assert c.keys() == ["b", "a"]
    assert [r.key for r in c.all()] == ["b", "a"]


def test_reviewer_for_without_project_manager_returns_empty():
    c = RoleCatalog([spec("lonely")])
    assert c.reviewer_for("lonely") == []


def test_reviewer_for_drops_unregistered_reviewers():
    c = RoleCatalog([spec("x", reviewer_for=["ghost"])])
    assert c.reviewer_for("x") == []


def test_project_manager_is_not_its_own_fallback_reviewer():
    c = RoleCatalog([spec("project_manager")])
    assert c.reviewer_for("project_manager") == []


def test_load_dir_missing_path_returns_zero(tmp_path):
    assert RoleCatalog().load_dir(tmp_path / "nope") == 0


def test_load_dir_reports_invalid_yaml(tmp_path):
    d = tmp_path / "roles.d"
    d.mkdir()
    (d / "bad.yaml").write_text("key: bad\n", encoding="utf-8")
    c = RoleCatalog()
    with pytest.raises(ValidationError):
        c.load_dir(d)

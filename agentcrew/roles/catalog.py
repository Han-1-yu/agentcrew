"""角色目录：注册、查找与评审人解析（spec §8.2）。

目录是角色层的唯一入口：内置角色与 ``roles.d/`` 里的自定义角色都注册到这里，
调度器再通过 ``reviewer_for`` 决定每个任务由谁评审。注册顺序全程保持稳定，
因此全部查询结果都是确定的。
"""

from collections.abc import Sequence
from pathlib import Path

import yaml

from agentcrew.roles.spec import RoleSpec


class DuplicateRoleError(ValueError):
    """注册了已存在的 role key。"""


class UnknownRoleError(KeyError):
    """查询了未注册的 role key。"""


class RoleCatalog:
    """角色注册表：一个 role key 对应唯一 ``RoleSpec``。

    ``keys()``/``all()`` 按注册顺序返回；``reviewer_for`` 的结果顺序同样确定。
    """

    def __init__(self, specs: Sequence[RoleSpec] = ()) -> None:
        self._specs: dict[str, RoleSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: RoleSpec) -> None:
        """注册一个角色；key 已存在时抛 ``DuplicateRoleError``。"""
        if spec.key in self._specs:
            raise DuplicateRoleError(f"角色 key 重复: {spec.key!r}")
        self._specs[spec.key] = spec

    def get(self, key: str) -> RoleSpec:
        """按 key 取角色；缺失时抛 ``UnknownRoleError``。"""
        try:
            return self._specs[key]
        except KeyError:
            raise UnknownRoleError(f"未注册的角色 key: {key!r}") from None

    def has(self, key: str) -> bool:
        """key 是否已注册；从不抛异常。"""
        return key in self._specs

    def keys(self) -> list[str]:
        """全部已注册 key，按注册顺序。"""
        return list(self._specs)

    def all(self) -> list[RoleSpec]:
        """全部已注册角色，按注册顺序。"""
        return list(self._specs.values())

    def reviewer_for(self, role_key: str) -> list[str]:
        """返回应评审 ``role_key`` 的全部角色 key。

        收集分两步：先取 ``role_key`` 自身声明的评审人（``RoleSpec.reviewer_for``，
        保持声明顺序），再追加所有声明 ``["*"]`` 的通用评审人（保持注册顺序）；
        去重后依次过滤掉 ``role_key`` 自身（角色绝不评审自己的产出，否则会死锁）
        与目录中不存在的 key。

        没有任何评审人（含未声明的自定义角色）时回落到 ``project_manager``；
        它在目录中也不存在、或过滤后为空时返回 ``[]``——调用方据此跳过 G2，
        直接由 G1 决定，这不是错误。未注册的 ``role_key`` 同样不抛错，按“无声明
        评审人”处理。
        """
        spec = self._specs.get(role_key)
        declared = list(spec.reviewer_for) if spec is not None else []
        wildcard_reviewers = [
            r.key for r in self._specs.values() if "*" in r.reviewer_for
        ]
        candidates = declared + wildcard_reviewers
        if not candidates:
            candidates = ["project_manager"]  # 回落；未注册会在下面被过滤掉

        reviewers: list[str] = []
        for key in candidates:
            if key == role_key or not self.has(key) or key in reviewers:
                continue
            reviewers.append(key)
        return reviewers

    def load_dir(self, path: Path) -> int:
        """从目录加载 YAML 自定义角色，返回加载条数。

        ``path`` 不存在（或不是目录）时返回 0——该目录是可选的。读取 ``*.yaml``
        与 ``*.yml``，逐个 ``yaml.safe_load`` 后校验为 ``RoleSpec`` 并注册；
        任一文件解析失败或字段非法都直接抛错，绝不静默跳过。
        """
        if not path.is_dir():
            return 0
        count = 0
        for file in sorted((*path.glob("*.yaml"), *path.glob("*.yml"))):
            data = yaml.safe_load(file.read_text(encoding="utf-8"))
            self.register(RoleSpec.model_validate(data))
            count += 1
        return count

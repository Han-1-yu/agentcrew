"""跑评测：一次 case = 一次完整 run（P2-5）。

用 ``Crew`` 直接在进程内跑，而不是起 ``main.py run`` 子进程：进程内能拿到结构化
的 run 记录、产物与 ledger，不用二次解析终端输出；而且离线 mock 下一条 case 是
秒级，几十条 case 也不会把 CI 拖成十几分钟。

隔离：每条 case 用**自己的临时 output 目录**。共享目录会让「上一条 case 的产物」
出现在下一条的 artifacts 里，断言随之失真——这类污染极难排查。
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcrew.config import Settings
from agentcrew.crew.crew import Crew
from agentcrew.evals.case import CaseOutput, CaseResult, EvalCase
from agentcrew.evals.checks import evaluate_checks
from agentcrew.runtime.checkpoint import CheckpointStore
from agentcrew.runtime.ledger import Ledger

__all__ = [
    "EvalReport",
    "MatrixReport",
    "VariantResult",
    "apply_overrides",
    "compute_pass_pow_k",
    "load_variant_file",
    "run_case",
    "run_dataset",
    "run_matrix",
]

#: 只断言文本产物；超过这个大小的产物不读全文（评测不该把内存吃穿）。
_MAX_ARTIFACT_BYTES = 512 * 1024


def _readartifacts(root: Path) -> dict[str, str]:
    """读回产物全文（相对路径 → 文本）；二进制与超大文件跳过。"""
    if not root.is_dir():
        return {}
    collected: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_ARTIFACT_BYTES:
                continue
            collected[path.relative_to(root).as_posix()] = path.read_text(
                encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return collected


async def run_case(
    case: EvalCase, *,
    base_settings: Settings | None = None,
    output_root: Path | None = None,
) -> CaseOutput:
    """跑一条 case 并返回可断言快照（不抛异常：失败收敛进 ``CaseOutput.error``）。"""
    root = Path(output_root) if output_root else Path(
        tempfile.mkdtemp(prefix="agentcrew-eval-"))
    try:
        overrides: dict[str, Any] = {
            "output_dir": root,
            # 评测要的是「一次跑到底的确定性结果」：mock + 不卡门禁。
            "mock": True,
            "gate": "none",
        }
        overrides.update(case.options or {})
        settings = (base_settings or Settings.load()).model_copy(
            update=overrides)
        goal = case.goal
        if case.sample:
            # 样例夹具通过全局注入点切换（与 CLI ``--sample`` 同一条路径）。
            # **必须还原**：这个注入点是进程级的，跑完 eval 还留着上一条 case 的
            # 夹具，会污染同进程里后续的 run（测试套件里表现为「某条无关用例
            # 突然失败，单独跑又能过」——最难查的那种）。
            from agentcrew.models import factory as model_factory
            from examples.samples import activate

            saved = model_factory.DEFAULT_FIXTURES_DIR
            try:
                spec = activate(case.sample)
                goal = goal or spec.goal
                return await _run_crew(settings, goal, root)
            finally:
                model_factory.DEFAULT_FIXTURES_DIR = saved
        # 走到这里说明 case.sample 为空，而 load_dataset 已保证 sample 与 goal
        # 至少给一个，故 goal 必非 None（此处仅作类型收窄，不会触发）。
        assert goal is not None
        return await _run_crew(settings, goal, root)
    except Exception as exc:  # noqa: BLE001 —— 评测要如实记录失败，不是中断整批
        return CaseOutput(error=f"{type(exc).__name__}: {exc}"[:500])
    finally:
        if output_root is None:
            shutil.rmtree(root, ignore_errors=True)


async def _run_crew(settings: Settings, goal: str, root: Path) -> CaseOutput:
    """真跑一次 run 并把盘上的结果读成 :class:`CaseOutput`。"""
    record = await Crew(settings).run(goal)
    run_dir = root / record.run_id
    board = CheckpointStore(run_dir).load_board()
    counts: dict[str, int] = {}
    for event in Ledger(run_dir / "ledger.jsonl", run_id=record.run_id).events():
        counts[event.event] = counts.get(event.event, 0) + 1
    return CaseOutput(
        status=record.status.value,
        run_id=record.run_id,
        artifacts=_readartifacts(run_dir / "artifacts"),
        task_status={task.id: task.status.value for task in board.all()},
        event_counts=counts,
        cost_usd=float(getattr(record.stats, "estimated_cost_usd", 0.0) or 0.0),
    )


@dataclass
class EvalReport:
    """一次评测的汇总：逐 case 的 k 次结果 + ``pass^k`` 指标。"""

    results: list[CaseResult] = field(default_factory=list)
    repeat: int = 1

    def by_case(self) -> dict[str, list[CaseResult]]:
        grouped: dict[str, list[CaseResult]] = {}
        for result in self.results:
            grouped.setdefault(result.case_id, []).append(result)
        return grouped

    @property
    def pass_at_1(self) -> float:
        """单次通过率：所有运行里通过的比例（k>1 时也不含糊）。"""
        return (sum(1 for item in self.results if item.ok) / len(self.results)
                if self.results else 0.0)

    @property
    def pass_pow_k(self) -> float:
        """``pass^k``：k 次**全过**的 case 占比（k=1 时与 pass@1 相同）。"""
        grouped = self.by_case()
        if not grouped:
            return 0.0
        strict = sum(
            1 for runs in grouped.values()
            if len(runs) >= self.repeat and all(run.ok for run in runs))
        return strict / len(grouped)

    @property
    def strict_cases(self) -> list[str]:
        """k 次全过的 case id（按首次出现顺序）。"""
        return [
            case_id for case_id, runs in self.by_case().items()
            if len(runs) >= self.repeat and all(run.ok for run in runs)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "repeat": self.repeat,
            "cases": len(self.by_case()),
            "runs": len(self.results),
            "pass_at_1": round(self.pass_at_1, 4),
            "pass_pow_k": round(self.pass_pow_k, 4),
            "strict_cases": self.strict_cases,
            "results": [result.model_dump(mode="json") for result in self.results],
        }


def compute_pass_pow_k(results: Sequence[CaseResult], repeat: int) -> float:
    """独立函数版本的 ``pass^k``（测试与外部脚本直接可用）。"""
    if not results or repeat < 1:
        return 0.0
    grouped: dict[str, list[CaseResult]] = {}
    for result in results:
        grouped.setdefault(result.case_id, []).append(result)
    strict = sum(
        1 for runs in grouped.values()
        if len(runs) >= repeat and all(run.ok for run in runs))
    return strict / len(grouped)


async def run_dataset(
    cases: Sequence[EvalCase], *,
    repeat: int = 1,
    base_settings: Settings | None = None,
    output_dir: Path | None = None,
) -> EvalReport:
    """跑完整个数据集：每条 case 跑 ``repeat`` 次（每次都独立目录）。

    ``output_dir`` 给了就保留每次 run 的目录（``<output_dir>/<case>-<k>/``）便于
    事后翻账；不给则用临时目录并在跑完后清理。
    """
    repeats = max(1, int(repeat))
    report = EvalReport(repeat=repeats)
    for case in cases:
        for index in range(repeats):
            root = None
            if output_dir is not None:
                root = Path(output_dir) / f"{case.id}-{index + 1}"
                root.mkdir(parents=True, exist_ok=True)
            output = await run_case(
                case, base_settings=base_settings, output_root=root)
            checks = evaluate_checks(case.checks, output)
            report.results.append(CaseResult(
                case_id=case.id,
                ok=all(item.ok for item in checks),
                checks=checks,
                status=output.status,
                error=output.error,
                cost_usd=output.cost_usd,
                llm_calls=output.event_counts.get("llm_call", 0),
                revise_rounds=output.event_counts.get("agent_revise", 0),
            ))
    return report


# ── R45：绑定配置 A/B 矩阵 ────────────────────────────────────────────────


def load_variant_file(path: Path | str) -> dict[str, Any]:
    """读一份「配置变体」文件（YAML 或 JSON，JSON 是 YAML 的子集）。

    变体文件与 ``agentcrew.yaml`` **同形**（``agents.profiles`` / ``roles`` 等），
    只写要覆盖的那一段——其余字段沿用基线配置。否则每份变体都要抄一遍全量配置，
    改一处要改 N 份。

    归一化复用 :func:`agentcrew.config.load_yaml_config`，保证变体与主配置走
    同一套规则（含 profile key 自动回填）；格式错误一律收敛成 ``ValueError``。
    """
    from agentcrew.config import load_yaml_config

    file_path = Path(path)
    if not file_path.is_file():
        raise ValueError(f"变体文件不存在：{file_path}")
    # strict：拼错的段名必须当场报错，否则变体静默失效、A/B 结论是假的。
    return dict(load_yaml_config(file_path, strict=True))


def apply_overrides(settings: Settings, overrides: dict[str, Any]) -> Settings:
    """把变体覆盖到 ``Settings`` 上，并在**起跑前**完整校验一次。

    两道关：

    1. 未知字段直接拒绝——``model_copy(update=...)`` 会静默加属性，跑完才发现
       配置根本没生效（本项目历史上踩过这类"死字段"）；
    2. 走 ``model_validate`` 全量重建，让类型/边界（``ge`` / ``le``）与嵌套模型
       （``AgentProfile`` / ``RoleAgents``）的校验真正执行。
    """
    unknown = sorted(key for key in overrides if key not in type(settings).model_fields)
    if unknown:
        raise ValueError(
            f"未知配置项：{', '.join(unknown)}"
            f"（变体文件应与 agentcrew.yaml 同形：agents / roles / models …）")
    payload = settings.model_dump(mode="json")
    payload.update(dict(overrides))
    return type(settings).model_validate(payload)


@dataclass
class VariantResult:
    """一个配置变体跑完整数据集的结果。"""

    name: str
    overrides: dict[str, Any]
    report: EvalReport

    @property
    def metrics(self) -> dict[str, Any]:
        results = self.report.results
        return {
            "cases": len(self.report.by_case()),
            "runs": len(results),
            "passed": sum(1 for item in results if item.ok),
            "pass_at_1": round(self.report.pass_at_1, 4),
            "pass_pow_k": round(self.report.pass_pow_k, 4),
            "llm_calls": sum(item.llm_calls for item in results),
            "cost_usd": round(sum(item.cost_usd for item in results), 6),
            "revise_rounds": sum(item.revise_rounds for item in results),
        }


@dataclass
class MatrixReport:
    """多配置对照结果；第一个变体是基线，其余给相对差值。"""

    variants: list[VariantResult] = field(default_factory=list)

    @property
    def baseline(self) -> VariantResult | None:
        return self.variants[0] if self.variants else None

    def to_dict(self) -> dict[str, Any]:
        base = self.baseline
        base_metrics = base.metrics if base else {}
        variants: list[dict[str, Any]] = []
        for item in self.variants:
            metrics = item.metrics
            delta = {
                key: round(float(metrics[key]) - float(base_metrics.get(key, 0)), 6)
                for key in ("pass_pow_k", "pass_at_1", "llm_calls", "cost_usd",
                            "revise_rounds")
            } if base is not None and item is not base else {}
            variants.append({
                "name": item.name,
                "overrides": item.overrides,
                "metrics": metrics,
                "delta_vs_baseline": delta,
                "results": [r.model_dump(mode="json") for r in item.report.results],
            })
        return {
            "baseline": base.name if base else None,
            "variants": variants,
        }


async def run_matrix(
    variants: Sequence[tuple[str, dict[str, Any]]],
    cases: Sequence[EvalCase],
    *,
    repeat: int = 1,
    base_settings: Settings | None = None,
    output_dir: Path | None = None,
) -> MatrixReport:
    """同一批 case × 多套配置，逐变体跑一遍并汇总。

    用途：把「换便宜复核」「开 review_rounds」「按 duty 拆两个 primary」这类**配置
    决策**变成可比较的数字，而不是凭感觉。第一个变体视为基线。

    每个变体用自己的子目录（``<output_dir>/<name>/``），否则第二次跑会读到第一次的
    产物，对比全失真。
    """
    if not variants:
        raise ValueError("至少需要一个变体")
    names = [name for name, _ in variants]
    if len(set(names)) != len(names):
        raise ValueError(f"变体名重复：{names}")
    base = base_settings or Settings.load()
    matrix = MatrixReport()
    for name, overrides in variants:
        settings = apply_overrides(base, overrides)
        sub = None
        if output_dir is not None:
            sub = Path(output_dir) / name
            sub.mkdir(parents=True, exist_ok=True)
        report = await run_dataset(
            cases, repeat=repeat, base_settings=settings, output_dir=sub)
        matrix.variants.append(
            VariantResult(name=name, overrides=dict(overrides), report=report))
    return matrix

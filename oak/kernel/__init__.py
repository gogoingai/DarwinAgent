"""OaK 内核资产层（框架级，数据集无关）。

四部件（docs/ARCHITECTURE.md）：
- schema    可表达什么（YAML，由各任务提供/起草）
- functions 可计算什么（规格 + 实现引用）
- checks    必须成立什么（声明 + 可执行校验器，见 checks.py）
- prompts   怎么驱动模型（角色 → 提示词来源 + 指纹）

本包只做"资产的登记、指纹、校验"，不含任何数据集知识；
数据集侧通过路径注入。评测器（judge/attr/确定性规则/gold 表）不属于内核，
永不进修补面（docs/DESIGN-closeout.md §4）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


def fp(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "-"


@dataclass
class KernelAssets:
    """一次运行所用内核资产的登记与指纹。"""
    schema_path: Path | None = None
    prompt_paths: dict[str, Path] = field(default_factory=dict)   # role -> 文件
    function_paths: list[Path] = field(default_factory=list)
    check_ids: list[str] = field(default_factory=list)           # checks.py 注册表里的 id

    def manifest(self) -> dict:
        return {
            "schema": {"path": str(self.schema_path), "fp": fp(self.schema_path)} if self.schema_path else None,
            "prompts": {r: {"path": str(p), "fp": fp(p)} for r, p in self.prompt_paths.items()},
            "functions": [{"path": str(p), "fp": fp(p)} for p in self.function_paths],
            "checks": sorted(self.check_ids),
        }

    def write_manifest(self, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.manifest(), ensure_ascii=False, indent=2))


def locomo_kernel_assets() -> KernelAssets:
    """locomo 轨道当前的内核资产登记（收口用；其他数据集照此注入路径）。"""
    root = Path(__file__).resolve().parents[2]
    pipe = root / "datasets" / "locomo" / "pipeline"
    from . import checks as _checks
    return KernelAssets(
        schema_path=root / "datasets" / "locomo" / "runs" / "frozen" / "schema.yaml",
        prompt_paths={
            "extract": pipe / "prompts" / "extract.py",
            "audit": pipe / "prompts" / "extract.py",     # 审计共用抽取提示词文件
            "canon": pipe / "entity_resolve.py",
            "act": pipe / "prompts" / "answer.py",        # ReAct 步骤规则在作答提示词族
            "answer": pipe / "prompts" / "answer.py",
        },
        function_paths=[pipe / "dates.py", pipe / "funcs_compile.py"],
        check_ids=[c.id for c in _checks.CHECKS],
    )

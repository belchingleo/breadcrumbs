"""安装或更新 Crumbs 与 Bread，并把旧版本移出 skill 扫描目录。

用法：
    python3 scripts/install.py --target claude
    python3 scripts/install.py --target codex
    python3 scripts/install.py --target both

安装后 Claude Code 使用 /crumbs 与 /bread；Codex 显式入口为
$crumbs 与 $bread。两个 skill 各自完整，避免运行时依赖仓库位置。
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import tempfile


SOURCE_ROOT = Path(__file__).resolve().parent.parent
SKILL_NAMES = ("crumbs", "bread")
SHARED_ENTRIES = ("scripts", "references", "assets")
LEGACY_NAME = "breadcrumbs"
HOST_AGENT_FILES = {
    "claude": ("claude/crumbs-evaluator.md", "crumbs-evaluator.md"),
    "codex": ("codex/crumbs-evaluator.toml", "crumbs-evaluator.toml"),
}


def workspace_for(skills_root: Path) -> Path:
    """备份与暂存必须在 skills 目录之外，避免被当成活 skill。"""
    return skills_root.parent / "breadcrumbs-backups"


def _copy_skill(name: str, destination: Path) -> None:
    source_skill = SOURCE_ROOT / "skills" / name / "SKILL.md"
    shutil.copy2(source_skill, destination / "SKILL.md")
    for entry in SHARED_ENTRIES:
        source = SOURCE_ROOT / entry
        target = destination / entry
        if source.is_dir():
            shutil.copytree(
                source, target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )


def install_to(
    skills_root: Path,
) -> tuple[dict[str, Path], dict[str, Path]]:
    """原子替换两个入口；返回安装位置与实际产生的备份。"""
    skills_root.mkdir(parents=True, exist_ok=True)
    workspace = workspace_for(skills_root)
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    staging = Path(tempfile.mkdtemp(prefix="staging-", dir=str(workspace)))
    backups: dict[str, Path] = {}
    installed: dict[str, Path] = {}
    moved: list[tuple[Path, Path]] = []
    placed: list[Path] = []

    try:
        for name in SKILL_NAMES:
            staged_skill = staging / name
            staged_skill.mkdir()
            _copy_skill(name, staged_skill)

        # 旧的单入口 breadcrumbs 不是兼容层。更新时直接移出加载目录，
        # 但保留可恢复备份，避免留下第三个会争抢触发的 skill。
        existing_names = (*SKILL_NAMES, LEGACY_NAME)
        for name in existing_names:
            current = skills_root / name
            if not current.exists():
                continue
            backup = workspace / f"{stamp}-{name}"
            current.rename(backup)
            backups[name] = backup
            moved.append((current, backup))

        for name in SKILL_NAMES:
            destination = skills_root / name
            (staging / name).rename(destination)
            installed[name] = destination
            placed.append(destination)
        staging.rmdir()
    except Exception:
        for destination in reversed(placed):
            if destination.exists():
                shutil.rmtree(destination)
        for current, backup in reversed(moved):
            if backup.exists() and not current.exists():
                backup.rename(current)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return installed, backups


def roots_for(target: str) -> list[Path]:
    home = Path.home()
    roots = {
        "claude": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
    }
    return list(roots.values()) if target == "both" else [roots[target]]


def install_host_agent(
    target: str,
    skills_root: Path,
) -> tuple[Path, Path | None]:
    """安装使用当前宿主认证的 evaluator；旧版仍放到可恢复备份。"""
    source_name, destination_name = HOST_AGENT_FILES[target]
    source = SOURCE_ROOT / "host-agents" / source_name
    agents_root = skills_root.parent / "agents"
    agents_root.mkdir(parents=True, exist_ok=True)
    destination = agents_root / destination_name
    workspace = workspace_for(skills_root)
    workspace.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = None
    temporary = agents_root / f".{destination_name}.{stamp}.tmp"
    shutil.copy2(source, temporary)
    try:
        if destination.exists():
            backup = workspace / f"{stamp}-agent-{destination_name}"
            destination.rename(backup)
        temporary.rename(destination)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        if backup is not None and backup.exists() and not destination.exists():
            backup.rename(destination)
        raise
    return destination, backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target", choices=("claude", "codex", "both"), default="claude",
        help="安装到哪个客户端的 skills 目录",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印目标位置，不写文件",
    )
    args = parser.parse_args()

    targets = ("claude", "codex") if args.target == "both" else (args.target,)
    for target in targets:
        root = roots_for(target)[0]
        if args.dry_run:
            for name in SKILL_NAMES:
                print(f"将安装到: {root / name}")
            print(
                "将安装 evaluator: "
                f"{root.parent / 'agents' / HOST_AGENT_FILES[target][1]}"
            )
            continue
        installed, backups = install_to(root)
        agent, agent_backup = install_host_agent(target, root)
        for name in SKILL_NAMES:
            print(f"已安装: {installed[name]}")
        print(f"已安装 evaluator: {agent}")
        for name, backup in backups.items():
            print(f"{name} 上一版备份: {backup}")
        if agent_backup is not None:
            print(f"evaluator 上一版备份: {agent_backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

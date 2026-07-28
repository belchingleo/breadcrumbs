"""安装或更新 Breadcrumbs skill，并保留上一版作为可恢复备份。

用法：
    python3 scripts/install.py --target claude
    python3 scripts/install.py --target codex
    python3 scripts/install.py --target both

只分发运行所需的 SKILL.md、scripts/ 与 assets/。测试、QA 报告和产品文档
不会进入用户的 skill 目录。
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import shutil
import tempfile


SOURCE_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ENTRIES = ("SKILL.md", "scripts", "assets")


def workspace_for(skills_root: Path) -> Path:
    """备份与暂存的存放处——必须在 skills 目录**之外**。

    实测教训: 原先把 `breadcrumbs.backup-<时间戳>` 直接放在
    `~/.claude/skills/` 里。那是 skill 的加载目录, 于是备份被当成一个
    活的 skill 加载了——而且它的 frontmatter 里 `name: breadcrumbs`
    与正式版同名, 两个同名 skill 争抢触发, 用户敲 /crumbs 可能命中旧版。
    「可回滚」这个安全措施本身制造了一个更隐蔽的故障。

    放到 skills 的父目录（~/.claude/ 或 ~/.codex/）下: 仍在工具自己的
    配置目录内, 与目标同一文件系统（保证 rename 原子）, 但不会被扫描成 skill。
    """
    return skills_root.parent / "breadcrumbs-backups"


def install_to(skills_root: Path) -> tuple[Path, Path | None]:
    skills_root.mkdir(parents=True, exist_ok=True)
    destination = skills_root / "breadcrumbs"
    workspace = workspace_for(skills_root)
    workspace.mkdir(parents=True, exist_ok=True)

    # 暂存也移出 skills 目录: 进程被硬杀时残留的半成品同样不该被当 skill 扫到。
    staging = Path(tempfile.mkdtemp(
        prefix="staging-", dir=str(workspace)
    ))
    backup = None
    try:
        for name in PACKAGE_ENTRIES:
            source = SOURCE_ROOT / name
            target = staging / name
            if source.is_dir():
                shutil.copytree(
                    source, target,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )
            else:
                shutil.copy2(source, target)

        if destination.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = workspace / stamp
            destination.rename(backup)
        staging.rename(destination)
    except Exception:
        if backup is not None and backup.exists() and not destination.exists():
            backup.rename(destination)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return destination, backup


def roots_for(target: str) -> list[Path]:
    home = Path.home()
    roots = {
        "claude": home / ".claude" / "skills",
        "codex": home / ".codex" / "skills",
    }
    return list(roots.values()) if target == "both" else [roots[target]]


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

    for root in roots_for(args.target):
        destination = root / "breadcrumbs"
        if args.dry_run:
            print(f"将安装到: {destination}")
            continue
        installed, backup = install_to(root)
        print(f"已安装: {installed}")
        if backup:
            print(f"上一版备份: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

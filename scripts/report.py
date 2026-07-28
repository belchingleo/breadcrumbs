"""Breadcrumbs 第 4 步: analysis.json (+ annotations.json) → replay.html

分开成独立入口是有意的: 同一份结构分析可以配不同版本的标注反复重渲染,
agent 改了标注不必重跑相似度计算。

用法
    python3 report.py analysis.json -o replay.html
    python3 report.py analysis.json -a annotations.json -o replay.html
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from render import build_page                               # noqa: E402
from validate_annotations import validate                   # noqa: E402


def attach_annotations(res: dict, ann: dict) -> None:
    """把通过校验的标注送进渲染契约，不丢失报告级语义。"""
    res["annotations"] = {
        int(t["id"]): t for t in ann.get("threads", [])
    }
    report_meta = ann.get("report")
    if isinstance(report_meta, dict):
        res["report_meta"] = dict(report_meta)


def to_res(analysis: dict) -> dict:
    """把 analysis.json 的 _render 段还原成 render.py 期望的结构。"""
    r = dict(analysis["_render"])
    r["threads"] = {int(k): v for k, v in r["threads"].items()}
    r["analysis_meta"] = {
        "analysis_id": analysis.get("analysis_id"),
        "algorithm_version": analysis.get("algorithm_version"),
        "session_fingerprint": analysis.get("session_fingerprint"),
        "corpus": analysis.get("corpus") or {},
        "filtered_out": analysis.get("filtered_out") or {},
    }
    r["analysis_threads"] = {
        int(t["id"]): t for t in analysis.get("threads", [])
    }
    r["trust"] = {
        "annotations": "inferred",
        "forced": False,
        "validation_errors": [],
    }
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("analysis", nargs="+", help="一个或多个 analysis.json")
    ap.add_argument("-a", "--annotations", action="append", default=[],
                    help="对应的 annotations.json（顺序与 analysis 一致）")
    ap.add_argument("-o", "--out", default="replay.html")
    ap.add_argument("--force", action="store_true",
                    help="忽略标注校验错误强行渲染（仅调试用）")
    args = ap.parse_args()

    sessions = []
    for i, ap_path in enumerate(args.analysis):
        p = Path(ap_path).expanduser()
        if not p.exists():
            print(f"不存在: {p}", file=sys.stderr)
            return 1
        analysis = json.loads(p.read_text(encoding="utf-8"))
        res = to_res(analysis)

        ann = None
        if i < len(args.annotations):
            apath = Path(args.annotations[i]).expanduser()
            if not apath.exists():
                print(f"标注文件不存在: {apath}", file=sys.stderr)
                return 1
            ann = json.loads(apath.read_text(encoding="utf-8"))
        # 标注也可能直接写回 analysis.json 的 threads 里
        if ann is None:
            filled = {t["id"]: t for t in analysis["threads"]
                      if t.get("name") or t.get("topic")}
            ann = (
                {
                    "threads": list(filled.values()),
                    **({"report": analysis["report"]}
                       if isinstance(analysis.get("report"), dict) else {}),
                }
                if filled else None
            )
        if ann:
            # P0-1: 校验不通过就拒绝出图。错位但漂亮的报告比没有报告更危险。
            warns: list[str] = []
            errs = validate(analysis, ann, warns)
            if errs and not args.force:
                print(f"✗ {p.name} 的标注校验失败（{len(errs)} 项），拒绝渲染：",
                      file=sys.stderr)
                for e in errs:
                    print(f"  · {e}", file=sys.stderr)
                print("\n如确认要带着已知错位渲染（仅供调试），加 --force。",
                      file=sys.stderr)
                return 1
            if errs:
                print(f"⚠ 已用 --force 忽略 {len(errs)} 项校验错误，"
                      f"报告内容可能与实际线路不符", file=sys.stderr)
            for w in warns:
                print(f"⚠ {w}", file=sys.stderr)
            attach_annotations(res, ann)
            res["trust"] = {
                "annotations": "forced" if errs else "confirmed",
                "forced": bool(errs),
                "validation_errors": errs,
            }

        meta = {
            "cwd": analysis["session"].get("cwd", ""),
            "first_time": analysis["session"].get("first_time", ""),
            "last_time": analysis["session"].get("last_time", ""),
        }
        sessions.append((analysis["session"]["title"], meta, res))

    out = Path(args.out)
    build_page(sessions, out)
    print(f"报告: {out}  ({out.stat().st_size/1024:.0f}K)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

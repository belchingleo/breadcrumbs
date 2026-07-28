"""把旧标注重新对齐到新一份分析上，救回可复用的语义判断。

为什么需要
----------
线路划分对实现细节仍然敏感：同一场会话，算法微调后条数可能从 7 变 8。
`thread_signature` 里含轮次集合，一重划就整批失配，于是每次调参都要重标一遍。
但**语义判断往往还成立**——变的是这条线覆盖到第几轮，不是它在追问什么。

本工具按「起点内容」而非「顺序编号」重新配对：
    content_signature（只由首轮原文决定）→ anchor_quote 前缀 → 放弃

对齐结果不是免检通行证。凡是轮次构成变过的，都会被标进 `agent_note`
提醒复核；配不上的旧标注会明确列出来，不静默丢弃。

用法
    python3 realign.py 新的analysis.json 旧的annotations.json -o 新的annotations.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import identity                                    # noqa: E402

CARRY_FIELDS = (
    "name", "topic", "yield", "outcome", "resolved", "spawned_by",
    "relation_to_trunk", "relation_note", "resolution_evidence", "agent_note",
)


def _content_sig(entry: dict) -> str | None:
    """旧标注可能没有 content_signature，用 anchor_quote 现算一个。"""
    if entry.get("content_signature"):
        return entry["content_signature"]
    anchor = entry.get("anchor_quote")
    return identity.content_signature(anchor) if anchor else None


def realign(analysis: dict, old: dict) -> tuple[dict, list[str]]:
    notes: list[str] = []
    by_content: dict[str, dict] = {}
    by_prefix: dict[str, dict] = {}
    for entry in old.get("threads", []):
        sig = _content_sig(entry)
        if sig:
            by_content.setdefault(sig, entry)
        anchor = entry.get("anchor_quote") or ""
        if anchor:
            by_prefix.setdefault(anchor[:20], entry)

    used: set[int] = set()
    out_threads = []
    for thread in analysis.get("threads", []):
        anchor = thread.get("anchor_quote") or ""
        match = by_content.get(thread.get("content_signature") or "")
        how = "内容签名"
        if match is None:
            match = by_prefix.get(anchor[:20])
            how = "起点原话"
        if match is None:
            notes.append(f"线{thread['id']} 没有可复用的旧标注，需要新标："
                         f"「{anchor[:40]}」")
            continue

        used.add(id(match))
        row = {
            "id": thread["id"],
            "thread_signature": thread["thread_signature"],
            "anchor_quote": thread["anchor_quote"],
            "content_signature": thread.get("content_signature"),
        }
        for field in CARRY_FIELDS:
            if match.get(field) is not None:
                row[field] = match[field]

        # 轮次构成变了就留痕，不让复用看起来像已核实
        if match.get("thread_signature") != thread["thread_signature"]:
            hint = (f"[已按{how}对齐] 这条线的轮次构成较上一版有变动"
                    f"（现 {thread.get('turn_count','?')} 轮），"
                    f"语义判断沿用旧标注，覆盖范围待复核。")
            row["agent_note"] = (
                f"{hint} {row['agent_note']}" if row.get("agent_note") else hint
            )
            notes.append(f"线{thread['id']} 按{how}对齐，轮次构成有变动，"
                         f"已在 agent_note 标记待复核")
        out_threads.append(row)

    for entry in old.get("threads", []):
        if id(entry) not in used:
            notes.append(f"旧标注「{entry.get('name') or entry.get('anchor_quote','')[:24]}」"
                         f"在新分析里找不到对应线路，已丢弃")

    new = {"analysis_id": analysis.get("analysis_id"), "threads": out_threads}
    if old.get("report"):
        new["report"] = old["report"]
    return new, notes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("analysis")
    ap.add_argument("annotations")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args()

    analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    old = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
    new, notes = realign(analysis, old)

    Path(args.out).write_text(
        json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")

    total = len(analysis.get("threads", []))
    print(f"对齐 {len(new['threads'])}/{total} 条 → {args.out}")
    for n in notes:
        print(f"  · {n}")
    if len(new["threads"]) < total:
        print("\n仍有线路缺标注，渲染前需要补齐。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

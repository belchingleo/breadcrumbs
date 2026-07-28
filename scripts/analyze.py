"""Breadcrumbs 第 2 步: 结构分析, 产出 analysis.json 供 agent 标注。

设计要点
--------
* 输出**按追问线组织**, 不按轮次。agent 是一条线一条线地读和命名的,
  按线组织能让它一次处理一个完整语义单元, 也便于长会话分批。
* prompt 默认截断到 PREVIEW_CHARS, 控制 token。agent 需要全文时
  用 fetch_reply.py --prompt <轮号> 取。
* 只写**结构事实**, 不写任何语义判断。命名、主题、产出、是否解决,
  全部留 null 给 agent 填——脚本臆造摘要会毁掉整张表的可信度。

用法
    python3 analyze.py <session.jsonl> -o analysis.json
    python3 analyze.py --source codex <task-id> -o analysis.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from conversation_sources import (Conversation, ConversationSourceError,  # noqa: E402
                                  collapsed_prompts, load_conversation)
from probe import (analyze as run_analyze, fit_corpus,      # noqa: E402
                   collect_corpus_texts)
from render import thread_stats                             # noqa: E402
import identity                                             # noqa: E402

PREVIEW_CHARS = 200

# P0-3: 语料太小 -> 相似度失去分辨率, 每条 prompt 都会被判成新线。
# 新装 skill 的用户恰恰最可能没有历史会话, 必须显式降级而不是照常出图。
MIN_CORPUS = 40


def build_analysis(
    session: Path | Conversation,
    corpus_limit: int = 200,
    *,
    source: str = "claude",
) -> dict:
    conversation = (
        session if isinstance(session, Conversation)
        else load_conversation(source, session)
    )
    all_entries = conversation.prompts
    kept = [p for p in all_entries if not p["suspect_auto"]]
    # 折叠重发: 撞额度导致的原样重发不是新一轮。保留最后一条,
    # 因为用户若在重发前微调过, 最后一条才是他真正想说的。
    prompts = collapsed_prompts(all_entries)
    retries = len(kept) - len(prompts)
    # P1-1: extract_prompts 的设计原则是「只标记不静默删除」, 下游不能背叛它。
    # 这里如实记录被排除的条目, 让 agent 和用户都能复核。
    dropped = [p for p in all_entries if p["suspect_auto"]]
    filtered_summary = {
        "retries_collapsed": retries,
        "count": len(dropped),
        "samples": [{"line": d["line"],
                     "text": " ".join(d["text"].split())[:60]}
                    for d in dropped[:12]],
        "note": "这些条目被判为 harness 自动产生（中断标记、系统提醒、"
                "skill 载入等）。若其中有你真实输入的内容，请人工核对。",
    }
    if len(prompts) < 2:
        return {"error": "真人轮次不足 2, 无法分析", "n": len(prompts)}

    texts_all = [p["text"] for p in prompts]

    # --- 语料与降级判定（P0-3）---
    corpus = collect_corpus_texts(
        limit_files=corpus_limit, source=conversation.source
    )
    corpus = sorted(corpus)          # 排序: rglob 顺序不定会破坏可复现性
    history_corpus_size = len(corpus)
    degraded = history_corpus_size < MIN_CORPUS
    if degraded:
        # 退而用本会话自身作语料。分辨率差, 但至少不是零维向量。
        corpus = sorted(set(corpus) | set(texts_all))
    vec = fit_corpus(corpus)
    res = run_analyze(prompts, vec)
    stats = thread_stats(res)
    trunk_id = max(stats, key=lambda s: s["count"])["tid"]

    # 每轮的事件与链接, 按轮号索引, 方便 agent 交叉引用
    step_by_turn = {s["to"]: s for s in res["steps"]}

    threads = []
    for st in stats:
        turns = []
        for t in st["rounds"]:
            s = step_by_turn.get(t)
            full = res["texts_full"][t - 1]
            turns.append({
                "turn": t,
                "time": res["timestamps"][t - 1][:19].replace("T", " "),
                "chars": len(full),
                "text": full[:PREVIEW_CHARS] +
                        ("…" if len(full) > PREVIEW_CHARS else ""),
                "truncated": len(full) > PREVIEW_CHARS,
                "event": (s or {}).get("kind"),
                "links_to": (s or {}).get("link_to"),
                "markers": list((s or {}).get("l2_hits", {}).values()),
            })
        threads.append({
            "id": st["tid"],
            "thread_signature": identity.thread_signature(
                st["rounds"], res["texts_full"][st["first"] - 1]),
            "anchor_quote": identity.anchor_quote(
                res["texts_full"][st["first"] - 1]),
            # 跨会话匹配用（不含会话相对轮次），当前不消费, 仅前向兼容
            "content_signature": identity.content_signature(
                res["texts_full"][st["first"] - 1]),
            "is_trunk": st["tid"] == trunk_id,
            "first_turn": st["first"],
            "last_turn": st["last"],
            "turn_count": st["count"],
            "spawn_event": st["kind"],
            "dormancies": st["dormancies"],
            # 脚本的结构判断; agent 可以推翻它
            "dangling_by_structure": st["dangling"],
            "turns_since_last_touch": res["n"] - st["last"],
            "turns": turns,
            # ---- 以下留给 agent 填 ----
            "name": None,              # 一句话给这条线命名
            "topic": None,             # 在讨论什么
            "yield": None,             # 产出了什么
            "outcome": None,           # conclusion | assumption | unresolved
            "resolved": None,          # true / false
            "spawned_by": None,        # user | assistant
            "relation_to_trunk": None,  # redirected|supplied|blocked|tangent|dropped
            "relation_note": None,      # 一句话说明这条线对主线做了什么
            "resolution_evidence": None,  # 声称未解决时必填: 读了哪一轮之后的
                                          # AI 回复, 看到了什么
            "agent_note": None,        # 需要推翻脚本判断时写在这里
        })

    fp = identity.session_fingerprint(texts_all)
    cdig = identity.corpus_digest(corpus)
    anchors = [
        identity.prompt_anchor(p["text"], p.get("timestamp", ""),
                               p.get("source_id", ""))
        for p in prompts
    ]

    # 锚点属于 prompt，而不属于算法划出的线路。把它回填到 agent 视图，
    # 即使之后重新分线，用户仍可沿同一句原话回到现场。
    for thread in threads:
        for turn in thread["turns"]:
            turn["prompt_anchor"] = anchors[turn["turn"] - 1]

    return {
        "analysis_id": identity.analysis_id(fp, res["theta"], cdig),
        "algorithm_version": identity.ALGORITHM_VERSION,
        "session_fingerprint": fp,
        "corpus": {
            "history_size": history_corpus_size,
            "fit_size": len(corpus),
            # 兼容旧消费者；size 表示实际用于拟合的规模。
            "size": len(corpus),
            "digest": cdig,
            "degraded": degraded,
            "degrade_reason": (
                f"本机可用历史会话语料仅 {history_corpus_size} 条"
                f"（低于 {MIN_CORPUS}），"
                "相似度分辨率不足，线路划分可信度低"
            ) if degraded else None,
        },
        "filtered_out": filtered_summary,
        "session": {
            "source": conversation.source,
            "source_id": conversation.source_id,
            # 兼容旧的 report 消费者；Codex 使用 codex:<thread-id> locator。
            "file": conversation.locator,
            "title": conversation.title,
            "cwd": conversation.cwd,
            "human_turns": res["n"],
            "thread_count": len(threads),
            "theta": res["theta"],
            "first_time": res["timestamps"][0][:19].replace("T", " "),
            "last_time": res["timestamps"][-1][:19].replace("T", " "),
        },
        "threads": threads,
        # 渲染需要, agent 不必读
        "_render": {
            "n": res["n"], "theta": res["theta"],
            "threads": {str(k): v for k, v in res["threads"].items()},
            "thread_id": res["thread_id"],
            "steps": res["steps"],
            "sim_matrix": res["sim_matrix"],
            "texts_head": res["texts_head"],
            "texts_full": res["texts_full"],
            "timestamps": res["timestamps"],
            "prompt_anchors": anchors,
            "sim_adj": res["sim_adj"],
            "sim_to_first": res["sim_to_first"],
        },
    }


def agent_view(analysis: dict) -> dict:
    """剥掉 _render, 只留 agent 需要读的部分（省 token）。"""
    return {k: v for k, v in analysis.items() if k != "_render"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session", nargs="?",
                    help="会话路径/id；Codex id 可写完整值或唯一前缀")
    ap.add_argument("--source", choices=("claude", "codex"), default="claude",
                    help="输入来源（默认 claude，旧命令保持兼容）")
    ap.add_argument("--last", action="store_true", help="用最近修改的会话")
    ap.add_argument("--corpus-limit", type=int, default=200,
                    help="最多用多少个同来源会话拟合结构语料（默认 200）")
    ap.add_argument("-o", "--out", default="analysis.json")
    ap.add_argument("--agent-view", action="store_true",
                    help="额外写一份不含渲染数据的精简版, 供 agent 读")
    args = ap.parse_args()

    try:
        conversation = load_conversation(
            args.source, args.session, last=args.last
        )
    except ConversationSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        analysis = build_analysis(
            conversation, corpus_limit=max(1, args.corpus_limit)
        )
    except ConversationSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if "error" in analysis:
        print(analysis["error"], file=sys.stderr)
        return 1

    out = Path(args.out)
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    if args.agent_view:
        av = out.with_name(out.stem + ".agent.json")
        av.write_text(json.dumps(agent_view(analysis), ensure_ascii=False,
                                 indent=1), encoding="utf-8")
        print(f"agent 视图: {av}  ({av.stat().st_size/1024:.0f}K)")

    s = analysis["session"]
    print(f"《{s['title']}》 {s['human_turns']} 轮 / "
          f"{s['thread_count']} 条线  →  {out}")
    dang = [t for t in analysis["threads"]
            if t["dangling_by_structure"] and not t["is_trunk"]]
    if dang:
        print(f"结构上疑似悬空的线: {[t['id'] for t in dang]}"
              f"（待 agent 核实）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

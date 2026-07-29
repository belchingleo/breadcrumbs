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
    keep_sim_matrix: bool = False,
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
            "texts_head": res["texts_head"],
            "texts_full": res["texts_full"],
            "timestamps": res["timestamps"],
            "prompt_anchors": anchors,
            "sim_adj": res["sim_adj"],
            "sim_to_first": res["sim_to_first"],
            # n×n 相似度矩阵只在诊断时才落盘。它是链接算法的中间量, 渲染层
            # 自热力图移除后已无消费者; 83 轮会话就要 21K, 长会话更夸张。
            **({"sim_matrix": res["sim_matrix"]} if keep_sim_matrix else {}),
        },
    }


# agent view 里每条线保留的轮次上限。超过就折叠中段, 只留结构上有话说的轮次。
# 这个数字要够 agent 判断「特别核查四类误判」, 又不至于把长主线整段灌进上下文。
KEEP_TURNS = 8

# 这三个键只有渲染器需要, 而渲染器读的是完整 analysis.json（带 _render）,
# 不读 agent view。留在 agent view 里纯属让 agent 为看不懂的东西付钱。
_TURN_KEYS_FOR_RENDER_ONLY = ("time", "chars", "truncated")


def _slim_turn(turn: dict) -> dict:
    """去掉 agent 用不上的键, 并省略空值——空值也要花 token。"""
    out = {
        k: v for k, v in turn.items()
        if k not in _TURN_KEYS_FOR_RENDER_ONLY
    }
    for key in ("event", "links_to"):
        if out.get(key) is None:
            out.pop(key, None)
    if not out.get("markers"):
        out.pop("markers", None)
    return out


def _keep_turn_numbers(turns: list[dict]) -> set[int]:
    """挑出中段折叠后仍要保留的轮次。

    保留标准是「这一轮在结构和时间位置上有代表性」：线的首尾、带事件或话语
    标记的轮次，再用均匀采样补齐中段。不能按文本长度挑选：平台自动注入的
    环境外壳和粘贴材料往往最长，却不一定是这条思路最关键的证据。
    """
    keep = {turns[0]["turn"], turns[-1]["turn"]}

    def spread(items: list[dict], slots: int) -> list[dict]:
        if slots <= 0 or not items:
            return []
        if len(items) <= slots:
            return items
        if slots == 1:
            return [items[len(items) // 2]]
        indexes = {
            round(i * (len(items) - 1) / (slots - 1))
            for i in range(slots)
        }
        return [items[index] for index in sorted(indexes)]

    remaining = [t for t in turns if t["turn"] not in keep]
    for group in (
        [t for t in remaining if t.get("event")],
        [t for t in remaining if not t.get("event") and t.get("markers")],
        [t for t in remaining if not t.get("event") and not t.get("markers")],
    ):
        for turn in spread(group, KEEP_TURNS - len(keep)):
            keep.add(turn["turn"])
        if len(keep) >= KEEP_TURNS:
            break
    return keep


def agent_view(analysis: dict) -> dict:
    """剥掉 _render 与渲染专用字段, 只留 agent 真正要读的部分（省 token）。

    完整 analysis.json 不受影响: 它才是 validate_annotations 校验
    ``evidence_turn`` 归属、以及 render 出图的依据。这里只压缩喂给 agent 的那份。
    """
    view = {k: v for k, v in analysis.items() if k != "_render"}
    threads = []
    for thread in view.get("threads", []):
        thread = dict(thread)
        turns = [_slim_turn(t) for t in thread.get("turns", [])]
        if len(turns) > KEEP_TURNS:
            keep = _keep_turn_numbers(turns)
            kept = [t for t in turns if t["turn"] in keep]
            # 明确告诉 agent 中间被折叠了多少轮, 以及怎么取回来,
            # 免得它把「没看到」误当成「没发生」。
            thread["turns_elided"] = len(turns) - len(kept)
            thread["turns_elided_hint"] = (
                "中段轮次已折叠；需要时用 fetch_reply.py --prompt <轮号> 取全文"
            )
            turns = kept
        thread["turns"] = turns
        threads.append(thread)
    view["threads"] = threads
    return view


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
    ap.add_argument("--debug", action="store_true",
                    help="额外写入 n×n 相似度矩阵等诊断数据（体积明显变大）")
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
            conversation, corpus_limit=max(1, args.corpus_limit),
            keep_sim_matrix=args.debug,
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

"""按轮号回读 AI 回复或 prompt 全文——供 agent 按需取用, 而非全量灌入。

这是「AI 输出可读、不可测、不入库」这条约束的执行工具:
AI 回复只在 agent 判断某条支线的产出时才回读, 不进入结构分析。

用法
    python3 fetch_reply.py <session.jsonl> --auto analysis.agent.json
    python3 fetch_reply.py <session.jsonl> --reply 18
    python3 fetch_reply.py --source codex <task-id> --reply 18
    python3 fetch_reply.py <session> --reply 18 --before
    python3 fetch_reply.py <session> --before-reply 3,7 --reply 18,25
    python3 fetch_reply.py <session> --prompt 18

``--auto`` 是默认路径: 一条命令取回整场标注所需的全部回复。逐条回读会让
每条线产生两次工具往返, 而每次往返都要重新计费已累积的上下文——
真正的 token 成本在往返次数, 不在单段回复长度。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conversation_sources import (Conversation, ConversationSourceError,  # noqa: E402
                                  collapsed_prompts, load_conversation)

MAX_CHARS = 3000
# 整场扫描时每段的上限, 见 --auto。
SWEEP_CHARS = 1500


def load_stream(session: Path | Conversation, *, source: str = "claude"):
    """返回两个来源共用的 [(human|assistant, text, source_id)]。"""
    conversation = (
        session if isinstance(session, Conversation)
        else load_conversation(source, session)
    )
    return conversation.stream


def reply_for_turn(stream, turn: int, before: bool = False) -> str | None:
    """取第 turn 个真人轮次「之后」(默认) 或「之前」的助手回复。"""
    idxs = [i for i, (k, _, _) in enumerate(stream) if k == "human"]
    if turn < 1 or turn > len(idxs):
        return None
    pos = idxs[turn - 1]
    rng = range(pos - 1, -1, -1) if before else range(pos + 1, len(stream))
    chunks = []
    for i in rng:
        kind, txt, _ = stream[i]
        if kind == "human":
            break
        chunks.append(txt)
    if not chunks:
        return None
    if before:
        chunks.reverse()
    return "\n\n".join(chunks)


def prompt_for_turn(
    session: Path | Conversation,
    turn: int,
    *,
    source: str = "claude",
) -> str | None:
    conversation = (
        session if isinstance(session, Conversation)
        else load_conversation(source, session)
    )
    ps = collapsed_prompts(conversation.prompts)
    if 1 <= turn <= len(ps):
        return ps[turn - 1]["text"]
    return None


def _turns(spec: str) -> list[int]:
    return [int(x) for x in spec.split(",") if x.strip()]


def sweep_plan(agent_view: dict) -> list[tuple[int, int, bool, str]]:
    """从 agent view 推出整场需要回读的位置。

    对每条线只取两处, 与 SKILL.md 第 3 步一致: 起点之前（判断由谁引出）、
    末次提问之后（判断留下了什么、是否收束）。这是一次扫完全场所需的最小集合。
    """
    plan = []
    for thread in agent_view.get("threads", []):
        tid = thread.get("id")
        first, last = thread.get("first_turn"), thread.get("last_turn")
        if first:
            plan.append((tid, first, True, "起点前 · 判断由谁引出"))
        if last:
            plan.append((tid, last, False, "末次提问后 · 判断产出与是否收束"))
    return plan


def compact_sweep_plan(
    plan: list[tuple[int, int, bool, str]],
) -> list[tuple[list[int], int, bool, str]]:
    """合并多个思路指向的同一段回复，避免重复输出、重复占 token。"""
    grouped: dict[tuple[int, bool], tuple[list[int], str]] = {}
    for tid, turn, before, why in plan:
        key = (turn, before)
        if key not in grouped:
            grouped[key] = ([], why)
        grouped[key][0].append(tid)
    return [
        (tids, turn, before, why)
        for (turn, before), (tids, why) in grouped.items()
    ]


def clip_reply(text: str, limit: int) -> str:
    """超限时保留开头与结尾；结论和执行结果经常出现在回复末尾。"""
    if len(text) <= limit:
        return text
    marker = "\n\n…（中间省略；可单独回读全文）…\n\n"
    room = max(0, limit - len(marker))
    head = round(room * 0.58)
    tail = room - head
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--source", choices=("claude", "codex"), default="claude")
    ap.add_argument("--reply", help="轮号, 逗号分隔（取该轮之后的回复）")
    ap.add_argument("--before-reply", dest="before_reply",
                    help="轮号, 逗号分隔（取该轮之前的回复）。"
                         "可与 --reply 同时使用, 一次调用取回两种位置")
    ap.add_argument("--prompt", help="轮号, 逗号分隔")
    ap.add_argument("--before", action="store_true",
                    help="让 --reply 取之前的回复（等价于 --before-reply, 保留兼容）")
    ap.add_argument("--auto", metavar="ANALYSIS_AGENT_JSON",
                    help="读 analysis.agent.json, 一次取回整场标注所需的全部回复")
    ap.add_argument("--max-chars", type=int, default=None)
    args = ap.parse_args()

    if not (args.reply or args.before_reply or args.prompt or args.auto):
        ap.error("需要 --reply / --before-reply / --prompt / --auto 之一")

    try:
        conversation = load_conversation(args.source, args.session)
    except ConversationSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # --auto 是整场扫描, 每段收紧一些; 实测回复中位数约 780 字符, 1500 几乎不截断,
    # 又能保证长会话不会一次灌爆上下文。单点回读仍用宽的 MAX_CHARS。
    limit = args.max_chars if args.max_chars else (
        SWEEP_CHARS if args.auto else MAX_CHARS)

    def emit(title: str, txt: str | None) -> None:
        print(f"===== {title} =====")
        print(clip_reply(txt, limit) if txt else "(无)")
        print()

    if args.auto:
        try:
            view = json.loads(Path(args.auto).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"读不了 {args.auto}: {exc}", file=sys.stderr)
            return 1
        expected = view.get("session") or {}
        if (
            expected.get("source")
            and expected.get("source") != conversation.source
        ) or (
            expected.get("source_id")
            and str(expected.get("source_id")) != str(conversation.source_id)
        ):
            print(
                "analysis.agent.json 与当前会话不匹配："
                f"分析属于 {expected.get('source')}:{expected.get('source_id')}，"
                f"当前读取 {conversation.source}:{conversation.source_id}。"
                "请重新分析或选择正确会话。",
                file=sys.stderr,
            )
            return 1
        plan = sweep_plan(view)
        if not plan:
            print("agent view 里没有线, 无可回读的位置", file=sys.stderr)
            return 1
        stream = load_stream(conversation)
        compact = compact_sweep_plan(plan)
        for tids, turn, before, why in compact:
            refs = "、".join(str(tid) for tid in tids)
            emit(f"思路 {refs} · 轮次 {turn} · {why}",
                 reply_for_turn(stream, turn, before))
        print(f"（共 {len(compact)} 段，覆盖 {len(plan)} 个标注位置，"
              f"每段上限 {limit} 字符；"
              f"需要某段全文时用 --reply/--before-reply 单取）")
        return 0

    if args.prompt:
        for t in _turns(args.prompt):
            emit(f"轮次 {t} · 用户 prompt 全文", prompt_for_turn(conversation, t))

    if args.reply or args.before_reply:
        stream = load_stream(conversation)
        for spec, before in ((args.before_reply, True),
                             (args.reply, bool(args.before))):
            if not spec:
                continue
            which = "之前" if before else "之后"
            for t in _turns(spec):
                emit(f"轮次 {t} {which}的 AI 回复",
                     reply_for_turn(stream, t, before))
    return 0


if __name__ == "__main__":
    sys.exit(main())

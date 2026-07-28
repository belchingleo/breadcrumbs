"""按轮号回读 AI 回复或 prompt 全文——供 agent 按需取用, 而非全量灌入。

这是「AI 输出可读、不可测、不入库」这条约束的执行工具:
AI 回复只在 agent 判断某条支线的产出时才回读, 不进入结构分析。

用法
    python3 fetch_reply.py <session.jsonl> --reply 18
    python3 fetch_reply.py --source codex <task-id> --reply 18
    python3 fetch_reply.py <session> --reply 18 --before
    python3 fetch_reply.py <session> --prompt 18
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from conversation_sources import (Conversation, ConversationSourceError,  # noqa: E402
                                  collapsed_prompts, load_conversation)

MAX_CHARS = 3000


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--source", choices=("claude", "codex"), default="claude")
    ap.add_argument("--reply", help="轮号, 逗号分隔")
    ap.add_argument("--prompt", help="轮号, 逗号分隔")
    ap.add_argument("--before", action="store_true",
                    help="取该轮之前的回复（判断是谁挑起了这条线时用）")
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    args = ap.parse_args()

    try:
        conversation = load_conversation(args.source, args.session)
    except ConversationSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    def clip(s: str) -> str:
        return s if len(s) <= args.max_chars else s[:args.max_chars] + "\n…（截断）"

    if args.prompt:
        for t in [int(x) for x in args.prompt.split(",")]:
            txt = prompt_for_turn(conversation, t)
            print(f"===== 轮次 {t} · 用户 prompt 全文 =====")
            print(clip(txt) if txt else "(无)")
            print()

    if args.reply:
        stream = load_stream(conversation)
        which = "之前" if args.before else "之后"
        for t in [int(x) for x in args.reply.split(",")]:
            txt = reply_for_turn(stream, t, args.before)
            print(f"===== 轮次 {t} {which}的 AI 回复 =====")
            print(clip(txt) if txt else "(无)")
            print()

    if not args.reply and not args.prompt:
        ap.error("需要 --reply 或 --prompt")
    return 0


if __name__ == "__main__":
    sys.exit(main())

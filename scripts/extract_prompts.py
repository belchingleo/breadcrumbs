"""从 Claude Code 或 Codex 会话中抽取「真人输入的 prompt」序列。

用法:
    python3 extract_prompts.py <session.jsonl>              # 打印摘要
    python3 extract_prompts.py <session.jsonl> --json out.json
    python3 extract_prompts.py --list                       # 列出本机所有会话
    python3 extract_prompts.py --source codex --list
    python3 extract_prompts.py --source codex <task-id>

设计要点:
    JSONL 里 role=user 的条目混杂了四类东西, 只有第一类是研究对象:
      1. 真人敲进去的 prompt          <- 要的
      2. 工具返回 (tool_result)        <- 丢弃
      3. 系统注入 (system-reminder 等) <- 丢弃
      4. harness 生成的伪 prompt       <- 标记为 suspect, 不静默丢弃
    第 4 类不直接删除, 因为「什么算真人输入」本身是可争议的,
    静默丢弃会让下游无法复核。一律标记, 由使用者决定。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# harness / 客户端自动生成, 不是人打的字。前缀匹配。
AUTO_PREFIXES = (
    "<system-reminder>",
    "[SYSTEM NOTIFICATION",
    "<task-notification>",
    "[Image:",
    "Base directory for this skill:",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-caveat>",
    "Caveat: The messages below were generated",
    # 中断标记是 harness 写入的, 不是人打的字。
    # 实测漏掉它会凭空造出一条只含两个中断标记的「追问线」。
    "[Request interrupted",
    "[Request cancelled",
    "API Error",
    "You've hit your session limit",
)

# 完全等于这些的, 视为 harness 续接指令
AUTO_EXACT = {
    "Continue from where you left off.",
    "continue",
    "Continue",
}


def human_text(msg: dict) -> str | None:
    """取出 user 消息里的人类文本; 工具结果返回 None。"""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            # 含 tool_result 的整条都是工具返回, 不是人说的话
            if block.get("type") == "tool_result":
                return None
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts) if parts else None
    return None


def is_auto(text: str) -> bool:
    t = text.strip()
    if t in AUTO_EXACT:
        return True
    return any(t.startswith(p) for p in AUTO_PREFIXES)


def _near_dup(a: str, b: str) -> float:
    """相邻两条 prompt 的近似重复度。用于识别「重发」。

    为什么需要（v0.4）
    ------------------
    撞到额度上限时那一轮会失败, 用户通常原样再发一次。这些重发在 JSONL 里
    是独立条目, 会被当成独立轮次, 于是:
      * 轮次总数虚高（实测本机 1066 轮里约 3.8% 是重发）;
      * 相似度矩阵里出现相似度 1.0 的相邻对, 干扰线路划分;
      * 「这轮之前没有 AI 回复」不再能推出「用户自发连问」——
        很可能只是上一轮没跑成。
    因此必须识别并折叠, 且不能再用「无 AI 回复」当作自发的证据。
    """
    a, b = " ".join(a.split()), " ".join(b.split())
    if a == b:
        return 1.0
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    jac = len(sa & sb) / len(sa | sb)
    lenr = min(len(a), len(b)) / max(len(a), len(b))
    return jac * lenr


RETRY_SIM = 0.85


def extract(path: Path) -> list[dict]:
    """返回按时间排列的 prompt 记录列表。"""
    out = []
    with path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") != "user":
                continue
            msg = d.get("message")
            if not isinstance(msg, dict):
                continue
            text = human_text(msg)
            if not text:
                continue
            text = text.strip()
            if not text:
                continue
            out.append({
                "index": len(out) + 1,
                "line": lineno,
                # Claude Code 的 JSONL 通常带 uuid。旧记录没有 uuid 时退回
                # 原始行号；两者都比下游重新编号更适合做来源锚点。
                "source_id": str(d.get("uuid") or msg.get("id") or lineno),
                "timestamp": d.get("timestamp", ""),
                "chars": len(text),
                "suspect_auto": is_auto(text),
                "text": text,
                "retry_of": None,     # 下面回填
            })

    # 标记重发: 与紧邻的上一条真人 prompt 高度重复
    prev_i = None
    for i, cur in enumerate(out):
        if cur["suspect_auto"]:
            continue
        if prev_i is not None:
            sim = _near_dup(out[prev_i]["text"], cur["text"])
            if sim >= RETRY_SIM:
                cur["retry_of"] = out[prev_i]["index"]
                cur["retry_sim"] = round(sim, 3)
        prev_i = i
    return out


def session_meta(path: Path) -> dict:
    """快速提取会话元信息: 标题、真实工作目录、真人 prompt 数、首个 prompt。

    项目文件夹名会把非 ASCII 字符（如中文）压成横杠, 因此不可读;
    真实路径要从条目里的 cwd 字段取。标题存在 aiTitle / customTitle 里。

    为了在几十个大文件上仍然快, 先用字符串包含做粗筛, 再 json 解析。
    """
    meta = {"ai_title": None, "custom_title": None, "cwd": None,
            "n_prompts": 0, "first_prompt": None}
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return meta
    with fh:
        for line in fh:
            # 粗筛: 避免对每行都做 json 解析（有些行是几十 KB 的工具返回）
            has_title = '"ai-title"' in line or '"custom-title"' in line
            has_user = '"type":"user"' in line or '"type": "user"' in line
            if not (has_title or has_user or meta["cwd"] is None):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = d.get("type")
            if t == "ai-title":
                meta["ai_title"] = d.get("aiTitle") or meta["ai_title"]
            elif t == "custom-title":
                meta["custom_title"] = d.get("customTitle") or meta["custom_title"]
            if meta["cwd"] is None and d.get("cwd"):
                meta["cwd"] = d["cwd"]
            if t == "user":
                msg = d.get("message")
                if isinstance(msg, dict):
                    txt = human_text(msg)
                    if txt:
                        txt = txt.strip()
                        if txt and not is_auto(txt):
                            meta["n_prompts"] += 1
                            if meta["first_prompt"] is None:
                                meta["first_prompt"] = " ".join(txt.split())[:70]
    return meta


def list_sessions(min_prompts: int = 0) -> None:
    import datetime

    if not PROJECTS_DIR.is_dir():
        print(f"未找到 {PROJECTS_DIR}", file=sys.stderr)
        return

    files = []
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            files.append((f.stat().st_mtime, f.stat().st_size, f))
        except OSError:
            continue
    files.sort(reverse=True)

    print(f"扫描 {len(files)} 个会话…\n")
    shown = 0
    for mtime, size, f in files:
        meta = session_meta(f)
        if meta["n_prompts"] < min_prompts:
            continue
        shown += 1
        ts = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")
        title = meta["custom_title"] or meta["ai_title"] or "(无标题)"
        cwd = meta["cwd"] or f"(未知, 文件夹名: {f.parent.name})"
        # 把 home 缩成 ~ 便于阅读
        cwd = cwd.replace(str(Path.home()), "~")
        print(f"{title}")
        print(f"   {ts}   真人轮次 {meta['n_prompts']:>3}   {size/1024:>6.0f}K   {cwd}")
        if meta["first_prompt"]:
            print(f"   首问: {meta['first_prompt']}")
        print(f"   {f}")
        print()
    print(f"共显示 {shown} 个" + (f"（已过滤真人轮次 < {min_prompts} 的会话）"
                                  if min_prompts else ""))


def resolve_session(token: str | None, *, last: bool = False) -> Path | None:
    """把用户给的东西解析成一个 jsonl 路径。

    P0-2: SKILL.md 承诺了无参列出、--last、session-id 解析, 但脚本原先
    只接受真实路径。文档即接口, 不一致会让 agent 在第一步就卡住。

    支持: 完整路径 / session-id 前缀 / --last（按修改时间）。
    无法唯一确定时返回 None 并打印候选, 不猜。
    """
    if last:
        files = sorted(PROJECTS_DIR.rglob("*.jsonl"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        return files[0] if files else None

    if not token:
        return None

    p = Path(token).expanduser()
    if p.exists():
        return p

    # 当作 session-id（或其前缀）来找
    cands = [f for f in PROJECTS_DIR.rglob("*.jsonl")
             if f.stem.startswith(token)]
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        print(f"session-id 「{token}」匹配到 {len(cands)} 个会话，请写更长的前缀：",
              file=sys.stderr)
        for f in cands[:10]:
            m = session_meta(f)
            title = m["custom_title"] or m["ai_title"] or "(无标题)"
            print(f"  {f.stem}  {title}", file=sys.stderr)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session", nargs="?",
                    help="会话 jsonl 路径，或 session-id（可用前缀）")
    ap.add_argument("--source", choices=("claude", "codex"), default="claude",
                    help="输入来源（默认 claude，旧命令保持兼容）")
    ap.add_argument("--json", dest="json_out", help="把结果写成 JSON")
    ap.add_argument("--list", action="store_true", help="列出本机所有会话记录")
    ap.add_argument("--last", action="store_true",
                    help="选用最近修改的那个会话")
    ap.add_argument("--min-prompts", type=int, default=0,
                    help="配合 --list: 只显示真人轮次 >= N 的会话")
    ap.add_argument("--limit", type=int, default=50,
                    help="Codex --list 最多检查多少个最近任务（默认 50）")
    ap.add_argument("--all", action="store_true",
                    help="连 harness 自动生成的条目一起显示")
    args = ap.parse_args()

    if args.source == "codex":
        # 延迟导入，避免 conversation_sources 反向复用本模块时形成循环。
        from conversation_sources import (
            ConversationSourceError, load_conversation,
            list_codex_conversations,
        )
        if args.list or (not args.session and not args.last):
            try:
                list_codex_conversations(
                    min_prompts=max(0, args.min_prompts),
                    limit=max(1, args.limit),
                )
            except ConversationSourceError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            return 0
        try:
            conversation = load_conversation(
                "codex", args.session, last=args.last
            )
        except ConversationSourceError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        prompts = conversation.prompts
        source_label = conversation.locator
        source_name = conversation.source_id
    elif args.list:
        list_sessions(min_prompts=args.min_prompts)
        return 0
    elif not args.session and not args.last:
        # 无参数 = 列出候选会话供选择（SKILL.md 承诺的行为）
        print("未指定会话。以下是本机较长的会话，挑一个把路径或 session-id 传进来：\n")
        list_sessions(min_prompts=max(6, args.min_prompts))
        return 0
    else:
        path = resolve_session(args.session, last=args.last)
        if path is None:
            if args.session:
                print(f"无法唯一确定会话: {args.session}", file=sys.stderr)
            else:
                print("没有找到任何会话记录", file=sys.stderr)
            return 1
        prompts = extract(path)
        source_label = str(path)
        source_name = path.name
    real = [p for p in prompts if not p["suspect_auto"]]

    print(f"会话           : {source_name}")
    print(f"user 条目      : {len(prompts)}")
    print(f"其中疑似自动生成: {len(prompts) - len(real)}")
    print(f"判定为真人输入  : {len(real)}")
    print()

    show = prompts if args.all else real
    for p in show:
        flag = "  [auto?]" if p["suspect_auto"] else ""
        one = " ".join(p["text"].split())
        print(f'[{p["index"]:02d}] {p["timestamp"][:19]}  {p["chars"]:>6}字{flag}')
        print(f'     {one[:100]}')
        print()

    if args.json_out:
        payload = {
            "source": source_label,
            "total_user_entries": len(prompts),
            "human_prompts": real,
            "all_entries": prompts,
        }
        Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写出: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

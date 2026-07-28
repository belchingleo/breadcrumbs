"""把 Claude Code 与 Codex 会话归一化为 Breadcrumbs 的输入契约。"""
from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_source import (AppServerClient, CodexSourceError, choose_thread,
                          normalize_thread)
from extract_prompts import (PROJECTS_DIR, RETRY_SIM, _near_dup, extract,
                             human_text, is_auto, resolve_session, session_meta)


class ConversationSourceError(RuntimeError):
    """输入来源不可用或会话无法唯一确定。"""


@dataclass
class Conversation:
    source: str
    source_id: str
    locator: str
    title: str
    cwd: str | None
    prompts: list[dict]
    stream: list[tuple]
    updated_at: str = ""
    raw_meta: dict[str, Any] | None = None


def finalize_prompts(raw: list[dict]) -> list[dict]:
    """补齐两个来源共同使用的索引、自动标记与重发关系。"""
    prompts = []
    for item in raw:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        prompts.append({
            **item,
            "index": len(prompts) + 1,
            "chars": len(text),
            "suspect_auto": bool(item.get("suspect_auto", is_auto(text))),
            "text": text,
            "retry_of": None,
        })

    prev_i = None
    for i, current in enumerate(prompts):
        if current["suspect_auto"]:
            continue
        if prev_i is not None:
            similarity = _near_dup(prompts[prev_i]["text"], current["text"])
            if similarity >= RETRY_SIM:
                current["retry_of"] = prompts[prev_i]["index"]
                current["retry_sim"] = round(similarity, 3)
        prev_i = i
    return prompts


def collapsed_prompts(prompts: list[dict]) -> list[dict]:
    """与 analyze.py 一致：过滤自动条目并保留重发中的最后一条。"""
    kept = [prompt for prompt in prompts if not prompt["suspect_auto"]]
    result = []
    for index, prompt in enumerate(kept):
        nxt = kept[index + 1] if index + 1 < len(kept) else None
        if nxt is not None and nxt.get("retry_of") == prompt["index"]:
            continue
        result.append(prompt)
    return result


def stream_for_prompts(stream: list[tuple], prompts: list[dict]) -> list[tuple]:
    """让消息流与过滤、折叠后的 prompt 使用完全相同的轮次定义。"""
    allowed = {
        str(prompt.get("source_id"))
        for prompt in collapsed_prompts(prompts)
    }
    result = []
    keep_reply = False
    for kind, text, source_id in stream:
        if kind == "human":
            keep_reply = str(source_id) in allowed
            if keep_reply:
                result.append((kind, text, source_id))
        elif kind == "assistant" and keep_reply:
            result.append((kind, text, source_id))
    return result


def _claude_stream(path: Path, prompts: list[dict]) -> list[tuple]:
    dropped_lines = set()
    kept = [prompt for prompt in prompts if not prompt["suspect_auto"]]
    for index, prompt in enumerate(kept):
        nxt = kept[index + 1] if index + 1 < len(kept) else None
        if nxt is not None and nxt.get("retry_of") == prompt["index"]:
            dropped_lines.add(prompt["line"])

    stream = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            if row.get("type") == "user":
                text = human_text(message)
                if (text and text.strip() and not is_auto(text.strip())
                        and line_number not in dropped_lines):
                    stream.append(("human", text.strip(), line_number))
            elif row.get("type") == "assistant":
                content = message.get("content")
                parts = []
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    parts.extend(
                        block.get("text", "")
                        for block in content
                        if isinstance(block, dict) and block.get("type") == "text"
                    )
                text = "\n".join(part for part in parts if part).strip()
                if text:
                    stream.append(("assistant", text, line_number))
    return stream


def load_conversation(
    source: str,
    token: str | Path | None,
    *,
    last: bool = False,
) -> Conversation:
    if source == "claude":
        path = resolve_session(str(token) if token is not None else None, last=last)
        if path is None:
            raise ConversationSourceError(
                "无法确定 Claude Code 会话；请用 --list 查看候选，或加 --last"
            )
        meta = session_meta(path)
        prompts = extract(path)
        return Conversation(
            source="claude",
            source_id=path.stem,
            locator=str(path),
            title=meta["custom_title"] or meta["ai_title"] or path.stem[:8],
            cwd=meta["cwd"],
            prompts=prompts,
            stream=_claude_stream(path, prompts),
            updated_at=datetime.datetime.fromtimestamp(
                path.stat().st_mtime
            ).astimezone().isoformat(),
            raw_meta=meta,
        )

    if source == "codex":
        try:
            with AppServerClient() as client:
                thread = choose_thread(
                    client, str(token) if token is not None else None, last=last
                )
        except CodexSourceError as exc:
            raise ConversationSourceError(str(exc)) from exc
        raw_prompts, stream = normalize_thread(thread)
        prompts = finalize_prompts(raw_prompts)
        thread_id = str(thread.get("id") or token or "")
        preview = str(thread.get("preview") or "").strip()
        title = str(thread.get("name") or "").strip() or preview[:70]
        return Conversation(
            source="codex",
            source_id=thread_id,
            locator=f"codex:{thread_id}",
            title=title or thread_id[:8],
            cwd=thread.get("cwd"),
            prompts=prompts,
            stream=stream_for_prompts(stream, prompts),
            updated_at=str(thread.get("updatedAt") or ""),
            raw_meta=thread,
        )

    raise ConversationSourceError(
        f"未知来源「{source}」；目前支持 claude、codex"
    )


def list_codex_conversations(
    *,
    min_prompts: int = 0,
    limit: int = 50,
) -> int:
    """打印 Codex 任务候选；只有需要筛轮次时才读取完整 turns。"""
    try:
        with AppServerClient() as client:
            rows = client.list_threads(limit=limit)
            shown = 0
            print(f"扫描最近 {len(rows)} 个 Codex 任务…\n")
            for row in rows:
                prompts = None
                first_prompt = str(row.get("preview") or "").strip()
                if min_prompts:
                    thread = client.read_thread(str(row.get("id")))
                    raw, _ = normalize_thread(thread)
                    prompts = collapsed_prompts(finalize_prompts(raw))
                    if len(prompts) < min_prompts:
                        continue
                    if prompts:
                        first_prompt = prompts[0]["text"]
                shown += 1
                stamp = row.get("updatedAt") or row.get("createdAt")
                when = (
                    datetime.datetime.fromtimestamp(stamp).strftime("%m-%d %H:%M")
                    if isinstance(stamp, (int, float)) else "(时间未知)"
                )
                title = row.get("name") or row.get("preview") or "(无标题)"
                cwd = str(row.get("cwd") or "(工作目录未知)")
                cwd = cwd.replace(str(Path.home()), "~")
                count = f"真人输入 {len(prompts):>3}" if prompts is not None else ""
                print(title)
                print(f"   {when}   {count}   {cwd}".rstrip())
                if first_prompt:
                    print(f"   首问: {' '.join(first_prompt.split())[:70]}")
                print(f"   codex:{row.get('id')}")
                print()
            suffix = (
                f"（已过滤真人输入 < {min_prompts} 的任务）"
                if min_prompts else ""
            )
            print(f"共显示 {shown} 个{suffix}")
            return shown
    except CodexSourceError as exc:
        raise ConversationSourceError(str(exc)) from exc


def collect_corpus_texts(source: str, limit_sessions: int = 200) -> list[str]:
    """从同一来源汇总真人输入，避免跨产品 harness 文本污染 IDF。"""
    texts = []
    if source == "claude":
        if not PROJECTS_DIR.is_dir():
            return texts
        files = sorted(
            PROJECTS_DIR.rglob("*.jsonl"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[:limit_sessions]:
            try:
                entries = collapsed_prompts(extract(path))
            except OSError:
                continue
            texts.extend(
                prompt["text"] for prompt in entries
                if len(prompt["text"]) >= 20
            )
        return texts

    if source == "codex":
        try:
            with AppServerClient() as client:
                for row in client.list_threads(limit=limit_sessions):
                    try:
                        thread = client.read_thread(str(row.get("id")))
                    except CodexSourceError:
                        continue
                    raw, _ = normalize_thread(thread)
                    entries = collapsed_prompts(finalize_prompts(raw))
                    texts.extend(
                        prompt["text"] for prompt in entries
                        if len(prompt["text"]) >= 20
                    )
        except CodexSourceError as exc:
            raise ConversationSourceError(str(exc)) from exc
        return texts

    raise ConversationSourceError(f"未知来源「{source}」")

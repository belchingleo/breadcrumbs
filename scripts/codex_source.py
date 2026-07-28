"""Codex App Server 的只读会话适配器。

不直接解析 ``~/.codex/sessions``。官方说明原始 transcript 格式不是稳定
接口；这里通过稳定的 ``thread/list`` 与 ``thread/read`` 获取会话。
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any


class CodexSourceError(RuntimeError):
    """Codex 会话无法列出、读取或归一化。"""


class AppServerClient:
    """最小 JSONL 客户端；只实现 Breadcrumbs 所需的只读请求。"""

    def __init__(self, binary: str | None = None, timeout: float = 20.0):
        self.binary = binary or os.environ.get("BREADCRUMBS_CODEX_BIN")
        self.binary = self.binary or shutil.which("codex")
        self.timeout = timeout
        self.proc: subprocess.Popen[str] | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.stderr_tail: deque[str] = deque(maxlen=30)
        self._request_id = 0

    def __enter__(self) -> "AppServerClient":
        if not self.binary:
            raise CodexSourceError(
                "未找到 codex 命令。请先安装或更新 Codex CLI，"
                "也可用 BREADCRUMBS_CODEX_BIN 指定可执行文件。"
            )
        try:
            self.proc = subprocess.Popen(
                [self.binary, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise CodexSourceError(f"无法启动 Codex App Server：{exc}") from exc

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request("initialize", {
            "clientInfo": {
                "name": "breadcrumbs",
                "title": "Breadcrumbs",
                "version": "0.5.0",
            }
        })
        self.notify("initialized", {})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.proc is None:
            return
        if self.proc.stdin:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=2)

    def _read_stdout(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.messages.put(message)

    def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        for line in self.proc.stderr:
            line = line.strip()
            if line:
                self.stderr_tail.append(line)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise CodexSourceError("Codex App Server 尚未启动")
        if self.proc.poll() is not None:
            raise CodexSourceError(self._process_error())
        try:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexSourceError(self._process_error()) from exc

    def _process_error(self) -> str:
        details = "\n".join(self.stderr_tail)
        base = "Codex App Server 已退出，无法读取任务记录。"
        if "failed to initialize sqlite state runtime" in details:
            base += (
                " 当前沙箱没有读取 Codex 本地状态所需的权限；"
                "请允许这次只读会话访问后重试。"
            )
        return f"{base}\n{details}" if details else base

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexSourceError(
                    f"Codex App Server 响应超时（{method}，{self.timeout:g} 秒）"
                )
            try:
                message = self.messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                if self.proc is not None and self.proc.poll() is not None:
                    raise CodexSourceError(self._process_error())
                continue
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else error
                raise CodexSourceError(f"{method} 失败：{detail}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexSourceError(f"{method} 返回了无法识别的结果")
            return result

    def list_threads(self, limit: int = 50) -> list[dict[str, Any]]:
        """按最近活动时间列出用户可见的交互任务，不混入 sub-agent。"""
        data: list[dict[str, Any]] = []
        cursor = None
        while len(data) < limit:
            page_size = min(100, limit - len(data))
            params: dict[str, Any] = {
                "limit": page_size,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "sourceKinds": ["cli", "vscode", "appServer"],
            }
            if cursor:
                params["cursor"] = cursor
            result = self.request("thread/list", params)
            rows = result.get("data")
            if not isinstance(rows, list):
                raise CodexSourceError("thread/list 缺少 data 列表")
            data.extend(row for row in rows if isinstance(row, dict))
            cursor = result.get("nextCursor")
            if not cursor or not rows:
                break
        return data[:limit]

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        result = self.request("thread/read", {
            "threadId": thread_id,
            "includeTurns": True,
        })
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise CodexSourceError("thread/read 缺少 thread 对象")
        return thread


def epoch_iso(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return ""
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()


def clean_user_text(text: str) -> str:
    """去掉 Codex 桌面端生成的附件清单外壳，保留用户真正的请求。

    只处理带有固定 ``## My request for Codex:`` 标记且从附件标题开始的
    内容，避免把用户自己写的 Markdown 标题误删。
    """
    stripped = text.strip()
    marker = "## My request for Codex:"
    if stripped.startswith("# Files mentioned by the user:") and marker in stripped:
        request = stripped.split(marker, 1)[1].strip()
        if request:
            return request
    return stripped


def normalize_thread(thread: dict[str, Any]) -> tuple[list[dict], list[tuple]]:
    """把 App Server thread 映射为 Breadcrumbs 的 prompt 与消息流契约。"""
    prompts: list[dict] = []
    stream: list[tuple] = []
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return prompts, stream

    line_index = 0
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        timestamp = epoch_iso(turn.get("startedAt"))
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                text = clean_user_text(content_text(item.get("content")))
                if not text:
                    continue
                line_index += 1
                source_id = str(item.get("id") or item.get("clientId")
                                or turn.get("id") or line_index)
                prompts.append({
                    "line": line_index,
                    "source_id": source_id,
                    "timestamp": timestamp,
                    "text": text,
                })
                stream.append(("human", text, source_id))
            elif item_type == "agentMessage":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    stream.append(("assistant", text.strip(),
                                   str(item.get("id") or turn.get("id") or "")))
    return prompts, stream


def choose_thread(
    client: AppServerClient,
    token: str | None,
    *,
    last: bool = False,
    search_limit: int = 500,
) -> dict[str, Any]:
    """按完整 ID、唯一前缀或最近任务选择 thread。"""
    if last:
        rows = client.list_threads(limit=1)
        if not rows:
            raise CodexSourceError("没有找到可用的 Codex 任务")
        return client.read_thread(str(rows[0]["id"]))

    if not token:
        raise CodexSourceError("未指定 Codex task id；可先用 --list 查看候选")
    token = token.removeprefix("codex:")

    # UUID / 完整 thread id 直接读取，避免为了一个任务扫描全部记录。
    if len(token) >= 32:
        return client.read_thread(token)

    rows = client.list_threads(limit=search_limit)
    matches = [row for row in rows if str(row.get("id", "")).startswith(token)]
    if not matches:
        raise CodexSourceError(f"没有找到 id 以「{token}」开头的 Codex 任务")
    if len(matches) > 1:
        sample = "\n".join(
            f"  {row.get('id')}  {row.get('name') or row.get('preview') or '(无标题)'}"
            for row in matches[:10]
        )
        raise CodexSourceError(
            f"「{token}」匹配到 {len(matches)} 个 Codex 任务，请写更长的前缀：\n"
            f"{sample}"
        )
    return client.read_thread(str(matches[0]["id"]))

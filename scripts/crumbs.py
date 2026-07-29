"""Breadcrumbs 轻量闭环核查：结构定位、独立核验与逐项对账。

脚本把 Claude Code / Codex 会话裁成有限证据包，调用隔离的小模型封闭核验，
再把开放回路变成可逐项裁决的问题；全部裁决后生成对账总结和下一步：

    python3 crumbs.py prepare <session> -o /tmp/crumbs.json --agent-view
    python3 crumbs.py evaluate /tmp/crumbs.agent.json --host codex
    python3 crumbs.py present /tmp/crumbs.agent.json verdicts.json --ask
    python3 crumbs.py decide interaction.json --decision process --ask

设计边界：
* 用户提示词是任务契约，AI 回复是履约证据。
* 结构规则只找候选，不直接断言「仍未完成」。
* evaluator 只能依据随候选提供的证据判断 closed/open/uncertain。
* 用户沉默不能被解释为放弃；意图未知时必须询问。
* /crumbs 不生成 HTML，不重建完整思路图；完整复盘属于 /bread。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).parent))
from conversation_sources import (Conversation, ConversationSourceError,  # noqa: E402
                                  collapsed_prompts, load_conversation)
import identity  # noqa: E402


SCHEMA_VERSION = 1
DEFAULT_MAX_CANDIDATES = 6
DEFAULT_MAX_EVIDENCE = 2
# 证据包要由小模型核验，输入体积比候选召回更稀缺。source 与 direct reply
# 保留一段完整语义，后续证据只保留足以辨认响应状态的首尾。
SOURCE_CHARS = 340
REPLY_CHARS = 440
EVIDENCE_CHARS = 230
INDEX_USER_CHARS = 90
INDEX_ASSISTANT_CHARS = 130
PER_TYPE_LIMIT = 1
CLAUDE_EVALUATOR_MODEL = "haiku"
CLAUDE_EVALUATOR_FALLBACK = "sonnet"
CODEX_EVALUATOR_MODELS = ("gpt-5.6-luna", "gpt-5.6-terra")
EVALUATOR_EFFORT = "low"
STATE_VERSION = 2
MAX_STATE_SESSIONS = 100
MAX_ITEMS_PER_SESSION = 200
DECISION_TO_STATUS = {
    "process": "queued",
    "resolved": "resolved",
    "ignore": "ignored",
}
TERMINAL_DECISION_STATUSES = frozenset(DECISION_TO_STATUS.values())
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
EVALUATOR_CONTRACT_PATH = PACKAGE_ROOT / "references" / "crumbs-evaluator.md"
VERDICT_SCHEMA_PATH = (
    PACKAGE_ROOT / "references" / "crumbs-verdict.schema.json"
)

_QUESTION = re.compile(
    r"[？?]|(?:是否|能否|可否|怎么|怎样|为什么|为何|什么|哪里|哪个|哪些)|"
    r"\b(?:who|what|when|where|why|how)\b",
    re.I,
)
_REQUEST = re.compile(
    r"(?:请|帮我|需要你|麻烦|务必|记得|检查|修改|增加|新增|删除|确认|核实|"
    r"比较|解释|告诉我|看看|处理|提交|生成|实现|安装|发布|验证|"
    r"\b(?:please|check|fix|add|remove|confirm|verify|compare|explain|"
    r"implement|install|publish|generate)\b)",
    re.I,
)
_ASSISTANT_REQUEST = re.compile(
    r"(?:请(?:你)?(?:确认|选择|提供|执行|运行|决定|告诉我)|"
    r"需要你(?:确认|选择|提供|执行|运行|决定|补充)|"
    r"完成后(?:告诉我|发给我)|"
    r"你先.{0,30}(?:告诉我|发给我)|"
    r"如果你同意|你希望(?:选择|采用|继续)|请选择|"
    r"before I continue|please (?:confirm|choose|provide|run|decide))",
    re.I | re.S,
)
_PROMISE = re.compile(
    r"(?:我(?:会|将|接下来会|随后会|之后会|稍后会)|"
    r"让我先.{0,40}再|接下来我(?:来|先)|"
    r"I(?:'ll| will) |I am going to |let me .{0,40} then )",
    re.I | re.S,
)
_ASSUMPTION = re.compile(
    r"(?:我假设|暂时假设|暂按|默认你|我理解为|如果我理解得对|"
    r"按.{0,20}理解|I assume|assuming that|if I understand correctly)",
    re.I | re.S,
)
_COMPLETION = re.compile(
    r"(?:已(?:经)?(?:完成|处理|修改|修复|实现|提交|发布|验证|检查)|"
    r"全部(?:完成|处理|修改|修复)|"
    r"(?:done|completed|fixed|implemented|verified|published)\b)",
    re.I,
)
_DEFER = re.compile(
    r"(?:先放着|先搁置|稍后再|待会(?:儿)?再|以后再|之后再|"
    r"这个先不(?:做|谈|处理|决定)|暂时不(?:做|谈|处理|决定)|"
    r"leave (?:it|this) for later|come back to (?:it|this) later)",
    re.I,
)
_REJECT = re.compile(
    r"(?:这个不行|这个不好|不喜欢(?:这个|这种)|不要这个|"
    r"不采用|不接受|方案.{0,12}不行|先不要|"
    r"this (?:doesn't|does not) work|I don't want this|reject this)",
    re.I | re.S,
)
_POLITE_CLOSER = re.compile(
    r"(?:还有别的需要吗|还需要我做什么吗|需要我继续吗|"
    r"anything else|would you like anything else)\s*[？?]?\s*$",
    re.I,
)
_LIST_ITEM = re.compile(
    r"^\s*(?:\d{1,2}[.)、．]|[-*•])\s*(.+?)\s*$",
    re.M,
)

TYPE_SCORES = {
    "clarification_handoff": 100,
    "explicit_assumption": 92,
    "assistant_request": 88,
    "assistant_promise": 82,
    "user_deferred": 76,
    "multi_request": 72,
    "completion_claim": 68,
    "user_rejection": 64,
    "user_request": 44,
}

TYPE_LABELS = {
    "clarification_handoff": "原问题可能被澄清过程顶掉",
    "explicit_assumption": "未经确认的显式前提",
    "assistant_request": "等待你的确认、选择或行动",
    "assistant_promise": "AI 作出的后续承诺",
    "user_deferred": "你主动挂起的事项",
    "multi_request": "一次输入中的多项要求",
    "completion_claim": "需要核对证据的完成声明",
    "user_rejection": "被否定但可能尚无替代的方案",
    "user_request": "需要核对是否闭环的请求",
}

OWNER_BY_TYPE = {
    "clarification_handoff": "assistant",
    "explicit_assumption": "shared",
    "assistant_request": "user",
    "assistant_promise": "assistant",
    "user_deferred": "user",
    "multi_request": "assistant",
    "completion_claim": "assistant",
    "user_rejection": "shared",
    "user_request": "assistant",
}

QUESTION_BY_TYPE = {
    "clarification_handoff": "原来的问题现在要处理、已处理，还是忽略？",
    "explicit_assumption": "这个未经确认的前提现在要处理、已处理，还是忽略？",
    "assistant_request": "这件等待你回应的事现在要处理、已处理，还是忽略？",
    "assistant_promise": "这项尚未兑现的承诺现在要处理、已处理，还是忽略？",
    "user_deferred": "这件被搁置的事现在要处理、已处理，还是忽略？",
    "multi_request": "缺少的部分现在要处理、已处理，还是忽略？",
    "completion_claim": "这项完成声明现在要处理、已处理，还是忽略？",
    "user_rejection": "原需求现在要处理、已处理，还是忽略？",
    "user_request": "这件尚未闭环的事现在要处理、已处理，还是忽略？",
}

DEFAULT_OPTIONS = [
    ("process", "处理"),
    ("resolved", "已处理"),
    ("ignore", "忽略"),
]
OPTION_DESCRIPTIONS = {
    "process": "加入本轮行动，全部裁决后优先执行",
    "resolved": "确认此前已经处理，不再追问",
    "ignore": "主动关闭，后续不再提醒",
}


@dataclass
class Round:
    turn: int
    prompt_anchor: str
    user: str
    assistant: str


def clip(text: str, limit: int) -> str:
    """保留首尾；完成证据经常出现在回复末尾。"""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    marker = " … "
    room = max(0, limit - len(marker))
    head = round(room * 0.62)
    return text[:head].rstrip() + marker + text[-(room - head):].lstrip()


def rounds_from_conversation(conversation: Conversation) -> list[Round]:
    """把归一化消息流组装成真人轮次及其直接 AI 回复。"""
    prompts = collapsed_prompts(conversation.prompts)
    by_source = {}
    for prompt in prompts:
        # Codex 消息流使用 item id；Claude JSONL 消息流使用行号。
        # 两种键都登记，避免把同一输入适配器的实现细节带到上层。
        by_source[str(prompt.get("source_id"))] = prompt
        if prompt.get("line") is not None:
            by_source[str(prompt.get("line"))] = prompt
    rounds: list[Round] = []
    active: Round | None = None
    for kind, text, source_id in conversation.stream:
        if kind == "human":
            prompt = by_source.get(str(source_id))
            if prompt is None:
                continue
            active = Round(
                turn=len(rounds) + 1,
                prompt_anchor=identity.prompt_anchor(
                    prompt["text"], prompt.get("timestamp", ""),
                    prompt.get("source_id", ""),
                ),
                user=prompt["text"],
                assistant="",
            )
            rounds.append(active)
        elif kind == "assistant" and active is not None:
            active.assistant = (
                f"{active.assistant}\n\n{text}".strip()
                if active.assistant else str(text).strip()
            )
    return rounds


def _request_units(text: str) -> list[str]:
    """提取可枚举的请求单元；只用于找候选，不用于判断完成度。"""
    units = [clip(match, 180) for match in _LIST_ITEM.findall(text)]
    if len(units) >= 2:
        return units

    parts = re.split(r"(?<=[？?。；;])|\n+", text)
    found = [
        clip(part, 180) for part in parts
        if part.strip() and (_QUESTION.search(part) or _REQUEST.search(part))
    ]
    # 同一句里用「另外/同时/以及/并且」连接时，保留为两个待核对单元。
    if len(found) < 2:
        clauses = re.split(r"(?:另外|同时|以及|并且|还有|, and |; and )", text)
        found = [
            clip(part, 180) for part in clauses
            if part.strip() and (_QUESTION.search(part) or _REQUEST.search(part))
        ]
    return found


def _overlap(a: str, b: str) -> float:
    """零依赖字符二元组重叠，仅用于挑回读片段，不承担语义结论。"""
    def grams(value: str) -> set[str]:
        value = re.sub(r"\s+", "", value.lower())[:3000]
        return {value[i:i + 2] for i in range(max(0, len(value) - 1))}

    left, right = grams(a), grams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / math.sqrt(len(left) * len(right))


def _session_key(conversation: Conversation) -> str:
    """稳定标识同一窗口，不随新增消息变化，也不保存标题或正文。"""
    raw = "\0".join((
        conversation.source,
        str(conversation.source_id or conversation.locator),
    ))
    return "session-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _candidate_id(
    session_key: str,
    candidate_type: str,
    prompt_anchor: str,
) -> str:
    raw = "\0".join((
        session_key, candidate_type, prompt_anchor,
    ))
    return "crumb-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _is_crumbs_invocation(text: str) -> bool:
    """工具自己的调用轮次与追问不参与候选检测。"""
    value = str(text or "").strip()
    if re.match(
        r"^(?:(?:/|\$)crumbs)\b", value, re.I,
    ):
        return True
    if re.match(
        r"^(?:请)?(?:运行|调用|使用|用)?\s*crumbs\b", value, re.I,
    ):
        return True
    return bool(re.search(
        r"(?:还有什么没(?:说完|被接住)|"
        r"AI\s*有没有漏答漏做|"
        r"有哪些待确认(?:的)?选择|"
        r"有哪些未经确认的前提)",
        value, re.I,
    ))


def _is_substantive_user_request(text: str) -> bool:
    stripped = text.strip()
    if (
        not stripped
        or _is_crumbs_invocation(stripped)
        or re.match(r"^\s*(?:(?:/|\$)bread)\b", stripped, re.I)
    ):
        return False
    return bool(_QUESTION.search(stripped) or _REQUEST.search(stripped))


def _add_candidate(
    target: list[dict],
    seen: set[tuple[str, int]],
    *,
    session_key: str,
    candidate_type: str,
    round_: Round,
    source_role: str,
    source_text: str,
    signals: Iterable[str],
    request_units: list[str] | None = None,
    score_bonus: int = 0,
) -> None:
    key = (candidate_type, round_.turn)
    if key in seen:
        return
    seen.add(key)
    target.append({
        "id": _candidate_id(
            session_key, candidate_type, round_.prompt_anchor,
        ),
        "type": candidate_type,
        "label": TYPE_LABELS[candidate_type],
        "owner_hint": OWNER_BY_TYPE[candidate_type],
        "score": TYPE_SCORES[candidate_type] + score_bonus,
        "source_turn": round_.turn,
        "source_role": source_role,
        "prompt_anchor": round_.prompt_anchor,
        "source_quote": clip(source_text, SOURCE_CHARS),
        # assistant 自己就是言语行为来源时，不重复保存同一段为 direct_reply。
        "direct_reply": (
            clip(round_.assistant, REPLY_CHARS)
            if source_role == "user" else ""
        ),
        "signals": list(signals),
        **({"request_units": request_units} if request_units else {}),
    })


def detect_candidates(
    conversation: Conversation,
    *,
    max_candidates: int | None = DEFAULT_MAX_CANDIDATES,
    max_evidence: int = DEFAULT_MAX_EVIDENCE,
    excluded_ids: set[str] | None = None,
    priority_ids: set[str] | None = None,
) -> dict:
    """找高价值开放回路候选，并为每项裁出有限后续证据。"""
    rounds = rounds_from_conversation(conversation)
    analysis_rounds = [
        round_ for round_ in rounds
        if not _is_crumbs_invocation(round_.user)
    ]
    texts = [round_.user for round_ in rounds]
    session_fingerprint = identity.session_fingerprint(texts)
    session_key = _session_key(conversation)
    excluded_ids = excluded_ids or set()
    priority_ids = priority_ids or set()
    candidates: list[dict] = []
    seen: set[tuple[str, int]] = set()

    for index, round_ in enumerate(analysis_rounds):
        user, assistant = round_.user, round_.assistant
        units = _request_units(user)

        # 最特异的三步模式：用户问 A → AI 反问 B → 用户回答 B。
        if (
            _is_substantive_user_request(user)
            and _ASSISTANT_REQUEST.search(assistant)
            and index + 1 < len(analysis_rounds)
        ):
            _add_candidate(
                candidates, seen,
                session_key=session_key,
                candidate_type="clarification_handoff",
                round_=round_, source_role="user", source_text=user,
                signals=("用户提出请求", "AI 转而请求澄清", "后续存在用户回应"),
                score_bonus=min(8, len(units) * 2),
            )

        if len(units) >= 2:
            _add_candidate(
                candidates, seen,
                session_key=session_key,
                candidate_type="multi_request",
                round_=round_, source_role="user", source_text=user,
                signals=(f"检测到 {len(units)} 个可枚举请求单元",),
                request_units=units[:8],
                score_bonus=min(8, len(units)),
            )
        elif _is_substantive_user_request(user):
            _add_candidate(
                candidates, seen,
                session_key=session_key,
                candidate_type="user_request",
                round_=round_, source_role="user", source_text=user,
                signals=("用户提出问题或行动要求",),
                score_bonus=4 if _QUESTION.search(user) else 0,
            )

        if _DEFER.search(user):
            _add_candidate(
                candidates, seen,
                session_key=session_key,
                candidate_type="user_deferred",
                round_=round_, source_role="user", source_text=user,
                signals=("用户明确表示稍后再处理",),
            )
        if _REJECT.search(user):
            _add_candidate(
                candidates, seen,
                session_key=session_key,
                candidate_type="user_rejection",
                round_=round_, source_role="user", source_text=user,
                signals=("用户否定当前方案",),
            )

        if assistant and not _POLITE_CLOSER.search(assistant):
            if _ASSISTANT_REQUEST.search(assistant):
                _add_candidate(
                    candidates, seen,
                    session_key=session_key,
                    candidate_type="assistant_request",
                    round_=round_, source_role="assistant",
                    source_text=assistant,
                    signals=("AI 把确认、选择或行动交给用户",),
                )
            if _PROMISE.search(assistant):
                _add_candidate(
                    candidates, seen,
                    session_key=session_key,
                    candidate_type="assistant_promise",
                    round_=round_, source_role="assistant",
                    source_text=assistant,
                    signals=("AI 使用明确未来承诺",),
                )
            if _ASSUMPTION.search(assistant):
                _add_candidate(
                    candidates, seen,
                    session_key=session_key,
                    candidate_type="explicit_assumption",
                    round_=round_, source_role="assistant",
                    source_text=assistant,
                    signals=("AI 明确声明假设或默认理解",),
                )
            if _COMPLETION.search(assistant):
                _add_candidate(
                    candidates, seen,
                    session_key=session_key,
                    candidate_type="completion_claim",
                    round_=round_, source_role="assistant",
                    source_text=assistant,
                    signals=("AI 使用完成性声明",),
                )

    # 同一轮有更具体结构时，普通 user_request 降权，但保留在完整候选里。
    specific_turns = {
        c["source_turn"] for c in candidates
        if c["type"] not in {"user_request", "completion_claim"}
    }
    for candidate in candidates:
        if (
            candidate["type"] == "user_request"
            and candidate["source_turn"] in specific_turns
        ):
            candidate["score"] -= 18

    candidates.sort(
        key=lambda item: (
            1 if item["id"] in priority_ids else 0,
            item["score"],
            item["source_turn"],
        ),
        reverse=True,
    )
    total_candidates = len(candidates)
    previously_seen_candidates = sum(
        candidate["id"] in excluded_ids for candidate in candidates
    )
    candidates = [
        candidate for candidate in candidates
        if candidate["id"] not in excluded_ids
    ]
    new_candidates = len(candidates)
    if max_candidates is None:
        selected_candidates = candidates
    else:
        # 防止一种高频语言（尤其「我会」和「已完成」）占满 evaluator
        # 预算。每类先保留最强的一项，让地基、AI 欠账和用户待办都有机会
        # 进入封闭核验；这是候选采样，不是价值排序。
        selected_candidates = []
        per_type: dict[str, int] = {}
        for candidate in candidates:
            kind = candidate["type"]
            if per_type.get(kind, 0) >= PER_TYPE_LIMIT:
                continue
            selected_candidates.append(candidate)
            per_type[kind] = per_type.get(kind, 0) + 1
            if len(selected_candidates) >= max(1, max_candidates):
                break

    # 后续轮次索引只保存一次。它让 evaluator 能检查整个剩余范围，而不必在
    # 每个候选里重复复制同一批回复；候选中的 evidence 再提供少量较完整片段。
    conversation_index = [
        [
            round_.turn,
            clip(round_.user, INDEX_USER_CHARS),
            clip(round_.assistant, INDEX_ASSISTANT_CHARS),
            len(" ".join(round_.user.split())) > INDEX_USER_CHARS,
            (
                len(" ".join(round_.assistant.split())) > INDEX_ASSISTANT_CHARS
            ),
        ]
        for round_ in analysis_rounds
    ]

    for candidate in selected_candidates:
        source_turn = candidate["source_turn"]
        source_text = candidate["source_quote"]
        later = [
            round_ for round_ in analysis_rounds
            if round_.turn > source_turn
        ]
        scored = []
        for later_round in later:
            combined = f"{later_round.user}\n{later_round.assistant}"
            scored.append((_overlap(source_text, combined), later_round))

        selected: list[Round] = []
        # 先放入紧邻轮次，它最可能承载直接回应或澄清答案。
        if later:
            selected.append(later[0])
        for _, later_round in sorted(
            scored, key=lambda item: (item[0], item[1].turn), reverse=True
        ):
            if later_round not in selected:
                selected.append(later_round)
            if len(selected) >= max_evidence:
                break
        # 最后状态常能证明承诺是否兑现；有空间时保留。
        if (
            later
            and analysis_rounds[-1] not in selected
            and len(selected) < max_evidence
        ):
            selected.append(analysis_rounds[-1])
        selected.sort(key=lambda item: item.turn)

        candidate["evidence"] = [
            {
                "turn": item.turn,
                "user": clip(item.user, EVIDENCE_CHARS),
                "assistant": clip(item.assistant, EVIDENCE_CHARS),
                "selection": (
                    "紧邻回应" if item.turn == source_turn + 1
                    else "相关或末次证据"
                ),
            }
            for item in selected
        ]
        candidate["checked_through_turn"] = (
            analysis_rounds[-1].turn if analysis_rounds else 0
        )
        candidate["verification_question"] = (
            "只依据 source_quote、direct_reply、conversation_index 与 evidence："
            "该言语行为是否已经得到对应回应或被明确关闭？"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "crumbs",
        "session": {
            "source": conversation.source,
            "source_id": conversation.source_id,
            "title": conversation.title,
            "session_key": session_key,
            "session_fingerprint": session_fingerprint,
            "human_turns": (
                analysis_rounds[-1].turn if analysis_rounds else 0
            ),
            "source_human_turns": len(rounds),
        },
        "budget": {
            "total_candidates": total_candidates,
            "new_candidates": new_candidates,
            "previously_seen_candidates": previously_seen_candidates,
            "pending_candidates": sum(
                candidate["id"] in priority_ids
                for candidate in candidates
            ),
            "selected_candidates": len(selected_candidates),
            "omitted_candidates": total_candidates - len(selected_candidates),
            "max_evidence_per_candidate": max_evidence,
            "max_candidates_per_type": (
                None if max_candidates is None else PER_TYPE_LIMIT
            ),
            "source_chars": SOURCE_CHARS,
            "reply_chars": REPLY_CHARS,
            "evidence_chars": EVIDENCE_CHARS,
            "index_user_chars": INDEX_USER_CHARS,
            "index_assistant_chars": INDEX_ASSISTANT_CHARS,
        },
        "conversation_index_format": [
            "turn", "user", "assistant", "user_truncated", "assistant_truncated",
        ],
        "conversation_index": conversation_index,
        "candidates": selected_candidates,
    }


def agent_view(packet: dict) -> dict:
    """去除 Agent 不需要的空字段，并附上封闭核验约束。"""
    return {
        "schema_version": packet["schema_version"],
        "mode": packet["mode"],
        "session": packet["session"],
        "budget": packet["budget"],
        "conversation_index_format": packet["conversation_index_format"],
        "conversation_index": packet["conversation_index"],
        "evaluator_contract": {
            "task": "逐项判断 closed / open / uncertain，不发现新事项",
            "facts": (
                "只能使用 conversation_index 与候选的 "
                "source_quote、direct_reply、evidence"
            ),
            "intent": "沉默不等于放弃；没有明确用户原话时 intent 必须为 unknown",
            "negative_claim": "open 是否定性断言，必须说明检查范围与缺少的证据",
            "output": "严格按 references/crumbs-evaluator.md 的 JSON schema",
        },
        "candidates": packet["candidates"],
    }


def default_state_path() -> Path:
    """返回只存候选哈希与状态的本地账本；可用环境变量覆盖。"""
    override = os.environ.get("BREADCRUMBS_CRUMBS_STATE")
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(state_home).expanduser()
        if state_home else Path.home() / ".local" / "state"
    )
    return base / "breadcrumbs" / "crumbs-state.json"


def load_state(path: str | Path) -> dict:
    target = Path(path).expanduser()
    if not target.exists():
        return {"schema_version": STATE_VERSION, "sessions": {}}
    value = json.loads(target.read_text(encoding="utf-8"))
    if (
        isinstance(value, dict)
        and value.get("schema_version") == 1
        and isinstance(value.get("sessions"), dict)
    ):
        # v1 只知道「展示过」，并不知道用户有没有回答。诚实迁移为
        # pending，避免把沉默误判成关闭。
        sessions = {}
        for session_key, session in value["sessions"].items():
            seen = [
                str(candidate_id)
                for candidate_id in (session or {}).get("seen") or []
                if str(candidate_id)
            ]
            sessions[str(session_key)] = {
                "items": {
                    candidate_id: "pending" for candidate_id in seen
                },
                "order": seen[-MAX_ITEMS_PER_SESSION:],
            }
        return {"schema_version": STATE_VERSION, "sessions": sessions}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != STATE_VERSION
        or not isinstance(value.get("sessions"), dict)
    ):
        raise ValueError(f"无效的 crumbs 增量状态: {target}")
    return value


def candidate_statuses(state: dict, session_key: str) -> dict[str, str]:
    session = (state.get("sessions") or {}).get(session_key) or {}
    items = session.get("items") or {}
    if not isinstance(items, dict):
        return {}
    return {
        str(candidate_id): str(status)
        for candidate_id, status in items.items()
        if str(candidate_id)
        and status in {"pending", *TERMINAL_DECISION_STATUSES}
    }


def excluded_candidate_ids(state: dict, session_key: str) -> set[str]:
    """只排除已经裁决的事项；未回答的 pending 必须继续存在。"""
    return {
        candidate_id
        for candidate_id, status in candidate_statuses(
            state, session_key
        ).items()
        if status in TERMINAL_DECISION_STATUSES
    }


def pending_candidate_ids(state: dict, session_key: str) -> set[str]:
    return {
        candidate_id
        for candidate_id, status in candidate_statuses(
            state, session_key
        ).items()
        if status == "pending"
    }


def _write_state(path: str | Path, state: dict) -> None:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=target.parent,
        prefix=".crumbs-state-", delete=False,
    ) as handle:
        json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(target)


def register_questions(
    plan: dict,
    path: str | Path,
) -> bool:
    """把本轮问题登记为 pending；不保存原话、问题或模型结论。"""
    questions = plan.get("questions") or []
    if not questions:
        return False
    session_key = str((plan.get("session") or {}).get("session_key") or "")
    candidate_ids = [
        str(question.get("candidate_id") or "")
        for question in questions
    ]
    if not session_key or not all(candidate_ids):
        raise ValueError("交互计划缺少 candidate_id 或 session_key")

    target = Path(path).expanduser()
    state = load_state(target)
    sessions = state["sessions"]
    session = sessions.pop(session_key, {"items": {}, "order": []})
    items = {
        str(candidate_id): str(status)
        for candidate_id, status in (session.get("items") or {}).items()
        if str(candidate_id)
        and status in {"pending", *TERMINAL_DECISION_STATUSES}
    }
    order = [
        str(candidate_id)
        for candidate_id in session.get("order") or []
        if str(candidate_id) in items
    ]
    for candidate_id in candidate_ids:
        items.setdefault(candidate_id, "pending")
        if candidate_id not in order:
            order.append(candidate_id)
    order = order[-MAX_ITEMS_PER_SESSION:]
    items = {
        candidate_id: items[candidate_id]
        for candidate_id in order
    }
    sessions[session_key] = {"items": items, "order": order}
    while len(sessions) > MAX_STATE_SESSIONS:
        sessions.pop(next(iter(sessions)))
    _write_state(target, state)
    return True


def remember_verified_closures(
    plan: dict,
    verdicts: dict,
    path: str | Path,
) -> int:
    """把 evaluator 已证实闭环的候选记为 resolved。"""
    session_key = str((plan.get("session") or {}).get("session_key") or "")
    if not session_key:
        raise ValueError("交互计划缺少 session_key")
    closed_ids = [
        str(item.get("candidate_id") or "")
        for item in verdicts.get("items") or []
        if str(item.get("candidate_id") or "")
        and (
            item.get("status") == "closed"
            or item.get("intent") in {
                "explicit_drop", "explicit_done", "explicit_keep",
            }
        )
    ]
    if not closed_ids:
        return 0

    target = Path(path).expanduser()
    state = load_state(target)
    sessions = state["sessions"]
    session = sessions.pop(session_key, {"items": {}, "order": []})
    items = dict(session.get("items") or {})
    order = [
        str(value) for value in session.get("order") or []
        if str(value)
    ]
    changed = 0
    for candidate_id in closed_ids:
        if items.get(candidate_id) in {"queued", "ignored"}:
            continue
        if items.get(candidate_id) != "resolved":
            changed += 1
        items[candidate_id] = "resolved"
        if candidate_id not in order:
            order.append(candidate_id)
    order = order[-MAX_ITEMS_PER_SESSION:]
    sessions[session_key] = {
        "items": {cid: items[cid] for cid in order if cid in items},
        "order": order,
    }
    while len(sessions) > MAX_STATE_SESSIONS:
        sessions.pop(next(iter(sessions)))
    _write_state(target, state)
    return changed


def record_decision(
    plan: dict,
    decision: str,
    path: str | Path,
    *,
    candidate_id: str | None = None,
) -> str:
    """记录用户裁决；默认处理计划中第一个仍为 pending 的问题。"""
    if decision not in DECISION_TO_STATUS:
        raise ValueError(
            "decision 必须是 " + "/".join(DECISION_TO_STATUS)
        )
    session_key = str((plan.get("session") or {}).get("session_key") or "")
    questions = {
        str(item.get("candidate_id") or ""): item
        for item in plan.get("questions") or []
        if str(item.get("candidate_id") or "")
    }
    if not session_key or not questions:
        raise ValueError("交互计划没有可裁决的问题")

    target = Path(path).expanduser()
    state = load_state(target)
    statuses = candidate_statuses(state, session_key)
    selected = str(candidate_id or "")
    if selected:
        if selected not in questions:
            raise ValueError("candidate_id 不在当前交互计划中")
    else:
        selected = next(
            (
                cid for cid in questions
                if statuses.get(cid, "pending") == "pending"
            ),
            "",
        )
    if not selected:
        raise ValueError("当前交互计划已经全部裁决")

    sessions = state["sessions"]
    session = sessions.pop(session_key, {"items": {}, "order": []})
    items = dict(session.get("items") or {})
    order = [
        str(value) for value in session.get("order") or []
        if str(value)
    ]
    for cid in questions:
        items.setdefault(cid, "pending")
        if cid not in order:
            order.append(cid)
    items[selected] = DECISION_TO_STATUS[decision]
    order = order[-MAX_ITEMS_PER_SESSION:]
    sessions[session_key] = {
        "items": {cid: items[cid] for cid in order if cid in items},
        "order": order,
    }
    while len(sessions) > MAX_STATE_SESSIONS:
        sessions.pop(next(iter(sessions)))
    _write_state(target, state)
    return selected


def validate_verdicts(packet: dict, verdicts: dict) -> list[str]:
    """验证 evaluator 的身份、枚举和证据锚点，不替它做语义判断。"""
    errors: list[str] = []
    if verdicts.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"schema_version 应为 {SCHEMA_VERSION}，"
            f"实际 {verdicts.get('schema_version')!r}"
        )
    expected_fp = (packet.get("session") or {}).get("session_fingerprint")
    if verdicts.get("session_fingerprint") != expected_fp:
        errors.append("session_fingerprint 不匹配，拒绝套用另一场对话的核验")

    candidates = {item["id"]: item for item in packet.get("candidates", [])}
    seen: set[str] = set()
    raw_items = verdicts.get("items")
    if not isinstance(raw_items, list):
        errors.append("items 必须是数组，且每个候选恰好对应一项")
        return errors
    allowed_turns = {
        row[0] for row in packet.get("conversation_index", [])
        if isinstance(row, list) and row
    }
    for index, item in enumerate(raw_items):
        prefix = f"items[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} 必须是对象")
            continue
        cid = item.get("candidate_id")
        if cid not in candidates:
            errors.append(f"{prefix}.candidate_id 不在当前候选中: {cid!r}")
            continue
        if cid in seen:
            errors.append(f"{prefix}.candidate_id 重复: {cid}")
        seen.add(cid)
        if item.get("status") not in {"closed", "open", "uncertain"}:
            errors.append(f"{prefix}.status 必须是 closed/open/uncertain")
        if item.get("confidence") not in {"high", "medium", "low"}:
            errors.append(f"{prefix}.confidence 必须是 high/medium/low")
        if item.get("owner") not in {"assistant", "user", "shared"}:
            errors.append(f"{prefix}.owner 必须是 assistant/user/shared")
        if item.get("intent") not in {
            "unknown", "explicit_keep", "explicit_drop", "explicit_done",
        }:
            errors.append(f"{prefix}.intent 枚举不合法")

        source_turn = candidates[cid]["source_turn"]
        final_turn = max(allowed_turns, default=source_turn)
        checked = item.get("checked_range")
        if not isinstance(checked, dict):
            errors.append(f"{prefix}.checked_range 必须声明核查范围")
        else:
            start = checked.get("from_turn")
            end = checked.get("through_turn")
            if start != source_turn:
                errors.append(
                    f"{prefix}.checked_range.from_turn 必须等于候选起点"
                )
            if end not in allowed_turns or not isinstance(end, int):
                errors.append(
                    f"{prefix}.checked_range.through_turn 不在会话索引中"
                )
            elif item.get("status") == "open" and end != final_turn:
                errors.append(
                    f"{prefix}.open 必须核查到当前窗口最后一轮"
                )
        support = item.get("support_turns")
        if not isinstance(support, list):
            errors.append(f"{prefix}.support_turns 必须是数组")
            support = []
        if any(
            not isinstance(turn, int) or turn not in allowed_turns
            for turn in support
        ):
            errors.append(
                f"{prefix}.support_turns 引用了证据包之外的轮次"
            )
        if item.get("status") == "open" and not str(item.get("missing") or "").strip():
            errors.append(f"{prefix}.open 必须说明缺少什么闭环证据")
        if item.get("status") == "open" and not str(item.get("fact") or "").strip():
            errors.append(f"{prefix}.open 必须提供可核查的事实陈述")
        if (
            item.get("status") == "open"
            and item.get("confidence") != "high"
        ):
            errors.append(
                f"{prefix}.open 必须是 high confidence；否则使用 uncertain"
            )
        if item.get("status") == "uncertain" and str(
            item.get("fact") or ""
        ).strip():
            errors.append(
                f"{prefix}.uncertain 不得把疑点写成事实陈述"
            )
        if item.get("status") == "closed" and not support:
            errors.append(f"{prefix}.closed 必须提供至少一个 support_turn")
        if (
            item.get("status") != "closed"
            and item.get("intent") == "unknown"
            and not str(item.get("intent_question") or "").strip()
        ):
            errors.append(f"{prefix} 未闭环或疑似项必须提供 intent_question")
        if item.get("intent") != "unknown":
            intent_turn = item.get("intent_turn")
            if intent_turn not in allowed_turns:
                errors.append(
                    f"{prefix}.intent 非 unknown 时必须提供证据包内 intent_turn"
                )
        if item.get("impact") not in {"high", "medium", "low"}:
            errors.append(f"{prefix}.impact 必须是 high/medium/low")
        if item.get("impact") == "high" and not str(
            item.get("impact_reason") or ""
        ).strip():
            errors.append(f"{prefix}.impact=high 必须说明下游影响")
        if not str(item.get("reason") or "").strip():
            errors.append(f"{prefix}.reason 不能为空")
    missing_ids = set(candidates) - seen
    if missing_ids:
        errors.append(
            "evaluator 漏掉候选: " + "、".join(sorted(missing_ids))
        )
    return errors


def interaction_plan(packet: dict, verdicts: dict, limit: int = 3) -> dict:
    """把通过校验的核验结果变成本轮逐项对账计划。"""
    errors = validate_verdicts(packet, verdicts)
    if errors:
        raise ValueError("\n".join(errors))

    candidates = {item["id"]: item for item in packet["candidates"]}
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    impact_rank = {"high": 3, "medium": 2, "low": 1}
    items = []
    for verdict in verdicts.get("items") or []:
        if verdict["status"] == "closed":
            continue
        if verdict.get("intent") in {
            "explicit_drop", "explicit_done", "explicit_keep",
        }:
            continue
        candidate = candidates[verdict["candidate_id"]]
        uncertain = verdict["status"] == "uncertain"
        question = str(verdict.get("intent_question") or "").strip()
        if not question:
            question = QUESTION_BY_TYPE[candidate["type"]]
        options = DEFAULT_OPTIONS
        items.append({
            "candidate_id": candidate["id"],
            "kind": candidate["type"],
            "label": candidate["label"],
            "certainty": "suspected" if uncertain else "verified_open",
            "fact": (
                None if uncertain
                else str(verdict.get("fact") or "").strip()
            ),
            "missing": (
                str(verdict.get("missing") or "").strip() or None
            ),
            "question": question,
            "source_quote": candidate["source_quote"],
            "prompt_anchor": candidate["prompt_anchor"],
            "impact": verdict["impact"],
            "impact_reason": verdict.get("impact_reason"),
            "options": [
                {
                    "action": action,
                    "label": label,
                    "description": OPTION_DESCRIPTIONS[action],
                }
                for action, label in options
            ],
            "_rank": (
                impact_rank[verdict["impact"]],
                1 if verdict["status"] == "open" else 0,
                confidence_rank[verdict["confidence"]],
                candidate["score"],
            ),
        })
    items.sort(key=lambda item: item["_rank"], reverse=True)
    for item in items:
        item.pop("_rank", None)
        item["prompt"] = render_question_item(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "crumbs-interaction",
        "session": packet["session"],
        "summary": {
            "candidates_checked": len(verdicts.get("items") or []),
            "questions_available": len(items),
            "questions_selected": min(limit, len(items)),
            "automatically_closed": sum(
                1
                for item in verdicts.get("items") or []
                if item.get("status") == "closed"
                or item.get("intent") in {
                    "explicit_drop", "explicit_done", "explicit_keep",
                }
            ),
            "previously_seen_candidates": (
                (packet.get("budget") or {}).get(
                    "previously_seen_candidates", 0,
                )
            ),
        },
        "questions": items[:max(1, limit)],
    }


def render_question_item(item: dict) -> str:
    """渲染一个可直接用于宿主原生选择控件的问题。"""
    question = str(item.get("question") or "").strip()
    if not question.endswith(("？", "?")):
        question = question.rstrip("。！!；;") + "？"

    parts = [question]
    fact = str(item.get("fact") or "").strip()
    if fact:
        parts.append("我这样问是因为：" + fact)

    labels = [
        str(option.get("label") or "").strip()
        for option in item.get("options") or []
        if str(option.get("label") or "").strip()
    ]
    if labels:
        parts.append("你可以直接回答：" + " / ".join(labels))
    return "\n\n".join(parts)


def render_first_question(plan: dict) -> str:
    """把计划确定性地渲染成一问一答入口，而不是一段核查报告。"""
    questions = plan.get("questions") or []
    if not questions:
        if (plan.get("summary") or {}).get("previously_seen_candidates"):
            return "这次没有新增的未闭环事项。"
        return "这次没有找到足够可靠、且会改变下一步行动的未闭环事项。"
    return str(
        questions[0].get("prompt")
        or render_question_item(questions[0])
    )


def _decision_label(status: str) -> str:
    return {
        "queued": "处理",
        "resolved": "已处理",
        "ignored": "忽略",
    }[status]


def _summary_text(item: dict) -> str:
    missing = str(item.get("missing") or "").strip()
    if missing:
        return missing.rstrip("。")
    source = clip(str(item.get("source_quote") or ""), 110)
    if source:
        return f"「{source}」"
    return str(item.get("label") or "未命名事项")


def _next_action_text(text: str) -> str:
    value = str(text or "").strip().rstrip("。")
    if value.startswith("缺少"):
        return "补上" + value[2:]
    if value.startswith("「"):
        return "继续处理" + value
    return "处理" + value


def settlement_plan(
    plan: dict,
    state: dict,
) -> dict:
    """在本轮问题全部裁决后生成对账结果和唯一下一步。"""
    session_key = str((plan.get("session") or {}).get("session_key") or "")
    statuses = candidate_statuses(state, session_key)
    questions = plan.get("questions") or []
    pending = [
        item for item in questions
        if statuses.get(str(item.get("candidate_id") or ""), "pending")
        == "pending"
    ]
    if pending:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "crumbs-question",
            "session": plan.get("session") or {},
            "progress": {
                "answered": len(questions) - len(pending),
                "total": len(questions),
            },
            "question": pending[0],
        }

    groups = {"queued": [], "resolved": [], "ignored": []}
    for item in questions:
        candidate_id = str(item.get("candidate_id") or "")
        status = statuses.get(candidate_id)
        if status not in groups:
            raise ValueError(
                f"候选 {candidate_id} 没有有效裁决状态"
            )
        groups[status].append({
            "candidate_id": candidate_id,
            "text": _summary_text(item),
        })

    first_action = groups["queued"][0] if groups["queued"] else None
    next_action = None
    if first_action is not None:
        next_action = {
            "candidate_id": first_action["candidate_id"],
            "text": _next_action_text(first_action["text"]),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "crumbs-settlement",
        "session": plan.get("session") or {},
        "counts": {
            "total": len(questions),
            "process": len(groups["queued"]),
            "resolved": len(groups["resolved"]),
            "ignored": len(groups["ignored"]),
            "automatically_closed": int(
                (plan.get("summary") or {}).get(
                    "automatically_closed", 0,
                )
            ),
        },
        "groups": {
            _decision_label(status): items
            for status, items in groups.items()
        },
        "next_action": next_action,
    }


def render_settlement(settlement: dict) -> str:
    """以克制、可核查的格式渲染本轮对账结算。"""
    if settlement.get("mode") != "crumbs-settlement":
        raise ValueError("只有 crumbs-settlement 可以生成总结")
    counts = settlement.get("counts") or {}
    parts = [f"对账完成：{int(counts.get('total', 0))} 项。"]
    groups = settlement.get("groups") or {}
    for label in ("处理", "已处理", "忽略"):
        items = groups.get(label) or []
        if not items:
            continue
        parts.append(
            f"{label}：{len(items)} 项\n"
            + "\n".join(f"- {item['text']}" for item in items)
        )
    automatically_closed = int(counts.get("automatically_closed", 0))
    if automatically_closed:
        parts.append(f"自动核实已闭环：{automatically_closed} 项。")
    next_action = settlement.get("next_action")
    if next_action:
        parts.append("下一步：" + str(next_action["text"]).rstrip("。") + "。")
    else:
        parts.append("下一步：没有需要继续处理的事项。")
    return "\n\n".join(parts)


def render_decision_result(result: dict) -> str:
    if result.get("mode") == "crumbs-question":
        question = result.get("question") or {}
        return str(
            question.get("prompt") or render_question_item(question)
        )
    return render_settlement(result)


def _extract_json_object(text: str) -> dict:
    """接受结构化 JSON 或偶尔被模型包上的 Markdown fence。"""
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        start, end = value.find("{"), value.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("evaluator 没有返回 JSON 对象")
        result = json.loads(value[start:end + 1])
    if not isinstance(result, dict):
        raise ValueError("evaluator 顶层结果必须是 JSON 对象")
    return result


def evaluator_prompt(packet: dict) -> str:
    """把封闭任务、契约与有限证据合成一次独立核验输入。"""
    contract = EVALUATOR_CONTRACT_PATH.read_text(encoding="utf-8")
    compact_packet = json.dumps(
        agent_view(packet), ensure_ascii=False, separators=(",", ":"),
    )
    return (
        "你是 Breadcrumbs 的独立闭环核验器，不是当前任务的执行者。\n"
        "严格遵守以下契约；只返回符合 schema 的 JSON，不写 Markdown。\n\n"
        f"{contract}\n\n"
        "## Candidate packet\n"
        f"{compact_packet}\n"
    )


def _run_process(
    command: list[str],
    prompt: str,
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def run_evaluator(
    packet: dict,
    *,
    host: str,
    timeout: int = 240,
) -> tuple[dict, str]:
    """通过宿主 CLI 调用隔离的小模型，并返回已通过证据校验的 verdict。"""
    if host not in {"claude", "codex"}:
        raise ValueError("host 必须是 claude 或 codex")
    if not packet.get("candidates"):
        return {
            "schema_version": SCHEMA_VERSION,
            "session_fingerprint": packet["session"]["session_fingerprint"],
            "items": [],
        }, "未调用模型（没有候选）"
    prompt = evaluator_prompt(packet)
    schema_text = VERDICT_SCHEMA_PATH.read_text(encoding="utf-8")
    attempts: list[str] = []

    if host == "claude":
        executable = shutil.which("claude")
        if executable is None:
            raise ValueError("未找到 claude 命令，无法调用 Haiku evaluator")
        command = [
            executable,
            "-p",
            "--model", CLAUDE_EVALUATOR_MODEL,
            "--fallback-model", CLAUDE_EVALUATOR_FALLBACK,
            "--effort", EVALUATOR_EFFORT,
            "--tools", "",
            "--permission-mode", "dontAsk",
            "--no-session-persistence",
            "--output-format", "text",
            "--json-schema", schema_text,
        ]
        try:
            completed = _run_process(command, prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(
                f"Claude evaluator 超过 {timeout} 秒未完成"
            ) from exc
        if completed.returncode != 0:
            detail = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"进程退出码 {completed.returncode}"
            )[-1000:]
            if "OAuth session expired" in detail:
                detail += "；请先在 Claude Code 中运行 /login"
            raise ValueError(f"Claude evaluator 调用失败：{detail}")
        verdicts = _extract_json_object(completed.stdout)
        errors = validate_verdicts(packet, verdicts)
        if errors:
            raise ValueError("Claude evaluator 结果未通过校验：\n" + "\n".join(errors))
        return verdicts, (
            f"{CLAUDE_EVALUATOR_MODEL}"
            f"（不可用时由 Claude Code 回退 {CLAUDE_EVALUATOR_FALLBACK}）"
        )

    executable = shutil.which("codex")
    if executable is None:
        raise ValueError("未找到 codex 命令，无法调用 Luna evaluator")
    for model in CODEX_EVALUATOR_MODELS:
        with tempfile.TemporaryDirectory(prefix="crumbs-evaluator-") as tmp:
            output_path = Path(tmp) / "verdicts.json"
            command = [
                executable, "exec",
                "--model", model,
                "-c", f'model_reasoning_effort="{EVALUATOR_EFFORT}"',
                "--sandbox", "read-only",
                "--ephemeral",
                "--ignore-rules",
                "--output-schema", str(VERDICT_SCHEMA_PATH),
                "--output-last-message", str(output_path),
                "-",
            ]
            try:
                completed = _run_process(command, prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                attempts.append(f"{model}: 超过 {timeout} 秒")
                continue
            if completed.returncode != 0:
                attempts.append(
                    f"{model}: {completed.stderr.strip()[-600:]}"
                )
                continue
            raw = (
                output_path.read_text(encoding="utf-8")
                if output_path.exists() else completed.stdout
            )
            try:
                verdicts = _extract_json_object(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                attempts.append(f"{model}: {exc}")
                continue
            errors = validate_verdicts(packet, verdicts)
            if errors:
                attempts.append(f"{model}: " + "；".join(errors))
                continue
            return verdicts, model
    raise ValueError(
        "Codex evaluator 的 Luna 与 Terra 均未成功：\n"
        + "\n".join(attempts)
    )


def _load_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(
    path: str | Path,
    value: dict,
    *,
    compact: bool = False,
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":") if compact else None,
            indent=None if compact else 2,
        ) + "\n",
        encoding="utf-8",
    )


def prepare_command(args: argparse.Namespace) -> int:
    try:
        conversation = load_conversation(
            args.source, args.session, last=args.last
        )
    except ConversationSourceError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        state = load_state(args.state)
        session_key = _session_key(conversation)
        excluded_ids = (
            set() if args.repeat_seen
            else excluded_candidate_ids(
                state, session_key
            )
        )
        priority_ids = (
            set() if args.repeat_seen
            else pending_candidate_ids(state, session_key)
        )
        packet = detect_candidates(
            conversation,
            max_candidates=None if args.all else max(1, args.max_candidates),
            max_evidence=max(1, args.max_evidence),
            excluded_ids=excluded_ids,
            priority_ids=priority_ids,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法读取增量状态: {exc}", file=sys.stderr)
        return 1
    _write_json(args.out, packet)
    print(
        f"候选证据包: {args.out} "
        f"({packet['budget']['selected_candidates']}/"
        f"{packet['budget']['total_candidates']} 项)"
    )
    if args.agent_view:
        out = Path(args.out)
        view_path = out.with_name(out.stem + ".agent.json")
        _write_json(view_path, agent_view(packet), compact=True)
        print(
            f"小模型视图: {view_path} "
            f"({len(view_path.read_text(encoding='utf-8'))} 字符)"
        )
    return 0


def normalize_command(args: argparse.Namespace) -> int:
    """接收宿主原生子 Agent 的原始文本，并执行同一套证据校验。"""
    try:
        packet = _load_json(args.candidates)
        raw = Path(args.raw).read_text(encoding="utf-8")
        verdicts = _extract_json_object(raw)
        errors = validate_verdicts(packet, verdicts)
        if errors:
            raise ValueError("\n".join(errors))
        _write_json(args.out, verdicts)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"子 Agent 核验结果不可用: {exc}", file=sys.stderr)
        return 1
    print(f"核验结果已接受: {args.out}")
    return 0


def evaluate_command(args: argparse.Namespace) -> int:
    try:
        packet = _load_json(args.candidates)
        verdicts, model = run_evaluator(
            packet, host=args.host, timeout=max(30, args.timeout),
        )
        _write_json(args.out, verdicts)
    except (
        OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError,
    ) as exc:
        print(f"无法完成独立核验: {exc}", file=sys.stderr)
        return 1
    print(f"核验结果: {args.out}（{model}, {EVALUATOR_EFFORT} effort）")
    return 0


def present_command(args: argparse.Namespace) -> int:
    try:
        packet = _load_json(args.candidates)
        verdicts = _load_json(args.verdicts)
        plan = interaction_plan(packet, verdicts, limit=max(1, args.limit))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法生成交互计划: {exc}", file=sys.stderr)
        return 1
    if args.out:
        _write_json(args.out, plan)
    if args.remember:
        try:
            remember_verified_closures(plan, verdicts, args.state)
            register_questions(plan, args.state)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"无法登记待决定问题: {exc}", file=sys.stderr)
            return 1
    if args.ask:
        print(render_first_question(plan))
    elif args.out:
        print(f"交互计划: {args.out}")
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def decide_command(args: argparse.Namespace) -> int:
    try:
        plan = _load_json(args.interaction)
        register_questions(plan, args.state)
        selected = record_decision(
            plan,
            args.decision,
            args.state,
            candidate_id=args.candidate,
        )
        state = load_state(args.state)
        result = settlement_plan(plan, state)
        result["decision"] = {
            "candidate_id": selected,
            "value": args.decision,
            "status": DECISION_TO_STATUS[args.decision],
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"无法记录用户裁决: {exc}", file=sys.stderr)
        return 1
    if args.out:
        _write_json(args.out, result)
    if args.ask:
        print(render_decision_result(result))
    elif args.out:
        print(f"裁决结果: {args.out}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Breadcrumbs /crumbs 轻量闭环核查",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="定位候选并裁出小模型证据包")
    prepare.add_argument("session", nargs="?", help="会话路径或任务 id")
    prepare.add_argument(
        "--source", choices=("claude", "codex"), default="claude",
    )
    prepare.add_argument("--last", action="store_true", help="使用最近会话")
    prepare.add_argument(
        "-o", "--out", default="crumbs.candidates.json",
    )
    prepare.add_argument(
        "--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
    )
    prepare.add_argument(
        "--max-evidence", type=int, default=DEFAULT_MAX_EVIDENCE,
    )
    prepare.add_argument(
        "--all", action="store_true", help="保留全部候选，不使用默认上限",
    )
    prepare.add_argument(
        "--state", default=str(default_state_path()),
        help="只保存候选哈希与裁决状态的本地账本",
    )
    prepare.add_argument(
        "--repeat-seen", action="store_true",
        help="调试用：忽略账本，重新包含已经裁决的候选",
    )
    prepare.add_argument(
        "--agent-view", action="store_true",
        help="同时输出去掉冗余字段的小模型视图",
    )
    prepare.set_defaults(func=prepare_command)

    evaluate = sub.add_parser(
        "evaluate", help="调用隔离的小模型核验候选",
    )
    evaluate.add_argument("candidates")
    evaluate.add_argument(
        "--host", choices=("claude", "codex"), required=True,
    )
    evaluate.add_argument("-o", "--out", default="verdicts.json")
    evaluate.add_argument("--timeout", type=int, default=240)
    evaluate.set_defaults(func=evaluate_command)

    normalize = sub.add_parser(
        "normalize", help="校验宿主原生子 Agent 返回的原始 JSON",
    )
    normalize.add_argument("candidates")
    normalize.add_argument("raw")
    normalize.add_argument("-o", "--out", default="verdicts.json")
    normalize.set_defaults(func=normalize_command)

    present = sub.add_parser(
        "present", help="校验 evaluator 结果并生成行动式追问计划",
    )
    present.add_argument("candidates")
    present.add_argument("verdicts")
    present.add_argument("-o", "--out")
    present.add_argument("--limit", type=int, default=3)
    present.add_argument(
        "--ask", action="store_true",
        help="只输出最高优先级的一个用户问题，不输出计划摘要",
    )
    present.add_argument(
        "--remember", action="store_true",
        help="把本轮问题登记为 pending，未回答的问题不会消失",
    )
    present.add_argument(
        "--state", default=str(default_state_path()),
        help="只保存候选哈希与裁决状态的本地账本",
    )
    present.set_defaults(func=present_command)

    decide = sub.add_parser(
        "decide", help="记录处理/已处理/忽略，并继续下一题或生成总结",
    )
    decide.add_argument("interaction", help="present 生成的交互计划")
    decide.add_argument(
        "--decision",
        choices=tuple(DECISION_TO_STATUS),
        required=True,
    )
    decide.add_argument(
        "--candidate",
        help="指定候选 id；默认裁决计划中第一个 pending 问题",
    )
    decide.add_argument("-o", "--out")
    decide.add_argument(
        "--ask", action="store_true",
        help="输出下一题；全部裁决后输出总结与下一步",
    )
    decide.add_argument(
        "--state", default=str(default_state_path()),
        help="只保存候选哈希与裁决状态的本地账本",
    )
    decide.set_defaults(func=decide_command)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

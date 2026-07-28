"""把结构分析渲染成一份可以重新进入工作的复盘稿。

结果页分三层：
1. 持续可见的「讨论路径」——用用户原话和语义名称定位，不暴露线路编号；
2. 复盘正文——回答改变、建议继续项、每条思路留下的东西；
3. 原话与诊断——每个判断都能回到 prompt，工程编号只留在折叠层。

页面是单文件、无 CDN、可离线打开。CSS 与 JS 在源码里分开维护，生成时
内联进 HTML，既保持分发简单，也避免 render.py 再变成难以修改的巨型字符串。
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import html
import json
from pathlib import Path


ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"
DORMANT_MIN = 5
REL_LABEL = {
    "redirected": "改变了主问题的走向",
    "supplied": "为主问题补进了材料",
    "blocked": "暴露了主问题的卡点",
    "tangent": "形成了独立问题",
    "dropped": "暂未回到主问题",
}


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _one_line(value: object, limit: int = 64) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _asset_text(name: str) -> str:
    path = ASSET_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"报告资源缺失：{path}")
    return path.read_text(encoding="utf-8")


def _fallback_anchor(text: str, timestamp: str, turn: int) -> str:
    material = f"{text}\x1f{timestamp}\x1f{turn}".encode("utf-8")
    return "prompt-" + hashlib.sha256(material).hexdigest()[:16]


def prompt_anchor(res: dict, turn: int) -> str:
    anchors = res.get("prompt_anchors") or []
    if 1 <= turn <= len(anchors) and anchors[turn - 1]:
        return anchors[turn - 1]
    texts = res.get("texts_full") or []
    times = res.get("timestamps") or []
    text = texts[turn - 1] if 1 <= turn <= len(texts) else ""
    timestamp = times[turn - 1] if 1 <= turn <= len(times) else ""
    return _fallback_anchor(text, timestamp, turn)


def thread_stats(res: dict) -> list[dict]:
    """按思路汇总结构事实；这个函数也被 analyze.py 用来建立 agent 视图。"""
    n = res["n"]
    out = []
    for tid, rounds in sorted(res["threads"].items()):
        rounds = sorted(rounds)
        if not rounds:
            continue
        first, last = rounds[0], rounds[-1]
        dormancies = []
        for a, b in zip(rounds, rounds[1:]):
            if b - a > DORMANT_MIN:
                dormancies.append({"from": a, "to": b, "gap": b - a - 1})
        tail = max(4, int(n * 0.08))
        step = next((s for s in res.get("steps", [])
                     if s.get("to") == first), None)
        heads = res.get("texts_head") or res.get("texts_full") or []
        head = heads[first - 1] if first <= len(heads) else ""
        out.append({
            "tid": tid,
            "first": first,
            "last": last,
            "count": len(rounds),
            "rounds": rounds,
            "dangling": (n - last) >= tail,
            "dormancies": dormancies,
            "revisited": bool(dormancies),
            "since_last": n - last,
            "kind": (step or {}).get("kind"),
            "head": head,
        })
    return out


def trunk_id(res: dict, stats: list[dict] | None = None) -> int | None:
    analysis_threads = res.get("analysis_threads") or {}
    marked = [tid for tid, item in analysis_threads.items()
              if item.get("is_trunk")]
    if marked:
        return marked[0]
    stats = stats if stats is not None else thread_stats(res)
    return max(stats, key=lambda s: s["count"])["tid"] if stats else None


def thread_label(res: dict, ann: dict | None, tid: int) -> str:
    """第一视觉层永远使用语义名或用户原话，不使用「线 3」。"""
    annotation = (ann or {}).get(tid, {})
    if annotation.get("name"):
        return str(annotation["name"])
    stat = next((s for s in thread_stats(res) if s["tid"] == tid), None)
    return _one_line((stat or {}).get("head"), 24) or "一段尚未命名的追问"


def is_open_thread(stat: dict, annotation: dict) -> bool:
    if annotation.get("resolved") is not None:
        return annotation["resolved"] is False
    return stat["dangling"]


def open_threads(res: dict, ann: dict | None) -> list[dict]:
    annotations = ann or {}
    stats = thread_stats(res)
    main = trunk_id(res, stats)
    opened = [
        stat for stat in stats
        if stat["tid"] != main and is_open_thread(
            stat, annotations.get(stat["tid"], {}))
    ]
    return sorted(opened, key=lambda s: (-s["last"], -s["count"]))


def recommendation(res: dict, ann: dict | None) -> tuple[dict, str] | None:
    """给出可解释的编辑性建议，不伪装成客观价值分。

    排序只消费已存在的结构与标注：对主问题的关系、是否反复回访、已有产出、
    最近是否仍在场。用户可以看到理由，也能立刻回到原话自行推翻它。
    """
    annotations = ann or {}
    candidates = open_threads(res, ann)
    if not candidates:
        return None
    relation_weight = {
        "blocked": 4, "redirected": 3, "supplied": 2,
        "tangent": 1, "dropped": 0,
    }

    def score(stat: dict) -> tuple:
        a = annotations.get(stat["tid"], {})
        return (
            relation_weight.get(a.get("relation_to_trunk"), 0),
            int(stat["revisited"]),
            int(bool(a.get("yield"))),
            stat["last"],
            stat["count"],
        )

    chosen = max(candidates, key=score)
    a = annotations.get(chosen["tid"], {})
    rel = a.get("relation_to_trunk")
    reasons = []
    if rel == "blocked":
        reasons.append("它仍在卡住主问题")
    elif rel == "redirected":
        reasons.append("它已经改变主问题的走向，却还没有收束")
    elif rel == "supplied":
        reasons.append("它已经留下材料，但还没有明确回到主问题")
    if chosen["revisited"]:
        reasons.append("你曾在放下后又回到它")
    if a.get("yield"):
        reasons.append("它已有可继续使用的中间产出")
    if not reasons:
        reasons.append("它仍然开放，并且是最近仍在场的问题之一")
    return chosen, "；".join(reasons) + "。"


def discussion_path(res: dict, ann: dict | None) -> list[dict]:
    """从线路切换和回访生成可读的路程轴，不新增一次 LLM 总结。

    新思路第一次出现叫「转去」，隔一段后重现叫「回到」。如果路径过长，
    保留开头与结尾，中间只报告有几次短暂往返，避免导航本身成为噪声。
    """
    tids = res.get("thread_id") or []
    if not tids:
        return []
    runs = []
    start = 1
    current = tids[0]
    for turn, tid in enumerate(tids[1:], start=2):
        if tid != current:
            runs.append((current, start, turn - 1))
            current, start = tid, turn
    runs.append((current, start, len(tids)))

    seen: set[int] = set()
    last_seen_end: dict[int, int] = {}
    stops = []
    for index, (tid, first, last) in enumerate(runs):
        if index == 0:
            verb = "从这里开始"
        elif tid not in seen:
            verb = "转去"
        elif first - last_seen_end.get(tid, first) > DORMANT_MIN:
            verb = "回到"
        else:
            last_seen_end[tid] = last
            seen.add(tid)
            continue
        a = (ann or {}).get(tid, {})
        semantic_label = thread_label(res, ann, tid)
        semantic_confirmed = bool(a)
        # 如果 agent 明确把证据指向该算法主题的后半段，说明起点不足以
        # 支撑这个语义名。起点阶段继续显示用户原话，不能把假合并涂成实线。
        if a.get("evidence_turn") and a["evidence_turn"] > last:
            semantic_label = _one_line(res["texts_full"][first - 1], 24)
            semantic_confirmed = False
        stops.append({
            "tid": tid,
            "turn": first,
            "verb": verb,
            "label": semantic_label,
            "confirmed": semantic_confirmed,
            "anchor": prompt_anchor(res, first),
            "quote": _one_line(res["texts_full"][first - 1], 38),
        })
        seen.add(tid)
        last_seen_end[tid] = last

    final_tid, _, final_turn = runs[-1]
    if not stops or stops[-1]["turn"] != final_turn:
        stops.append({
            "tid": final_tid,
            "turn": final_turn,
            "verb": "最后停在",
            "label": thread_label(res, ann, final_tid),
            "confirmed": bool((ann or {}).get(final_tid)),
            "anchor": prompt_anchor(res, final_turn),
            "quote": _one_line(res["texts_full"][final_turn - 1], 38),
        })

    if len(stops) > 12:
        omitted = len(stops) - 11
        stops = stops[:6] + [{"omitted": omitted}] + stops[-5:]
    return stops


def trust_state(res: dict) -> tuple[str, str]:
    trust = res.get("trust") or {}
    corpus = (res.get("analysis_meta") or {}).get("corpus") or {}
    if trust.get("forced"):
        return (
            "danger",
            "这份报告强行使用了未通过校验的标注。语义名称可能贴错位置，"
            "只能用于排错，不能据此复盘。",
        )
    if corpus.get("degraded"):
        reason = corpus.get("degrade_reason") or "历史语料不足。"
        if trust.get("annotations") == "confirmed":
            return (
                "warning",
                f"{reason} 路径划分仍是低置信度；实线语义已由 Agent 回读核实。",
            )
        return (
            "warning inferred",
            f"{reason} 当前语义也未由 Agent 回读核实，整份报告只适合做导航草稿。",
        )
    if trust.get("annotations") == "confirmed":
        return (
            "confirmed",
            "语义名称与结论已由 Agent 回读核实；路径连接仍是算法推断，"
            "每个判断都能打开当时原话。",
        )
    return (
        "inferred",
        "虚线表示仅按用户提问的结构推断。尚未回读 AI 回复，因此不判断观点是否成立。",
    )


def _source_button(res: dict, turn: int, label: str = "查看当时原话") -> str:
    anchor = prompt_anchor(res, turn)
    return (f'<button class="source-link" type="button" '
            f'data-source="{esc(anchor)}">{esc(label)} →</button>')


def evidence_turn(stat: dict, annotation: dict) -> int:
    """语义判断的证据位置；默认回到起点，agent 可在假合并时改指。"""
    turn = annotation.get("evidence_turn")
    return turn if turn in stat["rounds"] else stat["first"]


def render_path(res: dict, ann: dict | None, session_key: str) -> str:
    parts = [
        '<aside class="path-panel" aria-label="讨论路径">',
        "<h2>讨论路径</h2>",
        '<p class="path-intro">用你当时的问题定位。实心节点的语义已核实；'
        "连接虚线仍是结构推断。</p>",
        '<ol class="journey">',
    ]
    for stop in discussion_path(res, ann):
        if stop.get("omitted"):
            parts.append(
                f'<li class="journey-omitted">中间还有 {stop["omitted"]} 次短暂往返</li>'
            )
            continue
        klass = "confirmed" if stop.get("confirmed") else "inferred"
        thought_id = f"thought-{session_key}-{stop['tid']}"
        parts.append(
            f'<li class="journey-stop {klass}">'
            f'<a class="journey-link" href="#{esc(thought_id)}" '
            f'title="{esc(stop["verb"] + "〈" + stop["label"] + "〉")}">'
            f'{esc(stop["verb"])}〈{esc(stop["label"])}〉</a>'
            f'<button class="journey-quote" type="button" '
            f'data-source="{esc(stop["anchor"])}" '
            f'title="{esc(stop["quote"])}">“{esc(stop["quote"])}”</button>'
            "</li>"
        )
    parts.extend(["</ol>", "</aside>"])
    return "".join(parts)


def render_changes(res: dict, ann: dict | None) -> str:
    annotations = ann or {}
    stats = thread_stats(res)
    main = trunk_id(res, stats)
    changes = []
    for stat in stats:
        if stat["tid"] == main:
            continue
        a = annotations.get(stat["tid"], {})
        rel = a.get("relation_to_trunk")
        if rel not in {"redirected", "supplied", "blocked"}:
            continue
        changes.append((stat, a, rel))

    parts = [
        '<section class="review-section" id="changes">',
        "<h2>这次改变了什么</h2>",
    ]
    if not changes:
        parts.append(
            '<p class="section-intro">目前只能确认讨论路径发生过变化。'
            "尚未得到足够的语义标注，因此不替你编写“观点发生了什么变化”。</p>"
        )
        if stats:
            first = stats[0]
            main = trunk_id(res, stats)
            first = next((s for s in stats if s["tid"] == main), first)
            parts.append('<div class="change-list"><article class="change-item">')
            parts.append("<h3>先从最初的问题重新进入</h3>")
            parts.append(f'<p>“{esc(_one_line(first["head"], 90))}”</p>')
            parts.append(_source_button(res, first["first"]))
            parts.append("</article></div>")
    else:
        parts.append(
            '<p class="section-intro">这里只列能由结构化标注和原话核对的变化，'
            "不生成一段无法验证的自由总结。</p><div class=\"change-list\">"
        )
        for stat, a, rel in changes:
            name = thread_label(res, ann, stat["tid"])
            body = a.get("relation_note") or a.get("yield") or REL_LABEL[rel]
            parts.append(
                '<article class="change-item">'
                f'<h3>〈{esc(name)}〉{esc(REL_LABEL[rel])}</h3>'
                f'<p>{esc(body)}</p>'
                f'{_source_button(res, evidence_turn(stat, a))}'
                "</article>"
            )
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def render_recommendation(res: dict, ann: dict | None) -> str:
    picked = recommendation(res, ann)
    parts = [
        '<section class="review-section" id="continue">',
        "<h2>建议优先继续</h2>",
        '<p class="section-intro">这是一条可推翻的编辑建议，不是“漂移质量分”。'
        "排序只看它是否仍开放、怎样影响主问题、是否被你重新捡起。</p>",
    ]
    if picked is None:
        parts.append(
            '<div class="recommendation"><h3>没有明显悬空的问题</h3>'
            "<p>现有标注里，每条派生思路都已收束或回到主问题。</p></div>"
        )
    else:
        stat, reason = picked
        a = (ann or {}).get(stat["tid"], {})
        parts.append('<div class="recommendation">')
        parts.append(f'<h3>{esc(thread_label(res, ann, stat["tid"]))}</h3>')
        parts.append(f'<p class="reason">{esc(reason)}</p>')
        if a.get("yield"):
            parts.append(f'<p>已经留下：{esc(a["yield"])}</p>')
        parts.append(_source_button(
            res, evidence_turn(stat, a), "从这个问题继续"))
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def _state_label(stat: dict, a: dict, is_main: bool) -> tuple[str, str]:
    if a.get("resolved") is True:
        return "已收束", ""
    if a.get("resolved") is False:
        return "仍开放", " open"
    if is_main:
        return "主问题", ""
    if stat["dangling"]:
        return "结构上未回访", " open"
    return "结构推断", ""


def _spawn_text(a: dict) -> str:
    by = a.get("spawned_by")
    return {
        "user": "由你的追问提出",
        "assistant": "由 AI 的上一段回复引出",
        "both": "由你与 AI 共同推进",
        "unknown": "暂时无法确认由谁引出",
    }.get(by, "尚未回读前文，不能确认由谁引出")


def render_thoughts(res: dict, ann: dict | None, session_key: str) -> str:
    annotations = ann or {}
    stats = thread_stats(res)
    main = trunk_id(res, stats)
    confirmed = (res.get("trust") or {}).get("annotations") == "confirmed"
    parts = [
        '<section class="review-section" id="thoughts">',
        "<h2>每条思路留下了什么</h2>",
        '<p class="section-intro">同一套字段讲清楚：它从哪里来、留下什么、'
        "怎样影响原问题，以及原话在哪里。</p>",
        '<div class="thought-list">',
    ]
    for stat in stats:
        tid = stat["tid"]
        a = annotations.get(tid, {})
        is_main = tid == main
        state, state_class = _state_label(stat, a, is_main)
        confidence = "Agent 已回读并核实" if confirmed else "仅按用户提问结构推断"
        yield_text = a.get("yield")
        if not yield_text:
            yield_text = (
                "尚未回读 AI 回复；现在只能确认这条思路出现过，"
                "不能可靠判断它产出了什么。"
            )
        relation = "它是本次讨论的主问题。" if is_main else (
            a.get("relation_note")
            or REL_LABEL.get(a.get("relation_to_trunk"))
            or "尚未核实它与主问题的关系。"
        )
        thought_id = f"thought-{session_key}-{tid}"
        parts.append(f'<article class="thought" id="{esc(thought_id)}">')
        for turn in stat["rounds"]:
            parts.append(
                f'<span class="source-anchor" id="{esc(prompt_anchor(res, turn))}" '
                'aria-hidden="true"></span>'
            )
        parts.append(
            '<header class="thought-head"><div class="thought-title-row">'
            f'<h3>{esc(thread_label(res, ann, tid))}</h3>'
            f'<span class="state{state_class}">{esc(state)}</span>'
            "</div>"
            f'<p class="confidence {"confirmed" if confirmed else "inferred"}">'
            f"{esc(confidence)}</p></header>"
        )
        parts.append('<dl class="thought-grid">')
        parts.append(
            '<div class="thought-field"><dt>从哪里来</dt>'
            f'<dd>{esc(_spawn_text(a))}</dd></div>'
        )
        parts.append(
            '<div class="thought-field"><dt>留下了什么</dt>'
            f'<dd>{esc(yield_text)}</dd></div>'
        )
        parts.append(
            '<div class="thought-field"><dt>对原问题的作用</dt>'
            f'<dd>{esc(relation)}</dd></div>'
        )
        open_note = ""
        if is_open_thread(stat, a):
            open_note = a.get("resolution_evidence") or (
                "这条思路后来没有得到明确回应。"
            )
        elif a.get("resolution_evidence"):
            open_note = a["resolution_evidence"]
        if open_note:
            parts.append(
                '<div class="thought-field"><dt>收束依据</dt>'
                f'<dd>{esc(open_note)}</dd></div>'
            )
        if a.get("agent_note"):
            parts.append(
                '<div class="thought-field"><dt>核对备注</dt>'
                f'<dd>{esc(a["agent_note"])}</dd></div>'
            )
        parts.append("</dl>")
        evidence = evidence_turn(stat, a)
        evidence_text = res["texts_full"][evidence - 1]
        parts.append(
            f'<blockquote class="origin-quote">“{esc(_one_line(evidence_text, 120))}”'
            "</blockquote>"
        )
        parts.append('<div class="thought-actions">')
        parts.append(_source_button(res, evidence))
        if stat["revisited"]:
            revisit = stat["dormancies"][0]["to"]
            parts.append(_source_button(res, revisit, "查看再次回到它时"))
        parts.append("</div></article>")
    parts.append("</div></section>")
    return "".join(parts)


def render_method(res: dict, ann: dict | None) -> str:
    stats = thread_stats(res)
    meta = res.get("analysis_meta") or {}
    corpus = meta.get("corpus") or {}
    filtered = meta.get("filtered_out") or {}
    parts = [
        '<details class="method"><summary>方法与诊断</summary>',
        '<div class="method-body">',
        '<p class="method-copy">这一层保留工程定位，用于核对算法和标注。'
        "它不承担用户的第一层理解，所以这里才出现记录号、结构主题 id 和阈值。</p>",
        '<table class="diagnostic"><thead><tr>'
        "<th>结构主题 id</th><th>用户可见名称</th><th>记录数</th>"
        "<th>起点</th><th>末次出现</th></tr></thead><tbody>",
    ]
    for stat in stats:
        parts.append(
            "<tr>"
            f"<td>{stat['tid']}</td>"
            f"<td>{esc(thread_label(res, ann, stat['tid']))}</td>"
            f"<td>{stat['count']}</td>"
            f"<td>{stat['first']}</td>"
            f"<td>{stat['last']}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    facts = {
        "analysis_id": meta.get("analysis_id"),
        "algorithm_version": meta.get("algorithm_version"),
        "theta": res.get("theta"),
        "history_corpus_size": corpus.get("history_size", corpus.get("size")),
        "fit_corpus_size": corpus.get("fit_size", corpus.get("size")),
        "corpus_degraded": corpus.get("degraded", False),
        "filtered_auto_entries": filtered.get("count", 0),
        "retries_collapsed": filtered.get("retries_collapsed", 0),
        "annotation_trust": (res.get("trust") or {}).get("annotations", "inferred"),
    }
    parts.append(f'<pre class="code-facts">{esc(json.dumps(facts, ensure_ascii=False, indent=2))}</pre>')
    parts.append("</div></details>")
    return "".join(parts)


def _duration(meta: dict) -> str:
    start, end = meta.get("first_time"), meta.get("last_time")
    if not start or not end:
        return ""
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return ""
    hours = (b - a).total_seconds() / 3600
    return f"{hours:.1f} 小时" if hours < 48 else f"{hours / 24:.1f} 天"


def source_data(name: str, res: dict, ann: dict | None) -> list[dict]:
    annotations = ann or {}
    labels = {stat["tid"]: thread_label(res, ann, stat["tid"])
              for stat in thread_stats(res)}
    tids = res.get("thread_id") or []
    texts = res.get("texts_full") or []
    times = res.get("timestamps") or []
    sources = []
    for index, text in enumerate(texts, start=1):
        tid = tids[index - 1] if index <= len(tids) else -1
        a = annotations.get(tid, {})
        topic = labels.get(tid, a.get("name", ""))
        if a.get("evidence_turn") and index < a["evidence_turn"]:
            topic = _one_line(text, 24)
        sources.append({
            "anchor": prompt_anchor(res, index),
            "turn": index,
            "thread": tid,
            "topic": topic,
            "text": text,
            "time": times[index - 1] if index <= len(times) else "",
        })
    return sources


def _safe_payload(value: object) -> str:
    return (json.dumps(value, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def report_identity(source_title: str, res: dict) -> dict[str, str]:
    """决定报告在用户层如何被命名。

    显式的报告语义优先；旧标注降级使用已核实的主问题名称；两者都没有时才
    使用宿主会话标题。这样既兼容旧数据，又不再把 Codex/Claude 的导航标题
    误当成复盘标题。
    """
    report_meta = res.get("report_meta") or {}
    title = str(report_meta.get("title") or "").strip()
    subtitle = str(report_meta.get("subtitle") or "").strip()

    if not title:
        annotations = res.get("annotations") or {}
        analysis_threads = res.get("analysis_threads") or {}
        trunk_id = next(
            (tid for tid, thread in analysis_threads.items()
             if thread.get("is_trunk")),
            0,
        )
        trunk = annotations.get(trunk_id) or {}
        title = str(trunk.get("name") or "").strip()

    if not title:
        title = str(source_title or "").strip() or "未命名对话复盘"

    if not subtitle:
        subtitle = (
            "沿用户原话重建这次讨论的转向、阶段性结果与仍然开放的问题。"
        )

    return {
        "title": title,
        "subtitle": subtitle,
        "source_title": str(source_title or "").strip(),
    }


def build_page(sessions: list[tuple], out_path: Path) -> None:
    """生成单文件 HTML 报告。"""
    visible = [(name, meta, res) for name, meta, res in sessions
               if res.get("n", 0) >= 2]
    identities = [report_identity(name, res) for name, _, res in visible]
    data = [
        {"name": name, "sources": source_data(name, res, res.get("annotations"))}
        for name, _, res in visible
    ]
    css = _asset_text("tokens.css") + "\n" + _asset_text("report.css")
    js = _asset_text("report.js")
    if len(identities) == 1:
        page_title = f"{identities[0]['title']}｜Breadcrumbs"
        masthead_title = identities[0]["title"]
        masthead_lede = identities[0]["subtitle"]
        source_context = identities[0]["source_title"]
    else:
        page_title = "Breadcrumbs · 多段对话复盘"
        masthead_title = "把走散的思路，重新放回手边。"
        masthead_lede = (
            "把讨论走过的路、仍然开放的问题和当时的原话放在一起，"
            "让你可以重新进入工作。"
        )
        source_context = ""

    parts = [
        "<!doctype html>",
        '<html lang="zh-CN"><head>',
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">',
        f"<title>{esc(page_title)}</title>",
        f"<style>{css}</style>",
        "</head><body>",
        '<main class="page-shell">',
        '<header class="masthead">',
        '<p class="wordmark">Breadcrumbs · 沿原话返回</p>',
    ]
    if source_context:
        parts.append(
            f'<p class="report-context">{esc(source_context)} · 对话复盘</p>'
        )
    parts.extend([
        f"<h1>{esc(masthead_title)}</h1>",
        f'<p class="lede">{esc(masthead_lede)}</p>',
        "</header>",
    ])

    for index, (name, meta, res) in enumerate(visible):
        identity_meta = identities[index]
        ann = res.get("annotations")
        key_material = (
            (res.get("analysis_meta") or {}).get("session_fingerprint")
            or f"{index}-{name}"
        )
        session_key = hashlib.sha256(
            str(key_material).encode("utf-8")
        ).hexdigest()[:10]
        trust_class, trust_copy = trust_state(res)
        duration = _duration(meta)
        meta_bits = [bit for bit in (meta.get("cwd"), duration) if bit]
        parts.append(f'<section class="session" data-session="{index}">')
        parts.append(
            '<header class="session-heading'
            + (' session-heading-meta' if len(visible) == 1 else '')
            + '">'
        )
        if len(visible) > 1:
            if identity_meta["source_title"]:
                parts.append(
                    f'<p class="session-context">'
                    f'{esc(identity_meta["source_title"])} · 对话复盘</p>'
                )
            parts.append(f"<h2>{esc(identity_meta['title'])}</h2>")
            parts.append(
                f'<p class="session-summary">'
                f'{esc(identity_meta["subtitle"])}</p>'
            )
        if meta_bits:
            parts.append(f'<p class="session-meta">{esc(" · ".join(meta_bits))}</p>')
        parts.append("</header>")
        parts.append(
            f'<div class="trust-note {esc(trust_class)}">'
            '<span class="trust-mark" aria-hidden="true"></span>'
            f"<p>{esc(trust_copy)}</p></div>"
        )
        parts.append('<div class="review-layout">')
        parts.append(render_path(res, ann, session_key))
        parts.append('<article class="review-body">')
        parts.append(render_changes(res, ann))
        parts.append(render_recommendation(res, ann))
        parts.append(render_thoughts(res, ann, session_key))
        parts.append(render_method(res, ann))
        parts.append("</article></div></section>")

    if not visible:
        parts.append(
            '<section class="session-heading"><h2>没有可复盘的会话</h2>'
            "<p>至少需要两条真人输入。</p></section>"
        )
    parts.extend([
        '<footer class="foot-line"><p>Breadcrumbs · 本地生成 · 原话不离开设备</p></footer>',
        "</main>",
        '<dialog class="source-dialog" id="source-dialog" aria-labelledby="source-title">',
        '<div class="source-inner">',
        '<button class="dialog-close" type="button" aria-label="关闭原话">×</button>',
        '<p class="dialog-label"></p>',
        '<h2 class="source-title" id="source-title"></h2>',
        '<pre class="source-text"></pre>',
        '<details class="source-tech"><summary>技术定位</summary>',
        '<pre class="source-tech-body"></pre></details>',
        "</div></dialog>",
        f"<script>window.__DATA__={_safe_payload(data)};</script>",
        f"<script>{js}</script>",
        "</body></html>",
    ])
    out_path.write_text("\n".join(parts), encoding="utf-8")

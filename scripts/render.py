"""把结构分析渲染成一份可以重新进入工作的复盘稿。

结果页分三层：
1. 「来路图」与持续可见的小地图——先看见分叉，再在阅读时保持空间感；
2. 复盘正文与返回提示词——回答改变、建议继续项、每条思路留下的东西；
3. 完整讨论结构、原话与诊断——逐条核对，工程编号只留在最后一层。

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
import re


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


def ui_text(value: object) -> str:
    """统一系统生成文案的中文括号，但不改动原话证据。"""
    text = str(value or "").replace("〈", "「").replace("〉", "」")
    text = re.sub(r"<([^<>\n]+)>", r"「\1」", text)
    return re.sub(r"＜([^＜＞\n]+)＞", r"「\1」", text)


def quote_ui(value: object) -> str:
    text = ui_text(value)
    return text if text.startswith("「") and text.endswith("」") else f"「{text}」"


def _one_line(value: object, limit: int = 64) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def latest_user_action(value: object, limit: int = 64) -> str:
    """显示最后一条有效用户内容，不拿 thread 名或系统分类词冒充用户语言。"""
    text = " ".join(str(value or "").split())
    if not text:
        return "尚未识别"
    for prefix in (
        "打错了，应为：", "打错了，应为:", "应为：", "应为:",
        "改为：", "改为:",
    ):
        if text.startswith(prefix):
            return _one_line(text[len(prefix):], limit)
    if text.startswith("另外"):
        detail = text.removeprefix("另外").lstrip("，,：: ")
        return _one_line(detail, limit)
    return _one_line(text, limit)


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
        return ui_text(annotation["name"])
    stat = next((s for s in thread_stats(res) if s["tid"] == tid), None)
    return ui_text(_one_line((stat or {}).get("head"), 24)) or "一段尚未命名的追问"


def is_open_thread(stat: dict, annotation: dict) -> bool:
    if annotation.get("resolved") is not None:
        return annotation["resolved"] is False
    return stat["dangling"]


def open_threads(res: dict, ann: dict | None) -> list[dict]:
    annotations = ann or {}
    stats = thread_stats(res)
    opened = [
        stat for stat in stats
        if is_open_thread(stat, annotations.get(stat["tid"], {}))
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
    if chosen["tid"] == trunk_id(res):
        reasons.append("当前主问题仍在推进")
    elif rel == "blocked":
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
            "run_start": first,
            "run_end": last,
            "verb": verb,
            "label": semantic_label,
            "confirmed": semantic_confirmed,
            # 回访同一条思路时名字会原样重复出现（主线尤其明显）。重复的恰好
            # 是最长、信息量最低的那串字, 会挤掉相邻新思路的可读宽度。
            # 标成重访, 让渲染层收窄它——名字用户在上游已经读过一次了。
            "repeat": tid in seen,
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
            "run_start": final_turn,
            "run_end": final_turn,
            "verb": "最后停在",
            "label": thread_label(res, ann, final_tid),
            "confirmed": bool((ann or {}).get(final_tid)),
            "repeat": False,
            "anchor": prompt_anchor(res, final_turn),
            "quote": _one_line(res["texts_full"][final_turn - 1], 38),
        })

    return stops


def breadcrumb_geometry(res: dict, ann: dict | None) -> dict:
    """左侧面包屑沿用逐站阅读路径，不跟随顶部来路图的概览压缩。

    它的任务是随着正文位置点亮「当前读到哪」，因此必须保留转去与回到；
    顶部来路图才负责把相同材料抽象成分叉—汇入结构。
    """
    stops = discussion_path(res, ann)
    lane_by_tid: dict[int, int] = {}
    for stop in stops:
        lane_by_tid.setdefault(stop["tid"], len(lane_by_tid))
    steps_by_to = {
        step.get("to"): step for step in res.get("steps", [])
        if step.get("to")
    }
    edges = []
    for index, stop in enumerate(stops):
        if index == 0:
            continue
        parent_index = index - 1
        first_for_thread = all(
            earlier.get("tid") != stop["tid"] for earlier in stops[:index]
        )
        if first_for_thread:
            link_to = (steps_by_to.get(stop["run_start"]) or {}).get("link_to")
            if link_to:
                for candidate in range(index - 1, -1, -1):
                    prior = stops[candidate]
                    if prior["run_start"] <= link_to <= prior["run_end"]:
                        parent_index = candidate
                        break
        edges.append({
            "from": parent_index,
            "to": index,
            "from_tid": stops[parent_index]["tid"],
            "to_tid": stop["tid"],
            "returning": stop["verb"] == "回到",
        })
    return {
        "stops": stops,
        "edges": edges,
        "lanes": lane_by_tid,
        "lane_count": max(1, len(lane_by_tid)),
    }


def route_geometry(res: dict, ann: dict | None) -> dict:
    """为全宽地图和侧栏小地图建立同一份真正的分叉拓扑。

    旧实现把每次话题切换首尾相连，因此即使道路上下转弯，拓扑仍是一条折线。
    这里把「概览」压缩为每条思路的首次出现，并根据之后是否回到旧思路识别
    被暂时搁置的父问题。旁支从父问题分出，再汇入通往最终思路的主路；逐次
    往返仍由下方「完整讨论结构」保留。
    """
    all_stops = discussion_path(res, ann)
    if not all_stops:
        return {"stops": [], "edges": [], "lanes": {}, "lane_count": 1}

    first_stop_by_tid: dict[int, dict] = {}
    tid_order: list[int] = []
    for stop in all_stops:
        if stop["tid"] not in first_stop_by_tid:
            first_stop_by_tid[stop["tid"]] = dict(stop)
            tid_order.append(stop["tid"])

    first_turn = {tid: first_stop_by_tid[tid]["turn"] for tid in tid_order}
    last_turn: dict[int, int] = {}
    count_by_tid: dict[int, int] = {}
    # 这里必须看完整 turn 序列，而不是压缩后的站点数。否则一个长期反复推进的
    # 主问题会和只出现过两次的岔题同权，后半段分支就会被错误挂回旧节点。
    for turn, tid in enumerate(res.get("thread_id") or [], start=1):
        last_turn[tid] = turn
        count_by_tid[tid] = count_by_tid.get(tid, 0) + 1

    # 新思路通常是从一个稍后又被捡起的旧问题旁逸出去。候选中优先选择反复
    # 出现最多的旧思路，避免把连续两个短暂岔题误画成越来越深的一条链。
    parent_by_tid: dict[int, int | None] = {tid_order[0]: None}
    seen: list[int] = [tid_order[0]]
    for tid in tid_order[1:]:
        turn = first_turn[tid]
        candidates = [old for old in seen if last_turn.get(old, 0) > turn]
        if candidates:
            parent = max(
                candidates,
                key=lambda old: (count_by_tid.get(old, 0), first_turn.get(old, 0)),
            )
        else:
            parent = seen[-1]
        parent_by_tid[tid] = parent
        seen.append(tid)

    final_tid = all_stops[-1]["tid"]
    lineage: list[int] = []
    cursor: int | None = final_tid
    while cursor is not None and cursor not in lineage:
        lineage.append(cursor)
        cursor = parent_by_tid.get(cursor)
    lineage.reverse()
    lineage_set = set(lineage)

    stops = [first_stop_by_tid[tid] for tid in tid_order]
    for stop in stops:
        stop["repeat"] = False
        stop["route_role"] = "main" if stop["tid"] in lineage_set else "branch"
    stop_index = {stop["tid"]: index for index, stop in enumerate(stops)}

    steps_by_to = {
        step.get("to"): step for step in res.get("steps", [])
        if step.get("to")
    }
    branch_similarity = {
        tid: float(
            (steps_by_to.get(first_stop_by_tid[tid]["run_start"]) or {})
            .get("link_sim") or 0
        )
        for tid in tid_order[1:]
    }
    ranked_similarity = sorted(branch_similarity.values())

    def _drift_level(tid: int) -> str:
        values = ranked_similarity
        if len(values) <= 1:
            return "中"
        value = branch_similarity.get(tid, 0)
        rank = sum(item < value for item in values) / (len(values) - 1)
        return "高" if rank <= 0.33 else "低" if rank >= 0.67 else "中"

    edges = []
    for tid in tid_order[1:]:
        stop = first_stop_by_tid[tid]
        # 字符 TF-IDF 的绝对值会随语料变化，所以只做会话内相对分档。
        # 它表示与分叉前问题的语义距离，不评价分叉的价值。
        if stop.get("route_role") == "branch":
            stop["drift_level"] = _drift_level(tid)
        parent_tid = parent_by_tid[tid]
        edges.append({
            "from": stop_index[parent_tid],
            "to": stop_index[tid],
            "from_tid": parent_tid,
            "to_tid": tid,
            "returning": False,
        })

    # 最终思路若在中途已出现，再放一个「最后停在」端点。主路穿过它，旁支
    # 则从同一分叉点出发后汇入端点，由此形成用户能一眼认出的岔路形状。
    final_index = stop_index[final_tid]
    if all_stops[-1]["turn"] != first_stop_by_tid[final_tid]["turn"]:
        endpoint = dict(all_stops[-1])
        endpoint["repeat"] = True
        endpoint["route_role"] = "endpoint"
        endpoint["drift_level"] = None
        endpoint["verb"] = "最后停在"
        stops.append(endpoint)
        endpoint_index = len(stops) - 1
        edges.append({
            "from": final_index,
            "to": endpoint_index,
            "from_tid": final_tid,
            "to_tid": final_tid,
            "returning": True,
        })

        next_on_lineage = {
            lineage[index]: lineage[index + 1]
            for index in range(len(lineage) - 1)
        }
        for tid in tid_order:
            if tid in lineage_set:
                continue
            parent_tid = parent_by_tid.get(tid)
            merge_tid = next_on_lineage.get(parent_tid)
            merge_index = (
                stop_index.get(merge_tid)
                if merge_tid is not None
                else endpoint_index if parent_tid == final_tid else None
            )
            if merge_index is not None:
                edges.append({
                    "from": stop_index[tid],
                    "to": merge_index,
                    "from_tid": tid,
                    "to_tid": stops[merge_index]["tid"],
                    "returning": True,
                })

    lane_by_tid = {tid: index for index, tid in enumerate(tid_order)}
    return {
        "stops": stops,
        "edges": edges,
        "lanes": lane_by_tid,
        "lane_count": max(1, len(lane_by_tid)),
        "lineage": lineage,
        "parents": parent_by_tid,
    }


# 新思路的节点给足宽度——名字被截成「某条支线提前出现在…」就等于没写。
# 重访节点收窄: 那串名字用户在上游已经读过, 这里只需要认出「回到了哪条」。
STEP_NEW = 196
STEP_REPEAT = 152


def _route_dimensions(route: dict) -> tuple[int, int, list[tuple[int, int]]]:
    stops = route["stops"]
    if not stops:
        return 520, 188, []
    lineage = route.get("lineage") or [stops[0]["tid"]]
    parents = route.get("parents") or {}
    lineage_pos = {tid: index for index, tid in enumerate(lineage)}
    stage_width = 390
    center_y = 174
    branch_gap = 116
    endpoint_index = next(
        (i for i, stop in enumerate(stops) if stop.get("route_role") == "endpoint"),
        None,
    )
    points: list[tuple[int, int]] = [(96, center_y)] * len(stops)

    for index, stop in enumerate(stops):
        tid = stop["tid"]
        if stop.get("route_role") == "endpoint":
            points[index] = (96 + len(lineage) * stage_width, center_y)
        elif tid in lineage_pos:
            points[index] = (96 + lineage_pos[tid] * stage_width, center_y)

    branch_count: dict[int, int] = {}
    for index, stop in enumerate(stops):
        if stop.get("route_role") != "branch":
            continue
        parent = parents.get(stop["tid"])
        parent_stage = lineage_pos.get(parent, 0)
        rank = branch_count.get(parent, 0)
        branch_count[parent] = rank + 1
        direction = -1 if rank % 2 == 0 else 1
        level = 1 + rank // 2
        points[index] = (
            96 + parent_stage * stage_width + stage_width // 2,
            center_y + direction * level * branch_gap,
        )

    width = max(520, max(x for x, _ in points) + 110)
    highest = min(y for _, y in points)
    lowest = max(y for _, y in points)
    if highest < 48:
        shift = 48 - highest
        points = [(x, y + shift) for x, y in points]
        lowest += shift
    height = max(248, lowest + 92)
    return width, height, points


def _curve(a: tuple[int, int], b: tuple[int, int]) -> str:
    x1, y1 = a
    x2, y2 = b
    delta = max(44, (x2 - x1) * 0.48)
    # 同一泳道也必须像道路一样有弧度；两个控制点都落在端点高度上会退化成直线。
    if abs(y2 - y1) < 1:
        arch = -28
        return (
            f"M {x1} {y1} C {x1 + delta} {y1 + arch}, "
            f"{x2 - delta} {y2 + arch}, {x2} {y2}"
        )
    return f"M {x1} {y1} C {x1 + delta} {y1}, {x2 - delta} {y2}, {x2} {y2}"


def render_route_overview(res: dict, ann: dict | None, session_key: str) -> str:
    route = route_geometry(res, ann)
    stats_by_tid = {stat["tid"]: stat for stat in thread_stats(res)}
    width, height, points = _route_dimensions(route)
    parts = [
        f'<section class="route-overview" id="route-{esc(session_key)}" '
        f'data-session-key="{esc(session_key)}">',
        '<header class="route-heading">',
        '<div><h2>一、来路图</h2>'
        '<p class="orientation-subtitle">你是怎么走到终点的</p></div>',
        '<p class="map-instructions" aria-label="来路图使用方法">'
        f'共 {len(route["stops"])} 站，可横向滚动'
        '<br>点名称定位 · 点「原话」核对 · 点「返回」继续</p>',
        '</header>',
        '<div class="route-scroll" tabindex="0" aria-label="可横向滚动的来路图">',
        f'<div class="route-map" style="--map-width:{width}px;--map-height:{height}px">',
        f'<svg class="route-lines" viewBox="0 0 {width} {height}" '
        'aria-hidden="true" preserveAspectRatio="none">',
    ]
    for edge in route["edges"]:
        a, b = points[edge["from"]], points[edge["to"]]
        state = " returning" if edge["returning"] else ""
        parts.append(
            f'<path class="route-edge{state}" d="{_curve(a, b)}" '
            f'data-route-edge data-session-key="{esc(session_key)}" '
            f'data-from-thread="{edge["from_tid"]}" '
            f'data-to-thread="{edge["to_tid"]}"></path>'
        )
    parts.append("</svg>")
    for index, stop in enumerate(route["stops"]):
        x, y = points[index]
        thought_id = f"thought-{session_key}-{stop['tid']}"
        state = "confirmed" if stop.get("confirmed") else "inferred"
        stat = stats_by_tid.get(stop["tid"])
        return_control = ""
        if stat and is_open_thread(stat, (ann or {}).get(stop["tid"], {})):
            return_control = (
                f'<button class="route-return" type="button" '
                f'data-copy-prompt="map-resume-{esc(session_key)}-{stop["tid"]}">'
                "返回</button>"
            )
        repeat = " repeat" if stop.get("repeat") else ""
        branch = " branch" if stop.get("route_role") == "branch" else ""
        parts.append(
            f'<div class="route-stop {state}{repeat}{branch}" '
            f'style="--route-x:{x}px;--route-y:{y}px" '
            f'data-thread="{stop["tid"]}">'
            f'<a class="route-node" href="#{esc(thought_id)}" '
            f'data-route-node data-session-key="{esc(session_key)}" '
            f'data-thread="{stop["tid"]}" '
            f'title="{esc(stop["label"])}">'
            f'<span class="route-verb">{esc(stop["verb"])}</span>'
            f'<strong>{esc(stop["label"])}</strong></a>'
            '<div class="route-node-actions">'
            f'<button class="route-source" type="button" '
            f'data-source="{esc(stop["anchor"])}">原话</button>'
            f'{return_control}</div>'
            "</div>"
        )
    emitted: set[int] = set()
    for stat in stats_by_tid.values():
        if stat["tid"] in emitted:
            continue
        if is_open_thread(stat, (ann or {}).get(stat["tid"], {})):
            emitted.add(stat["tid"])
            parts.append(
                f'<template id="map-resume-{esc(session_key)}-{stat["tid"]}">'
                f'{esc(_resume_prompt(res, ann, stat))}</template>'
            )
    parts.extend(["</div></div>", "</section>"])
    return "".join(parts)


def _resume_prompt(res: dict, ann: dict | None, stat: dict) -> str:
    a = (ann or {}).get(stat["tid"], {})
    name = thread_label(res, ann, stat["tid"])
    evidence = evidence_turn(stat, a)
    quote = _one_line(res["texts_full"][evidence - 1], 220)
    relation = (
        a.get("relation_note")
        or REL_LABEL.get(a.get("relation_to_trunk"))
        or "与原问题的关系还需要重新确认"
    )
    yielded = a.get("yield") or "目前只确认这条思路出现过，尚未核实已有产出"
    return (
        f"我想回到这段对话里{quote_ui(name)}这条思路。\n\n"
        f"当时我的原话是：{quote}\n"
        f"它与原问题的关系：{relation}。\n"
        f"目前留下：{yielded}。\n\n"
        "请基于当前对话上下文，从这个未完成点继续。先用 2—3 句确认已经确定的"
        "内容和仍缺的内容，再提出一个可验证的下一步；如果上下文不足，请明确"
        "指出缺什么，不要自行扩展到其他支线。"
    )


def _resume_control(res: dict, ann: dict | None, stat: dict,
                    session_key: str, prominent: bool = False) -> str:
    suffix = "-featured" if prominent else ""
    prompt_id = f"resume-{session_key}-{stat['tid']}{suffix}"
    klass = " resume-copy-primary" if prominent else ""
    return (
        f'<template id="{esc(prompt_id)}">{esc(_resume_prompt(res, ann, stat))}</template>'
        f'<button class="resume-copy{klass}" type="button" '
        f'data-copy-prompt="{esc(prompt_id)}">复制返回提示词</button>'
    )


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
    route = breadcrumb_geometry(res, ann)
    mini_height = max(132, 32 + len(route["stops"]) * 58)
    mini_points = []
    for index, stop in enumerate(route["stops"]):
        mini_points.append((
            18 + min(route["lanes"].get(stop.get("tid"), 0), 5) * 28,
            24 + index * 58,
        ))
    parts = [
        '<aside class="path-panel" aria-label="思路的面包屑">',
        '<div class="path-panel-head">'
        '<span class="crumb-mark" aria-hidden="true">'
        '<svg viewBox="0 0 32 24" focusable="false">'
        '<path d="M3.5 9.2C3.5 5.8 6.1 3.5 9.5 3.5h4.2c3.4 0 6 2.3 6 5.7v8.3'
        'c0 1.7-1.3 3-3 3H6.5c-1.7 0-3-1.3-3-3Z"></path>'
        '<path d="M8 8.2c1 .6 1.5 1.5 1.6 2.7M13.2 7.2c1 .7 1.5 1.6 1.5 2.8"></path>'
        '<circle cx="23.5" cy="10" r="1"></circle>'
        '<circle cx="27.5" cy="14" r="1.25"></circle>'
        '<circle cx="30.5" cy="19" r="1.5"></circle>'
        '</svg></span>'
        '<h2>思路的面包屑</h2></div>',
        '<p class="path-intro">进入具体思路时，对应节点会点亮。</p>',
        f'<div class="route-minimap" style="--mini-height:{mini_height}px" '
        'aria-label="来路图缩略导航">',
        f'<svg viewBox="0 0 260 {mini_height}" aria-hidden="true">',
    ]
    for edge in route["edges"]:
        a, b = mini_points[edge["from"]], mini_points[edge["to"]]
        parts.append(
            f'<path class="mini-edge" d="{_curve(a, b)}" '
            f'data-route-edge data-session-key="{esc(session_key)}" '
            f'data-from-thread="{edge["from_tid"]}" '
            f'data-to-thread="{edge["to_tid"]}"></path>'
        )
    parts.append("</svg>")
    for index, stop in enumerate(route["stops"]):
        x, y = mini_points[index]
        klass = "confirmed" if stop.get("confirmed") else "inferred"
        if stop.get("drift_level"):
            klass += " branch"
        thought_id = f"thought-{session_key}-{stop['tid']}"
        parts.append(
            f'<a class="mini-node {klass}" href="#{esc(thought_id)}" '
            f'style="--mini-x:{x}px;--mini-y:{y}px" '
            f'data-route-node data-session-key="{esc(session_key)}" '
            f'data-thread="{stop["tid"]}" title="{esc(stop["label"])}">'
            f'<span class="mini-dot" aria-hidden="true"></span>'
            f'<span class="mini-node-copy"><strong>{esc(stop["label"])}</strong>'
            f'</span></a>'
        )
    parts.extend([
        "</div>",
        "</aside>",
    ])
    return "".join(parts)


def render_summary(res: dict, ann: dict | None, session_key: str) -> str:
    annotations = ann or {}
    stats = thread_stats(res)
    main = trunk_id(res, stats)
    opened = open_threads(res, ann)
    stops = discussion_path(res, ann)
    origin_texts = res.get("texts_full") or []
    current = latest_user_action(origin_texts[-1]) if origin_texts else "尚未识别"
    current_tid = stops[-1]["tid"] if stops else None
    current_is_open = any(stat["tid"] == current_tid for stat in opened)
    other_opened = [stat for stat in opened if stat["tid"] != current_tid]
    if other_opened:
        open_text = "、".join(
            thread_label(res, ann, stat["tid"]) for stat in other_opened[:2]
        )
        if len(other_opened) > 2:
            open_text += f"等 {len(other_opened)} 条"
        if current_is_open:
            open_text += "；当前停留的问题也未收束"
    elif current_is_open:
        open_text = "当前停留的问题尚未收束"
    else:
        open_text = "都已收束"
    origin_turn = 1
    origin_text = (
        _one_line(origin_texts[0], 56) if origin_texts else "尚未识别"
    )

    changes = []
    for stat in stats:
        if stat["tid"] == main:
            continue
        a = annotations.get(stat["tid"], {})
        rel = a.get("relation_to_trunk")
        if rel in {"redirected", "supplied", "blocked"}:
            changes.append((stat, a, rel))

    parts = [
        f'<section class="review-summary" id="summary-{esc(session_key)}">'
        '<header><h2>二、复盘总览</h2>'
        '<p class="orientation-subtitle summary-step-title">'
        '1. 先认出来路，再决定从哪里继续</p></header>'
        '<dl class="summary-ledger">'
        f'<div><dt>最初在问</dt><dd>{esc(origin_text)}</dd>'
        f'{_source_button(res, origin_turn, "查看起点原话")}</div>'
        f'<div><dt>最后停在</dt><dd>{esc(current)}</dd></div>'
        f'<div><dt>未尽事宜</dt><dd>{esc(open_text)}</dd></div>'
        '</dl>'
        '<div class="summary-outcomes">'
        f'<section class="summary-outcome" id="changes-{esc(session_key)}">'
        '<h3>2. 这次改变了什么</h3>'
    ]
    if changes:
        parts.append('<div class="summary-change-list">')
        for stat, a, rel in changes:
            name = thread_label(res, ann, stat["tid"])
            body = a.get("relation_note") or a.get("yield") or REL_LABEL[rel]
            parts.append(
                '<article>'
                f'<h4>{esc(quote_ui(name))}{esc(REL_LABEL[rel])}</h4>'
                f'<p>{esc(body)}</p>'
                f'{_source_button(res, evidence_turn(stat, a))}'
                '</article>'
            )
        parts.append('</div>')
    else:
        parts.append('<p>目前只能确认讨论路径发生过变化，'
                     '还没有足够证据概括观点如何改变。</p>')
    parts.append('</section>')

    picked = recommendation(res, ann)
    parts.append(
        f'<section class="summary-outcome" id="continue-{esc(session_key)}">'
        '<h3>3. 建议优先继续</h3>'
    )
    if picked is None:
        # 上面的账本已经说过「都已收束」，这里不复述状态，只说它意味着什么。
        parts.append('<h4>这次没有需要追回的岔口</h4>'
                     '<p>每条派生思路都已收束或回到主问题；'
                     '要继续的话，从主问题本身接着走。</p>')
    else:
        stat, reason = picked
        a = annotations.get(stat["tid"], {})
        parts.append(f'<h4>{esc(thread_label(res, ann, stat["tid"]))}</h4>')
        parts.append(f'<p>{esc(reason)}</p>')
        if a.get("yield"):
            parts.append(f'<p>已经留下：{esc(a["yield"])}</p>')
        parts.append('<div class="recommendation-actions">')
        parts.append(_source_button(res, evidence_turn(stat, a), "查看当时原话"))
        parts.append(_resume_control(res, ann, stat, session_key))
        parts.append('</div>')
    parts.extend([
        '</section></div>',
        '</section>',
    ])
    return ''.join(parts)


def render_changes(res: dict, ann: dict | None, session_key: str) -> str:
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
        f'<section class="review-section change-section" '
        f'id="changes-{esc(session_key)}" data-review-section>',
        '<header class="section-head"><h2>这次改变了什么</h2></header>',
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
                f'<h3>{esc(quote_ui(name))}{esc(REL_LABEL[rel])}</h3>'
                f'<p>{esc(body)}</p>'
                f'{_source_button(res, evidence_turn(stat, a))}'
                "</article>"
            )
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def render_recommendation(res: dict, ann: dict | None,
                          session_key: str) -> str:
    picked = recommendation(res, ann)
    parts = [
        f'<section class="review-section continue-section" '
        f'id="continue-{esc(session_key)}" data-review-section>',
        '<header class="section-head"><h2>建议优先继续</h2></header>',
        '<p class="section-intro">这是一条可推翻的编辑建议，不是“漂移质量分”。'
        "排序只看它是否仍开放、怎样影响主问题、是否被你重新捡起。</p>",
    ]
    if picked is None:
        parts.append(
            '<div class="recommendation"><p class="recommendation-label">'
            '当前判断</p><h3>没有明显悬空的问题</h3>'
            "<p>现有标注里，每条派生思路都已收束或回到主问题。</p></div>"
        )
    else:
        stat, reason = picked
        a = (ann or {}).get(stat["tid"], {})
        parts.append('<div class="recommendation">')
        parts.append('<p class="recommendation-label">可返回的未完成点</p>')
        parts.append(f'<h3>{esc(thread_label(res, ann, stat["tid"]))}</h3>')
        parts.append(f'<p class="reason">{esc(reason)}</p>')
        if a.get("yield"):
            parts.append(f'<p>已经留下：{esc(a["yield"])}</p>')
        parts.append('<div class="recommendation-actions">')
        parts.append(_source_button(res, evidence_turn(stat, a), "查看当时原话"))
        parts.append(_resume_control(res, ann, stat, session_key))
        parts.append("</div>")
        parts.append("</div>")
    parts.append("</section>")
    return "".join(parts)


def _state_label(stat: dict, a: dict, is_main: bool) -> tuple[str, str]:
    if a.get("resolved") is True:
        return "已收束", ""
    if a.get("resolved") is False:
        return "未收束", " open"
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
    drift_by_tid = {
        stop["tid"]: stop["drift_level"]
        for stop in route_geometry(res, ann)["stops"]
        if stop.get("drift_level")
    }
    confirmed = (res.get("trust") or {}).get("annotations") == "confirmed"
    parts = [
        f'<section class="review-section thoughts-section" '
        f'id="thoughts-{esc(session_key)}" data-review-section>',
        '<header class="section-head"><h2>三、思路复盘</h2></header>',
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
        drift = ""
        if tid in drift_by_tid:
            drift = (
                f'<em class="drift-badge drift-{esc(drift_by_tid[tid])}" '
                'title="表示与分叉前问题的语义距离，不评价这次漂移好坏">'
                f'注意力漂移 {esc(drift_by_tid[tid])}</em>'
            )
        parts.append(
            f'<article class="thought" id="{esc(thought_id)}" '
            f'data-thought data-session-key="{esc(session_key)}" '
            f'data-thread="{tid}">'
        )
        for turn in stat["rounds"]:
            parts.append(
                f'<span class="source-anchor" id="{esc(prompt_anchor(res, turn))}" '
                'aria-hidden="true"></span>'
            )
        parts.append(
            '<header class="thought-head"><div class="thought-title-row">'
            '<div class="thought-title-main">'
            f'<h3>{esc(thread_label(res, ann, tid))}</h3>{drift}</div>'
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
        # agent_note 按定义就是「推翻算法判断或记录误判」——它谈的是算法而不是
        # 讨论本身，实测里多半带着「轮 3」「11 轮」这类记录号。放在正文一级
        # 会直接破坏「用户可见层不出现工程标识」，所以整块下沉到方法与诊断。
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
        parts.append("</div>")
        if is_open_thread(stat, a):
            prompt_id = f"resume-{session_key}-{stat['tid']}"
            parts.append(
                '<details class="return-kit">'
                '<summary>带着上下文回到这里</summary>'
                '<div class="return-kit-body">'
                '<p>这段提示词复用已有名称、原话和产出，不重新总结整场对话。</p>'
                f'<pre>{esc(_resume_prompt(res, ann, stat))}</pre>'
                f'<template id="{esc(prompt_id)}">'
                f'{esc(_resume_prompt(res, ann, stat))}</template>'
                f'<button class="resume-copy" type="button" '
                f'data-copy-prompt="{esc(prompt_id)}">复制返回提示词</button>'
                '</div></details>'
            )
        parts.append("</article>")
    parts.append("</div></section>")
    return "".join(parts)


def render_detail_tree(res: dict, ann: dict | None, session_key: str) -> str:
    """保留旧树状图的空间直觉，但让节点直接使用用户原话。"""
    tids = res.get("thread_id") or []
    texts = res.get("texts_full") or []
    if not tids or not texts:
        return ""
    lane_order: dict[int, int] = {}
    for tid in tids:
        lane_order.setdefault(tid, len(lane_order))
    # 完整结构要保留分叉，但不能把每条算法主题都摊成一个横向泳道：真实长
    # 会话很容易得到七八列，容器虽未越界，右侧内容却长期藏在横向滚动区。
    # 主问题居中，其他思路在左右两侧交替复用视觉带；纵向顺序仍保留每条原话。
    main_tid = trunk_id(res)
    visual_lane: dict[int, int] = {}
    side_index = 0
    for tid in lane_order:
        if tid == main_tid:
            visual_lane[tid] = 1
        else:
            visual_lane[tid] = 0 if side_index % 2 == 0 else 2
            side_index += 1
    lane_width = 230
    row_height = 66
    width = 740 if len(lane_order) > 1 else 600
    height = max(240, 70 + len(texts) * row_height)
    points = {
        turn: (
            70 + visual_lane.get(tids[turn - 1], 1) * lane_width,
            42 + (turn - 1) * row_height,
        )
        for turn in range(1, min(len(tids), len(texts)) + 1)
    }
    parents = {1: None}
    for step in res.get("steps", []):
        turn = step.get("to")
        if turn in points:
            parent = step.get("link_to") or step.get("from")
            parents[turn] = parent if parent in points and parent < turn else turn - 1
    for turn in points:
        parents.setdefault(turn, turn - 1 if turn > 1 else None)

    parts = [
        f'<section class="review-section structure-section" '
        f'id="structure-{esc(session_key)}" '
        'data-review-section>',
        '<header class="section-head"><h2>完整讨论结构</h2></header>',
        '<p class="section-intro">来路图负责认方向；这里按原话逐条核对分叉。'
        '每一次提问都在，点任一节点可打开全文。</p>',
        # 默认展开: 折叠时这个一级章节只剩标题和一个开关, 视觉上等于不存在,
        # 与「完整讨论结构必须是正文一级章节」的定位矛盾。高度已由 CSS 限住。
        '<details class="structure-details" open>',
        '<summary><span>'
        '<strong>逐条原话</strong></span>'
        '<span class="summary-hint">折叠</span></summary>',
        '<div class="detail-tree-scroll" tabindex="0" '
        'aria-label="可横向和纵向滚动的完整讨论结构">',
        f'<div class="detail-tree" style="--tree-width:{width}px;'
        f'--tree-height:{height}px">',
        f'<svg viewBox="0 0 {width} {height}" aria-hidden="true">',
    ]
    for turn, parent in parents.items():
        if not parent or parent not in points:
            continue
        parts.append(
            f'<path d="{_curve(points[parent], points[turn])}"></path>'
        )
    parts.append("</svg>")
    previous_tid = None
    for turn, (x, y) in points.items():
        tid = tids[turn - 1]
        label = _one_line(texts[turn - 1], 34)
        # 线名只在换线时出一次。连着三个节点顶着同一个名字, 重复的是最长的
        # 那串字, 却不携带任何新信息——真正要读的是下面那句原话。
        head = (
            f'<span>{esc(thread_label(res, ann, tid))}</span>'
            if tid != previous_tid else ""
        )
        previous_tid = tid
        parts.append(
            f'<button class="detail-node" type="button" '
            f'style="--tree-x:{x}px;--tree-y:{y}px" '
            f'data-source="{esc(prompt_anchor(res, turn))}" '
            f'data-thread="{tid}" title="{esc(texts[turn - 1])}">'
            f'{head}<strong>{esc(label)}</strong></button>'
        )
    parts.extend(["</div></div>", "</details></section>"])
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

    # 标注者对算法判断的推翻与误判记录。它属于这一层, 不属于正文——
    # 它谈的是分线算法对不对, 不是这场讨论讲了什么。
    notes = [
        (stat, (ann or {}).get(stat["tid"], {}).get("agent_note"))
        for stat in stats
    ]
    notes = [(stat, note) for stat, note in notes if note]
    if notes:
        parts.append('<h4 class="method-subhead">标注者对算法判断的更正</h4>')
        parts.append('<dl class="method-notes">')
        for stat, note in notes:
            parts.append(
                f'<div><dt>结构主题 {stat["tid"]} · '
                f'{esc(thread_label(res, ann, stat["tid"]))}</dt>'
                f'<dd>{esc(note)}</dd></div>'
            )
        parts.append("</dl>")

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
    title = ui_text(report_meta.get("title")).strip()
    subtitle = ui_text(report_meta.get("subtitle")).strip()

    if not title:
        annotations = res.get("annotations") or {}
        analysis_threads = res.get("analysis_threads") or {}
        trunk_id = next(
            (tid for tid, thread in analysis_threads.items()
             if thread.get("is_trunk")),
            0,
        )
        trunk = annotations.get(trunk_id) or {}
        title = ui_text(trunk.get("name")).strip()

    if not title:
        title = ui_text(source_title).strip() or "未命名对话复盘"

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
        masthead_duration = _duration(visible[0][1]) if visible else ""
    else:
        page_title = "Breadcrumbs · 多段对话复盘"
        masthead_title = "把走散的思路，重新放回手边。"
        masthead_lede = (
            "把讨论走过的路、仍然开放的问题和当时的原话放在一起，"
            "让你可以重新进入工作。"
        )
        masthead_duration = ""
    masthead_trust = ""
    if len(visible) == 1:
        trust_class, trust_copy = trust_state(visible[0][2])
        masthead_trust = (
            f'<div class="trust-note masthead-trust {esc(trust_class)}">'
            '<span class="trust-mark" aria-hidden="true"></span>'
            f'<p>{esc(trust_copy)}</p></div>'
        )

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
        '<p class="wordmark">Breadcrumbs · 思路</p>',
    ]
    parts.extend([
        f"<h1>{esc(masthead_title)}</h1>",
        f'<p class="lede">{esc(masthead_lede)}</p>',
        (
            f'<p class="masthead-duration"><span>历时</span>'
            f'{esc(masthead_duration)}</p>'
            if masthead_duration else ""
        ),
        masthead_trust,
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
        meta_bits = [bit for bit in (duration,) if bit] if len(visible) > 1 else []
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
        if len(visible) > 1:
            parts.append(
                f'<div class="trust-note {esc(trust_class)}">'
                '<span class="trust-mark" aria-hidden="true"></span>'
                f"<p>{esc(trust_copy)}</p></div>"
            )
        parts.append('<div class="orientation">')
        parts.append(render_route_overview(res, ann, session_key))
        parts.append(render_summary(res, ann, session_key))
        parts.append("</div>")
        parts.append('<div class="review-layout">')
        parts.append(render_path(res, ann, session_key))
        parts.append('<article class="review-body">')
        parts.append(render_thoughts(res, ann, session_key))
        parts.append(render_detail_tree(res, ann, session_key))
        parts.append(render_method(res, ann))
        parts.append("</article></div></section>")

    if not visible:
        parts.append(
            '<section class="session-heading"><h2>没有可复盘的会话</h2>'
            "<p>至少需要两条真人输入。</p></section>"
        )
    parts.extend([
        '<footer class="foot-line"><p>Breadcrumbs · 思路 · 本地生成 · 原话不离开设备</p></footer>',
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

"""注意力分叉探针 v0.1

对一段会话的「真人 prompt 序列」计算三层特征, 找出候选分叉点。

三层的分工（这个分层是从真实数据里逼出来的, 不是先验设计）:

  L1 语义距离   —— 抓「话题跳转」。相邻 prompt 的字符 n-gram 余弦距离。
                   已知盲区: 对「框架修正」型分叉不敏感, 因为用词几乎不变。

  L2 话语标记   —— 抓「框架修正」。这类分叉会用「澄清一下」「我是说」
                   之类的词自我声明, 而这些词在语义上是废话, L1 必然忽略。
                   同时提供反向信号（「展开讲讲」= 延续, 不是分叉）。

  L3 结构信号   —— 时间间隔、长度突变。抓跨天续接、抑制误报。

刻意不做的事:
  * 不判断分叉「好不好」——只标位置和类型, 判断权留给用户。
  * 不合并成单一分数就完事——每层单独报告, 才能看出谁抓到了什么。

用法:
    python3 probe.py <session.jsonl> [更多会话...] --html report.html
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from textsim import (TfidfModel, sim_matrix, argmax, median,  # noqa: E402
                     mean)

sys.path.insert(0, str(Path(__file__).parent))
from extract_prompts import extract, session_meta  # noqa: E402


# ---------------------------------------------------------------- L2 话语标记

MARKERS = {
    "frame_correction": {
        "label": "框架修正",
        "weight": 1.0,          # 最高价值: 用户在重新定义问题本身
        "patterns": [
            r"澄清", r"我是说", r"我的意思", r"我想表达", r"想说的是",
            r"不是.{0,8}而是", r"误解", r"重新说", r"重新定义", r"更正",
            r"纠正", r"说错", r"确切地?说", r"严格来说", r"再次强调",
            r"重申", r"换句话说", r"准确地?说", r"我指的是",
        ],
    },
    "topic_jump": {
        "label": "话题跳转",
        "weight": 0.9,
        "patterns": [
            r"换个话题", r"另外", r"顺便", r"对了", r"新的想法",
            r"先不管", r"跳过", r"不说这个", r"我现在有", r"我突然想",
            r"抛开", r"先放下", r"下一个问题", r"另一个",
        ],
    },
    "backref": {
        "label": "回溯引用",
        "weight": 0.5,          # 指向旧线索: 可能是收束, 也可能是重开旧线
        "patterns": [
            r"你前面", r"你刚才", r"你刚说", r"之前提到", r"前面提到",
            r"回到", r"我们之前", r"上面说", r"刚提到", r"前文",
        ],
    },
    "negation": {
        "label": "否定纠偏",
        "weight": 0.8,
        "patterns": [
            r"不对", r"不是这样", r"我不同意", r"错了", r"有问题",
            r"不行", r"这不", r"并不是",
        ],
    },
    "meta": {
        "label": "元讨论",
        "weight": 0.6,          # 从内容层跳到过程层, 是一种层级切换
        "patterns": [
            r"你的计划", r"接下来", r"总结一下", r"复盘", r"我们现在",
            r"到哪一步", r"下一步", r"你打算", r"怎么做",
        ],
    },
    "continuation": {
        "label": "延续深化",
        "weight": -0.8,         # 负权重: 这是反分叉信号
        "patterns": [
            r"展开", r"详细", r"具体说", r"继续", r"再多说", r"深入",
            r"举个例子", r"为什么", r"进一步", r"细化",
        ],
    },
}

_COMPILED = {
    k: [re.compile(p) for p in v["patterns"]] for k, v in MARKERS.items()
}


def marker_hits(text: str) -> dict[str, list[str]]:
    """只看 prompt 开头一段——话语标记几乎总在句首宣告。"""
    head = text[:80]
    hits = {}
    for key, regs in _COMPILED.items():
        found = [r.pattern for r in regs if r.search(head)]
        if found:
            hits[key] = found
    return hits


def marker_score(hits: dict) -> float:
    return sum(MARKERS[k]["weight"] for k in hits)


# ---------------------------------------------------------------- L1 语义距离

# v0.1 的教训: 在单个会话（10~20 条 prompt）上现场 fit TF-IDF 完全没有分辨率
# ——min_df=1 让几乎每个 n-gram 都唯一, IDF 把共享词全压平, 结果任意两条
# prompt 的余弦相似度都趋近 0, 于是「处处都是分叉」。
# 修法: 用全部会话的 prompt 汇成大语料来 fit IDF, 再 transform 单个会话。

SHORT_PROMPT_CHARS = 20   # 短于此的多为「好」「你先改」这类确认, 无话题信号
                          # v0.2 教训: 设 12 时「可以，看起来不错哟，你先改」漏网被判新线


def fit_corpus(all_texts: list[str]) -> TfidfModel:
    """在汇总语料上拟合 IDF。语料越大, 相似度越有意义。"""
    return TfidfModel(lo=2, hi=4, min_df=2, sublinear_tf=True).fit(all_texts)


def semantic_matrix(texts: list[str], model: TfidfModel) -> list[list[float]]:
    """返回 n×n 余弦相似度矩阵（向量已 L2 归一）。"""
    return sim_matrix([model.transform(t) for t in texts])


# ---------------------------------------------------------------- L3 结构信号

def parse_ts(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- 主分析

NEW_THREAD_SIM = 0.05    # 与所有前文的最高相似度低于此 -> 判为新线
                         # 依据: 实测同主题 pair ~0.08-0.21, 跨主题 ~0.005-0.02


def _blocks_new_thread(hits: dict) -> bool:
    """判断话语标记是否否决「开了新线」。

    v0.2 教训: 原先把 backref（「我们之前」）也算作否决条件, 结果
    「我现在有一个新的想法…我们之前都在考察 X…」被判成延续——
    但这里的「之前」是**对比**用法, 恰恰在宣告转向。
    所以: 只有在出现延续标记、且没有任何转向标记时, 才否决新线。
    """
    pivot = {"topic_jump", "frame_correction", "negation"}
    if hits.keys() & pivot:
        return False
    return "continuation" in hits


def _is_real_return(best_sim: float, prev_sim: float, theta: float) -> bool:
    """「回到更早某轮」需要显著性, 否则 argmax 的噪声会让它满屏乱报。

    要求: 与旧轮的相似度既要明显高于与上一条的相似度, 本身也要够强。
    """
    return best_sim > theta * 1.5 and best_sim > prev_sim * 1.6


def analyze(prompts: list[dict], vec: TfidfModel,
            theta: float = NEW_THREAD_SIM) -> dict:
    """用「最近邻链接」重建追问线, 而不是对相邻步做距离阈值。

    对每条 prompt i, 找它最像的前文 j:
      * 全都不像            -> 开了一条新线
      * 最像的不是上一条    -> 回到了更早的某条线（跨过了中间轮次）
      * 最像的就是上一条    -> 延续
    这样直接长出树, 而不是先算分数再猜结构。
    """
    texts = [p["text"] for p in prompts]
    n = len(texts)
    if n < 2:
        return {"n": n, "steps": [], "threads": [], "note": "prompt 太少"}

    sim = semantic_matrix(texts, vec)
    sim_adj = [sim[i][i - 1] for i in range(1, n)]
    sim_to_first = [sim[i][0] for i in range(n)]

    # --- 自适应阈值 ---
    # v0.2 教训: 固定 θ=0.05 在 10 轮会话上正确, 在 92 轮上切出 24 条线。
    # 原因: 长会话里同一章节的不同段落, 词汇重叠天然就低。
    # 改用会话自身的相似度分布定标: 取各轮「最像的前文」相似度的中位数,
    # 新线门槛设为它的一个比例。这样密集讨论和松散讨论各自有合适的尺度。
    best_hist = []
    for i in range(1, n):
        if len(texts[i].strip()) >= SHORT_PROMPT_CHARS:
            best_hist.append(max(sim[i][:i]))
    if best_hist:
        med = median(best_hist)
        theta = max(0.015, min(0.05, 0.30 * med))
    else:
        theta = 0.02

    parent = [None] * n          # 每条 prompt 挂在谁下面
    thread_id = [0] * n          # 所属追问线
    steps = []
    next_thread = 0

    for i in range(1, n):
        text = texts[i]
        hits = marker_hits(text)
        mscore = marker_score(hits)
        is_short = len(text.strip()) < SHORT_PROMPT_CHARS

        t_prev = parse_ts(prompts[i - 1]["timestamp"])
        t_cur = parse_ts(prompts[i]["timestamp"])
        gap_min = ((t_cur - t_prev).total_seconds() / 60
                   if (t_prev and t_cur) else None)
        l3 = 0.0
        if gap_min is not None:
            l3 = 1.0 if gap_min > 12 * 60 else (0.5 if gap_min > 60 else 0.0)

        # --- 最近邻链接 ---
        prev_sims = sim[i][:i]
        best_j = argmax(prev_sims)
        best_sim = prev_sims[best_j]

        kind = None
        if is_short:
            # 「好」「来」这类确认没有话题信号, 一律挂在上一条
            parent[i] = i - 1
            thread_id[i] = thread_id[i - 1]
        elif best_sim < theta and not _blocks_new_thread(hits):
            # 语义上接不上, 且没有「展开讲讲」这类明确表示仍在原线的标记。
            next_thread += 1
            parent[i] = None
            thread_id[i] = next_thread
            kind = "新线"
        elif best_j != i - 1 and _is_real_return(best_sim, sim[i][i - 1],
                                                 theta):
            parent[i] = best_j
            thread_id[i] = thread_id[best_j]
            kind = f"回到轮次 {best_j + 1}"
        else:
            parent[i] = i - 1
            thread_id[i] = thread_id[i - 1]

        # L2 覆盖: 框架修正即使发生在同一条线内, 也是要标出来的关键事件
        if not is_short and ("frame_correction" in hits or "negation" in hits):
            kind = "框架修正" if kind is None else f"框架修正 · {kind}"
        elif kind is None and "topic_jump" in hits:
            kind = "话题跳转(标记)"
        elif kind is None and "meta" in hits:
            kind = "层级切换"
        if kind and kind.startswith("新线") and l3 >= 1.0:
            kind = "新线(跨时段)"

        steps.append({
            "i": i, "from": i, "to": i + 1,
            # 展示实际挂载点（短回复强制挂上一条）, 而非原始 argmax
            "link_to": (parent[i] + 1) if parent[i] is not None else None,
            "argmax_to": best_j + 1,
            "link_sim": round(best_sim, 4),
            "sim_prev": round(sim[i][i - 1], 4),
            "l2_marker_score": round(mscore, 2),
            "l2_hits": {k: MARKERS[k]["label"] for k in hits},
            "l3_gap_min": round(gap_min, 1) if gap_min is not None else None,
            "thread": thread_id[i],
            "kind": kind,
            "is_short": is_short,
            "text_head": " ".join(text.split())[:60],
        })

    threads = {}
    for i in range(n):
        threads.setdefault(thread_id[i], []).append(i + 1)

    # 相似度矩阵存成 0-100 的整数, 体积小一个量级, 精度对热力图足够
    sim_int = [[max(0, min(100, int(round(v * 100)))) for v in row]
               for row in sim]

    return {
        "n": n,
        "theta": round(theta, 4),
        "sim_adj": sim_adj,
        "sim_to_first": sim_to_first,
        "sim_matrix": sim_int,
        "steps": steps,
        "thread_id": thread_id,
        "threads": threads,
        "texts_head": [" ".join(t.split())[:44] for t in texts],
        "texts_full": texts,
        "timestamps": [p["timestamp"] for p in prompts],
    }


# ---------------------------------------------------------------- 报告

def report(name: str, meta: dict, res: dict) -> None:
    print("=" * 74)
    print(f"《{name}》")
    print(f"  {meta.get('cwd','')}   真人轮次 {res['n']}")
    print("=" * 74)
    if res["n"] < 2:
        print(res.get("note", ""))
        return

    threads = res["threads"]
    print(f"\n[追问线] 共 {len(threads)} 条   (自适应阈值 θ={res['theta']:.3f})")
    for tid, rounds in sorted(threads.items()):
        head = res["texts_head"][rounds[0] - 1]
        span = f"{rounds[0]}–{rounds[-1]}" if len(rounds) > 1 else str(rounds[0])
        print(f"  线{tid}  轮次 {span}（{len(rounds)} 轮）  起于「{head}」")

    print(f"\n[链接结构]")
    for s in res["steps"]:
        if s["link_to"] is None:
            tgt, arrow = "新", "★"
        elif s["link_to"] == s["from"]:
            tgt, arrow = str(s["link_to"]), "↳"
        else:
            tgt, arrow = str(s["link_to"]), "⤴"
        mark = f"  ← {s['kind']}" if s["kind"] else ""
        short = "  (短回复)" if s["is_short"] else ""
        print(f"  {s['to']:>2} {arrow} {tgt:<3} "
              f"sim={s['link_sim']:.3f} (对上一条 {s['sim_prev']:.3f}){mark}{short}")
        if s["kind"]:
            print(f"       「{s['text_head']}」")

    forks = [s for s in res["steps"] if s["kind"]]
    marker_only = [s for s in forks
                   if s["l2_hits"] and s["link_to"] == s["from"]
                   and s["link_sim"] >= 0.05]
    print(f"\n[分层贡献]")
    print(f"  分叉/关键事件 {len(forks)} 处 / {res['n']-1} 步")
    print(f"  其中「语义上看是延续、只有话语标记暴露」的 {len(marker_only)} 处")
    if marker_only:
        print("  → 这些就是纯 embedding 方法必然漏掉的:")
        for s in marker_only:
            print(f"     轮次{s['to']} [{s['kind']}] 「{s['text_head'][:40]}」")
    print()


# ---------------------------------------------------------------- HTML 输出

def svg_curve(values, width=760, height=120, color="#2f6ae0", baseline=None):
    if not values:
        return ""
    n = len(values)
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1e-9
    pts = []
    for i, v in enumerate(values):
        x = (i / max(1, n - 1)) * (width - 40) + 20
        y = height - 20 - ((v - lo) / span) * (height - 40)
        pts.append(f"{x:.1f},{y:.1f}")
    path = " ".join(pts)
    extra = ""
    if baseline is not None:
        by = height - 20 - ((baseline - lo) / span) * (height - 40)
        extra = (f'<line x1="20" y1="{by:.1f}" x2="{width-20}" y2="{by:.1f}" '
                 f'stroke="#bbb" stroke-dasharray="4 4"/>')
    return (f'<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto">'
            f'{extra}<polyline points="{path}" fill="none" stroke="{color}" '
            f'stroke-width="2"/></svg>')


def build_html(results: list[tuple], out: Path) -> None:
    parts = ["""<!doctype html><meta charset="utf-8">
<title>注意力分叉探针报告</title>
<style>
body{font:15px/1.7 system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#1c2430}
h1{font-size:1.5rem} h2{font-size:1.15rem;margin-top:2.5rem;border-top:1px solid #e6e8ec;padding-top:1.2rem}
.meta{color:#5b6774;font-size:.9rem}
.card{background:#f7f8fa;border-radius:8px;padding:.8rem 1rem;margin:.8rem 0}
.fork{border-left:3px solid #d05; padding-left:.8rem; margin:.6rem 0}
.fork .k{font-weight:600;color:#d05}
.frame{border-left-color:#e07800} .frame .k{color:#e07800}
code{background:#eef1f5;padding:.1rem .35rem;border-radius:4px;font-size:.85em}
.lab{color:#5b6774;font-size:.85rem;margin-top:.6rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{border-bottom:1px solid #e6e8ec;padding:.35rem .5rem;text-align:left}
</style>
<h1>注意力分叉探针报告 <span class="meta">v0.1</span></h1>
<p class="meta">L1 语义距离（字符 n-gram TF-IDF） · L2 话语标记 · L3 时间结构。
每层单独呈现，用于判断纯语义方法会漏掉哪些分叉。</p>"""]

    for name, meta, res in results:
        parts.append(f"<h2>{name}</h2>")
        parts.append(f'<p class="meta">{meta.get("cwd","")} · 真人轮次 {res["n"]}</p>')
        if res["n"] < 2:
            parts.append("<p>轮次过少</p>")
            continue
        sf = res["sim_to_first"]
        sa = res["sim_adj"]
        drop = sf[0] - sf[-1]
        parts.append('<div class="card">')
        parts.append(f'<b>与首问的相似度</b>（下滑 = 漂移）　首 {sf[0]:.2f} → 末 {sf[-1]:.2f}，累计 {drop:.2f}')
        parts.append(svg_curve(sf, color="#d05"))
        parts.append(f'<b>相邻轮次相似度</b>（高且平 = 每步都接得上）　均值 {mean(sa):.2f}')
        parts.append(svg_curve(sa, color="#2f6ae0"))
        parts.append("</div>")

        # 追问线一览
        parts.append(f'<p><b>追问线 {len(res["threads"])} 条</b>'
                     f'<span class="meta">（自适应阈值 θ={res["theta"]:.3f}）</span></p>')
        parts.append("<table><tr><th>线</th><th>轮次跨度</th><th>轮数</th><th>起于</th></tr>")
        for tid, rounds in sorted(res["threads"].items()):
            span = f"{rounds[0]}–{rounds[-1]}" if len(rounds) > 1 else str(rounds[0])
            head = res["texts_head"][rounds[0] - 1]
            parts.append(f"<tr><td>{tid}</td><td>{span}</td><td>{len(rounds)}</td>"
                         f"<td>{head}</td></tr>")
        parts.append("</table>")

        forks = [s for s in res["steps"] if s["kind"]]
        parts.append(f'<p class="lab">关键事件 {len(forks)} 处 / {res["n"]-1} 步</p>')
        for s in forks:
            k = s["kind"] or ""
            cls = "fork frame" if "框架修正" in k else "fork"
            gap = ""
            if s["l3_gap_min"] is not None:
                gap = (f" · {s['l3_gap_min']/1440:.1f}天" if s["l3_gap_min"] > 1440
                       else f" · {s['l3_gap_min']:.0f}分")
            link = (f"→ 挂在轮次 {s['link_to']}" if s["link_to"] else "→ 开新线")
            parts.append(f'<div class="{cls}">')
            parts.append(f'<span class="k">{k}</span> '
                         f'<span class="meta">轮次 {s["to"]} {link} · '
                         f'sim {s["link_sim"]:.3f}（对上一条 {s["sim_prev"]:.3f}）{gap}</span><br>')
            if s["l2_hits"]:
                parts.append(f'<span class="meta">标记：'
                             f'{"、".join(s["l2_hits"].values())}</span><br>')
            parts.append(f'「{s["text_head"]}」</div>')

    out.write_text("\n".join(parts), encoding="utf-8")


def collect_corpus_texts(
    limit_files: int = 200,
    *,
    source: str = "claude",
) -> list[str]:
    """汇总本机所有会话的真人 prompt, 供 IDF 拟合。

    单会话语料太小是 v0.1 的致命伤; 用全量语料才能让相似度有分辨率。
    """
    from conversation_sources import collect_corpus_texts as collect
    return collect(source, limit_sessions=limit_files)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sessions", nargs="+")
    ap.add_argument("--html", default=None)
    ap.add_argument("--no-global-corpus", action="store_true",
                    help="只用给定会话拟合 IDF（会没有分辨率，仅用于复现 v0.1 的失败）")
    args = ap.parse_args()

    paths = [Path(s).expanduser() for s in args.sessions]
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("没有可用的会话文件", file=sys.stderr)
        return 1

    # --- 先建全局语料 ---
    if args.no_global_corpus:
        corpus = []
        for p in paths:
            corpus += [x["text"] for x in extract(p) if not x["suspect_auto"]]
        print(f"[语料] 仅本次会话: {len(corpus)} 条\n")
    else:
        corpus = collect_corpus_texts()
        print(f"[语料] 全机会话汇总: {len(corpus)} 条 prompt 用于拟合 IDF\n")
    if len(corpus) < 5:
        print("语料过小, 结果不可信", file=sys.stderr)
    vec = fit_corpus(corpus)

    results = []
    for p in paths:
        meta = session_meta(p)
        name = meta["custom_title"] or meta["ai_title"] or p.stem[:8]
        allp = extract(p)
        human = [x for x in allp if not x["suspect_auto"]]
        res = analyze(human, vec)
        report(name, meta, res)
        results.append((name, meta, res))

    if args.html and results:
        out = Path(args.html)
        from render import build_page
        build_page(results, out)
        print(f"HTML 报告: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

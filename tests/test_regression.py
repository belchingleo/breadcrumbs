"""正确性回归测试：已修复的 bug 不许复发。

与「语义评测集」的区别（这个区分很重要）
----------------------------------------
语义评测集断言「什么算一条新线」「阈值多少最好」——它会把当前启发式的
偏好固化成基准。本项目的启发式在开发中已被真实数据推翻三次, 现在建基准
等于给尚未稳定的判断上锁, 因此缓做。

本文件只断言**正确性**: 错位必须被拒绝、注入必须被转义、静默丢弃不许发生。
这些断言与「怎么划线才对」无关, 不会妨碍算法继续演进。

运行: python3 tests/test_regression.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import identity                                    # noqa: E402
from extract_prompts import extract, is_auto       # noqa: E402
from codex_source import clean_user_text, normalize_thread  # noqa: E402
from conversation_sources import (finalize_prompts, stream_for_prompts)  # noqa: E402
from textsim import TfidfModel                     # noqa: E402
from validate_annotations import validate          # noqa: E402
from render import build_page, discussion_path, report_identity  # noqa: E402
from report import attach_annotations                   # noqa: E402
from analyze import build_analysis                 # noqa: E402
from install import install_to                     # noqa: E402
from realign import realign                        # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ✓ {name}")
    else:
        FAILURES.append(f"{name}{(' — ' + detail) if detail else ''}")
        print(f"  ✗ {name} {detail}")


# --------------------------------------------------------------- 夹具

def fake_analysis(n_threads: int = 8) -> dict:
    threads = []
    for i in range(n_threads):
        turns = [i * 3 + 1, i * 3 + 2]
        text = f"这是第{i}条线的起始提问，内容各不相同以便区分"
        threads.append({
            "id": i,
            "is_trunk": i == 0,
            "thread_signature": identity.thread_signature(turns, text),
            "anchor_quote": identity.anchor_quote(text),
            "first_turn": turns[0], "last_turn": turns[-1],
            "turn_count": len(turns),
            "turns": [{"turn": turn} for turn in turns],
        })
    return {
        "analysis_id": "sha256:testanalysis0000",
        "algorithm_version": identity.ALGORITHM_VERSION,
        "threads": threads,
    }


def full_annotation(tid: int, sig: str, anchor: str) -> dict:
    return {
        "id": tid, "thread_signature": sig, "anchor_quote": anchor,
        "name": f"线{tid}的名字", "topic": "主题", "yield": "产出",
        "outcome": "conclusion", "resolved": True,
        "spawned_by": "user", "relation_to_trunk": "supplied",
    }


def good_annotations(a: dict) -> dict:
    return {
        "analysis_id": a["analysis_id"],
        "report": {
            "title": "如何辨认讨论中真正的问题变化",
            "subtitle": "从跟踪表面话题，转向核查目标与判断发生了什么变化",
        },
        "threads": [full_annotation(t["id"], t["thread_signature"],
                                    t["anchor_quote"]) for t in a["threads"]],
    }


# --------------------------------------------------------------- 测试

def t_thread_count_mismatch() -> None:
    """P0-1: 8 条分析 + 9 条标注必须拒绝渲染。"""
    a = fake_analysis(8)
    ann = good_annotations(a)
    ann["threads"].append(full_annotation(8, "sha256:bogus", "不存在的线"))
    errs = validate(a, ann)
    check("8分析+9标注 被拒绝", bool(errs))
    check("错误信息点名多出的线", any("[8]" in e for e in errs),
          str(errs[:1]))


def t_signature_change() -> None:
    """签名失配要分两种情况处理, 不能一律判死。

    起点也变了 = 这条标注对的是另一条线 -> 硬拒绝。
    起点没变、只是轮次构成变了 = 算法重划边界 -> 语义判断多半仍成立,
    降级为待复核警告, 否则每次调参都要整批重标。
    """
    # ① 起点也变了: 必须拒绝
    a = fake_analysis(4)
    ann = good_annotations(a)
    a["threads"][3]["thread_signature"] = identity.thread_signature(
        [99, 100, 101], "完全不同的起始文本")
    a["threads"][3]["anchor_quote"] = "完全不同的起始文本"
    a["threads"][3]["content_signature"] = identity.content_signature(
        "完全不同的起始文本")
    errs = validate(a, ann)
    check("起点也变 被拒绝", bool(errs))
    check("拒绝信息含双方摘要",
          any("分析起于" in e or "另一条线" in e for e in errs), str(errs[:1]))

    # ② 只有轮次构成变了: 放行但警告
    b = fake_analysis(4)
    ann_b = good_annotations(b)
    b["threads"][2]["thread_signature"] = identity.thread_signature(
        [50, 51, 52], b["threads"][2]["anchor_quote"])
    warns: list[str] = []
    errs_b = validate(b, ann_b, warns)
    check("起点未变 不拒绝", not errs_b, str(errs_b[:2]))
    check("起点未变 给出待复核警告",
          any("轮次构成变了" in w for w in warns), str(warns[:2]))


def t_analysis_id_mismatch() -> None:
    """P0-1: 标注针对另一份分析时必须拒绝。"""
    a = fake_analysis(3)
    ann = good_annotations(a)
    ann["analysis_id"] = "sha256:someotheranalysis"
    errs = validate(a, ann)
    check("analysis_id 不符 被拒绝",
          any("analysis_id" in e for e in errs))


def t_missing_relation() -> None:
    """支线必须填 relation_to_trunk。"""
    a = fake_analysis(3)
    ann = good_annotations(a)
    ann["threads"][2]["relation_to_trunk"] = None
    errs = validate(a, ann)
    check("支线漏填 relation_to_trunk 被拒绝",
          any("relation_to_trunk" in e for e in errs))


def t_valid_passes() -> None:
    """正确的标注必须通过——否则校验器本身没用。"""
    a = fake_analysis(5)
    errs = validate(a, good_annotations(a))
    check("完全匹配的标注 通过", not errs, str(errs[:2]))


def t_report_title_validation_is_backward_compatible() -> None:
    """旧标注可降级；新标题结构必须完整，并劝阻“从 A 到 B”修辞。"""
    a = fake_analysis(2)
    legacy = good_annotations(a)
    legacy.pop("report")
    warns: list[str] = []
    check("旧标注没有 report 仍可通过", not validate(a, legacy, warns))
    check("旧标注会得到标题降级提示",
          any("report.title" in warning for warning in warns), str(warns))

    broken = good_annotations(a)
    broken["report"] = {"title": ""}
    check("声明 report 却没有 title 会被拒绝",
          any("report.title" in error for error in validate(a, broken)))

    rhetorical = good_annotations(a)
    rhetorical["report"]["title"] = "从注意力漂移到目标分叉账本"
    warns = []
    validate(a, rhetorical, warns)
    check("从 A 到 B 标题会得到结构提示",
          any("从 A 到 B" in warning for warning in warns), str(warns))


def t_evidence_turn_must_belong_to_thread() -> None:
    """原话指针不能跨到另一条思路，否则证据与判断会再次错位。"""
    a = fake_analysis(3)
    ann = good_annotations(a)
    ann["threads"][1]["evidence_turn"] = 999
    errs = validate(a, ann)
    check("跨线 evidence_turn 被拒绝",
          any("evidence_turn" in error for error in errs), str(errs[:2]))


def t_sim_matrix_is_opt_in() -> None:
    """n×n 矩阵无消费者, 默认不该落盘（83 轮就要 21K）。"""
    import inspect
    from analyze import build_analysis as ba
    params = inspect.signature(ba).parameters
    check("build_analysis 有 keep_sim_matrix 开关", "keep_sim_matrix" in params)
    check("默认关闭", params["keep_sim_matrix"].default is False)


def t_realign_rescues_annotations() -> None:
    """线重划后, 起点未变的标注必须能救回, 且留下待复核痕迹。"""
    a = fake_analysis(3)
    for t in a["threads"]:
        t["content_signature"] = identity.content_signature(t["anchor_quote"])
    old = good_annotations(a)

    # 模拟重新分析: 线1 轮次构成变了, 起点不变
    a2 = json.loads(json.dumps(a))
    a2["analysis_id"] = "sha256:newanalysis0001"
    a2["threads"][1]["thread_signature"] = identity.thread_signature(
        [4, 5, 6, 7], a2["threads"][1]["anchor_quote"])
    a2["threads"][1]["turn_count"] = 4

    new, notes = realign(a2, old)
    check("全部救回", len(new["threads"]) == 3, f"实际 {len(new['threads'])}")
    check("analysis_id 换成新的",
          new["analysis_id"] == "sha256:newanalysis0001")
    check("语义判断被沿用",
          new["threads"][1].get("name") == old["threads"][1]["name"])
    check("轮次变动留下待复核痕迹",
          "待复核" in (new["threads"][1].get("agent_note") or ""),
          str(new["threads"][1].get("agent_note")))
    check("对齐后可通过校验", not validate(a2, new), str(validate(a2, new)[:2]))

    # 起点也变了的, 不许被救
    a3 = json.loads(json.dumps(a))
    a3["threads"][2]["anchor_quote"] = "一段毫不相干的全新起始提问内容"
    a3["threads"][2]["content_signature"] = identity.content_signature(
        a3["threads"][2]["anchor_quote"])
    new3, _ = realign(a3, old)
    check("起点也变的线不被误配",
          all(t["id"] != 2 for t in new3["threads"]),
          str([t["id"] for t in new3["threads"]]))


def t_script_injection() -> None:
    """P0-5: prompt 含 </script> 不得破坏页面。"""
    n = 3
    res = {
        "n": n, "theta": 0.03,
        "threads": {0: [1, 2, 3]}, "thread_id": [0] * n,
        "steps": [{"to": i + 1, "from": i, "link_to": i, "link_sim": 0.5,
                   "sim_prev": 0.5, "kind": None, "is_short": False,
                   "l2_hits": {}, "l3_gap_min": 1.0, "thread": 0,
                   "text_head": "x", "argmax_to": i} for i in range(1, n)],
        "sim_matrix": [[100] * n for _ in range(n)],
        "texts_head": ["a", "b", "c"],
        "texts_full": ["</script><script>window.__PWNED__=1</script>",
                       "正常内容二", "正常内容三"],
        "timestamps": ["2026-01-01T00:00:00Z"] * n,
        "sim_adj": [0.5] * (n - 1), "sim_to_first": [1.0] * n,
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "x.html"
        build_page([("注入测试", {"cwd": "/x"}, res)], out)
        html = out.read_text(encoding="utf-8")
    check("无裸 </script><script>",
          "</script><script>window.__PWNED__" not in html)
    check("已转义为 \\u003c", "u003c" in html)
    check("script 标签仅剩预期的 2 个", html.count("<script") == 2,
          f"实际 {html.count('<script')}")


def t_suspect_auto_not_silent() -> None:
    """P1-1: 自动条目只能标记, 不能在抽取层被静默删除。"""
    lines = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "这是我真正输入的一段较长提问"}},
        {"type": "user", "timestamp": "2026-01-01T00:01:00Z",
         "message": {"role": "user", "content": "[Request interrupted by user]"}},
    ]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.jsonl"
        f.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                               for x in lines), encoding="utf-8")
        got = extract(f)
    check("两条都被保留（未静默删除）", len(got) == 2, f"实际 {len(got)}")
    check("中断标记被标为 suspect_auto",
          got[1]["suspect_auto"] is True)
    check("真实输入未被误标", got[0]["suspect_auto"] is False)


def t_interrupt_is_auto() -> None:
    """P0-1 起因: 中断标记曾凭空造出一条线。"""
    for s in ("[Request interrupted by user]", "[Request cancelled]",
              "Continue from where you left off."):
        check(f"识别为自动: {s[:22]}", is_auto(s))
    check("正常中文提问不被误判",
          not is_auto("我想追问一下这两个概念是不是同一回事"))


def t_empty_corpus_is_degenerate() -> None:
    """P0-3: 空语料会让相似度全为 0——必须能被上层察觉。"""
    m = TfidfModel().fit([])
    v = m.transform("一段关于地方与空间关系的讨论")
    check("空语料 -> 零维向量（可被检测）", len(v) == 0)
    m2 = TfidfModel().fit(["文本甲的内容", "文本乙的内容", "文本甲的内容"])
    check("正常语料 -> 非零词表", len(m2.idf) > 0)


def t_determinism() -> None:
    """相同输入必须得到相同结果, 否则报告不可复现。"""
    docs = [f"第{i}段文本，内容各异用于拟合" for i in range(30)]
    a = TfidfModel().fit(sorted(docs)).transform("第5段文本，内容各异用于拟合")
    b = TfidfModel().fit(sorted(docs)).transform("第5段文本，内容各异用于拟合")
    check("TF-IDF 可复现", a == b)
    sig1 = identity.thread_signature([1, 2, 3], "起始文本")
    sig2 = identity.thread_signature([1, 2, 3], "起始文本")
    check("签名可复现", sig1 == sig2)
    check("轮次不同则签名不同",
          identity.thread_signature([1, 2, 4], "起始文本") != sig1)


def t_cross_session_identity() -> None:
    """跨会话匹配的前提: 存在不含会话相对轮次的内容签名。"""
    text = "这批样本是否只说明了相关而非因果"
    c1 = identity.content_signature(text)
    c2 = identity.content_signature(text)
    check("content_signature 可复现", c1 == c2)
    check("content_signature 不随轮次变化",
          identity.content_signature(text) == c1)
    check("不同内容 -> 不同签名",
          identity.content_signature("完全不同的另一个问题") != c1)


def t_prompt_anchor_is_stable() -> None:
    """用户原话锚点不依赖线路编号，并能区分同文重发。"""
    a = identity.prompt_anchor("我想继续讨论抽样口径", "2026-01-01", "msg-a")
    b = identity.prompt_anchor("我想继续讨论抽样口径", "2026-01-01", "msg-a")
    c = identity.prompt_anchor("我想继续讨论抽样口径", "2026-01-02", "msg-b")
    check("prompt 锚点可复现", a == b)
    check("同文重发仍有不同锚点", a != c)
    check("prompt 锚点可直接用作 HTML id", a.startswith("prompt-"))


def t_degraded_metadata_is_honest() -> None:
    """降级原因必须报告历史语料规模，不能报告回填后的拟合规模。"""
    entries = [
        {"type": "user", "timestamp": "2026-01-01T00:00:00Z",
         "uuid": "a", "message": {"role": "user", "content": "先讨论主问题是什么"}},
        {"type": "user", "timestamp": "2026-01-01T00:01:00Z",
         "uuid": "b", "message": {"role": "user", "content": "再看一个不同的案例"}},
    ]
    fake = {
        "n": 2, "theta": 0.03,
        "threads": {0: [1], 1: [2]}, "thread_id": [0, 1],
        "steps": [{"to": 2, "from": 1, "link_to": 1, "link_sim": 0.1,
                   "sim_prev": 0.1, "kind": "新线", "is_short": False,
                   "l2_hits": {}, "l3_gap_min": 1.0, "thread": 1,
                   "text_head": "再看一个不同的案例", "argmax_to": 1}],
        "sim_matrix": [[100, 10], [10, 100]],
        "texts_head": ["先讨论主问题是什么", "再看一个不同的案例"],
        "texts_full": ["先讨论主问题是什么", "再看一个不同的案例"],
        "timestamps": ["2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z"],
        "sim_adj": [0.1], "sim_to_first": [1.0, 0.1],
    }
    with tempfile.TemporaryDirectory() as d:
        source = Path(d) / "session.jsonl"
        source.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                    for x in entries), encoding="utf-8")
        with patch("analyze.collect_corpus_texts", return_value=[]), \
             patch("analyze.fit_corpus", return_value=object()), \
             patch("analyze.run_analyze", return_value=fake):
            analysis = build_analysis(source)
    corpus = analysis["corpus"]
    check("历史语料规模单独记录", corpus["history_size"] == 0)
    check("拟合语料包含本会话", corpus["fit_size"] == 2)
    check("降级原因没有把回填规模写成历史规模",
          "仅 0 条" in corpus["degrade_reason"], corpus["degrade_reason"])
    check("每条 prompt 都有稳定锚点",
          all(t["prompt_anchor"].startswith("prompt-")
              for thread in analysis["threads"] for t in thread["turns"]))


def t_product_language_and_layers() -> None:
    """主结果页必须是复盘稿，而不是旧的分析仪表盘。"""
    n = 4
    res = {
        "n": n, "theta": 0.03,
        "threads": {0: [1, 2, 4], 1: [3]},
        "thread_id": [0, 0, 1, 0],
        "steps": [
            {"to": 2, "from": 1, "link_to": 1, "link_sim": 0.8,
             "sim_prev": 0.8, "kind": None, "is_short": False,
             "l2_hits": {}, "l3_gap_min": 1.0, "thread": 0,
             "text_head": "继续主问题", "argmax_to": 1},
            {"to": 3, "from": 2, "link_to": 2, "link_sim": 0.1,
             "sim_prev": 0.1, "kind": "新线", "is_short": False,
             "l2_hits": {}, "l3_gap_min": 1.0, "thread": 1,
             "text_head": "换个案例", "argmax_to": 2},
            {"to": 4, "from": 3, "link_to": 3, "link_sim": 0.8,
             "sim_prev": 0.8, "kind": "回访", "is_short": False,
             "l2_hits": {}, "l3_gap_min": 1.0, "thread": 0,
             "text_head": "回到主问题", "argmax_to": 2},
        ],
        "sim_matrix": [[100] * n for _ in range(n)],
        "texts_head": ["主问题是什么", "继续主问题", "换个案例", "回到主问题"],
        "texts_full": ["主问题是什么", "继续主问题", "换个案例", "回到主问题"],
        "timestamps": ["2026-01-01T00:00:00Z"] * n,
        "prompt_anchors": [f"prompt-stable-{i}" for i in range(n)],
        "sim_adj": [0.8, 0.1, 0.8],
        "sim_to_first": [1.0, 0.8, 0.1, 0.8],
        "trust": {"annotations": "inferred", "forced": False},
        "analysis_meta": {"corpus": {"degraded": False}},
        "annotations": {
            0: {
                "name": "确认主问题",
                "resolved": True,
                "yield": "确认了讨论对象",
            },
            1: {
                "name": "<换一个案例核查>",
                "resolved": False,
                "yield": "找到了一个反例，但还没有回到主问题",
                "relation_to_trunk": "blocked",
                "relation_note": "主问题的结论仍在等待这个案例被解释",
                "spawned_by": "user",
                "resolution_evidence": "后续回复没有完成核查",
            },
        },
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "review.html"
        build_page([("复盘测试", {
            "cwd": "/x",
            "first_time": "2026-01-01T00:00:00+00:00",
            "last_time": "2026-01-01T08:48:00+00:00",
        }, res)], out)
        page = out.read_text(encoding="utf-8")
    for phrase in ("一、来路图", "你是怎么走到终点的",
                   "进入具体思路时，对应节点会点亮",
                   "二、复盘总览", "1. 先认出来路，再决定从哪里继续",
                   "2. 这次改变了什么", "3. 建议优先继续",
                   "三、思路复盘", "完整讨论结构", "逐条原话",
                   "方法与诊断"):
        check(f"页面包含「{phrase}」", phrase in page)
    check("图上直接说明三种交互",
          all(text in page for text in (
              "点名称定位",
              "点「原话」核对",
              "点「返回」继续",
          )))
    check("全宽来路图和侧栏小地图同时存在",
          'class="route-overview"' in page and
          'class="route-minimap"' in page)
    check("完整讨论树包含逐条 prompt 节点",
          page.count('class="detail-node"') == n,
          f"实际 {page.count('detail-node')}")
    check("仍开放思路生成返回提示词",
          "复制返回提示词" in page and
          "我想回到这段对话里「换一个案例核查」这条思路" in page)
    check("开放思路可直接从来路图返回",
          'class="route-return"' in page and
          'data-copy-prompt="map-resume-' in page)
    check("品牌署名统一为 Breadcrumbs · 思路",
          "Breadcrumbs · 思路" in page and
          "Breadcrumbs · 沿原话返回" not in page)
    check("系统生成名称使用中文直角引号",
          "「换一个案例核查」" in page and
          "&lt;换一个案例核查&gt;" not in page)
    check("来路模块不重复显示当前章节",
          "data-current-route" not in page and "正在阅读" not in page)
    check("固定模块使用思路的面包屑命名与图标",
          "<h2>思路的面包屑</h2>" in page and
          'class="crumb-mark"' in page)
    check("复盘总览与来路图同属定向区",
          'class="orientation"' in page and
          page.index('class="review-summary"') < page.index('class="review-layout"'))
    check("变化与继续建议直接进入复盘总览",
          'class="summary-outcomes"' in page and
          'class="summary-nav"' not in page and
          'class="change-section"' not in page and
          'class="continue-section"' not in page)
    check("三项来路摘要紧跟在总览副标题后",
          page.index('class="summary-ledger"') <
          page.index('class="summary-outcomes"'))
    check("最初在问使用真实首问而非结构主线",
          "<dt>最初在问</dt><dd>主问题是什么</dd>" in page)
    check("最后停在排在未尽事宜之前",
          page.index("<dt>最后停在</dt>") <
          page.index("<dt>未尽事宜</dt>"))
    check("最后停在描述用户最后行为而不是所属思路",
          "<dt>最后停在</dt><dd>回到主问题</dd>" in page)
    check("单场历时进入开头卡片",
          '<p class="masthead-duration"><span>历时</span>8.8 小时</p>' in page)
    check("分叉在右侧思路状态旁显示非价值判断的漂移程度",
          "注意力漂移 " in page and "不评价这次漂移好坏" in page and
          page.index("注意力漂移 ") > page.index("三、思路复盘"))
    check("漂移程度与标题同组，不跟在收束状态后",
          'class="thought-title-main"' in page and
          page.index("注意力漂移 ") < page.index(">未收束</span>"))
    check("左侧思路树不承载漂移标签",
          "注意力漂移" not in page[
              page.index('class="route-minimap"'):
              page.index("</aside>")
          ])
    check("开放状态统一改为未收束", ">未收束</span>" in page)
    check("思路复盘不再重复字段说明",
          "同一套字段讲清楚" not in page)
    check("三级复盘标题按顺序编号",
          page.index("一、来路图") <
          page.index("二、复盘总览") <
          page.index("三、思路复盘"))
    check("工作目录不进入第一视觉层", "/x" not in page)
    check("完整结构是正文一级章节",
          'class="review-section structure-section"' in page and
          "<h2>完整讨论结构</h2>" in page)
    check("旧仪表盘标题已移除", "注意力分叉可视化" not in page)
    check("稳定 prompt 锚点进入页面", "prompt-stable-0" in page)
    check("未核实状态使用虚线语言", "虚线表示仅按用户提问的结构推断" in page)

    # 主问题本身尚未收束时，也必须进入开放清单与继续建议；不能因为它不是
    # “支线”就错误显示“没有明显悬空”。
    res["annotations"][0].update({
        "resolved": False,
        "yield": "主问题已有中间结果，但仍在推进",
        "resolution_evidence": "末次回复仍在执行主问题，没有得到用户确认",
    })
    res["annotations"][1]["resolved"] = True
    with tempfile.TemporaryDirectory() as d:
        main_open_out = Path(d) / "main-open.html"
        build_page([("主问题未收束", {"cwd": "/x"}, res)], main_open_out)
        main_open_page = main_open_out.read_text(encoding="utf-8")
    check("未收束主问题进入开放清单",
          "确认主问题" in main_open_page and
          "没有明显悬空" not in main_open_page)
    check("未收束主问题进入继续建议",
          "当前主问题仍在推进" in main_open_page and
          "我想回到这段对话里「确认主问题」这条思路" in main_open_page)


def t_report_title_has_its_own_semantics() -> None:
    """报告标题必须与宿主任务名分离，并同时进入页面 H1 与浏览器页签。"""
    n = 2
    res = {
        "n": n,
        "threads": {0: [1, 2]},
        "thread_id": [0, 0],
        "steps": [],
        "texts_head": ["先看真正的问题", "继续核查"],
        "texts_full": ["先看真正的问题", "继续核查"],
        "timestamps": ["2026-01-01"] * n,
        "report_meta": {
            "title": "如何记录 AI 长对话中的目标分叉",
            "subtitle": "从检测注意力漂移，转向记录目标、决定与撤销",
        },
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "title.html"
        build_page([("评估项目可行性与改进方向", {"cwd": "/x"}, res)], out)
        page = out.read_text(encoding="utf-8")
    check("语义标题进入页面 H1",
          "<h1>如何记录 AI 长对话中的目标分叉</h1>" in page)
    check("语义标题进入浏览器页签",
          "<title>如何记录 AI 长对话中的目标分叉｜Breadcrumbs</title>" in page)
    check("单会话首屏不再重复宿主标题",
          "评估项目可行性与改进方向 · 对话复盘" not in page)
    check("副标题解释变化而不抢占标题",
          "从检测注意力漂移，转向记录目标、决定与撤销" in page)


def t_report_title_survives_annotation_handoff() -> None:
    """annotations.json 的报告级标题不能在进入渲染器前再次被丢掉。"""
    res: dict = {}
    ann = {
        "report": {
            "title": "如何记录 AI 长对话中的目标分叉",
            "subtitle": "从检测漂移转向记录目标变化",
        },
        "threads": [{"id": 0, "name": "目标分叉"}],
    }
    attach_annotations(res, ann)
    check("报告标题通过 report.py 进入渲染契约",
          res["report_meta"]["title"] == ann["report"]["title"])
    check("线路标注仍按 id 建索引",
          res["annotations"][0]["name"] == "目标分叉")


def t_report_title_falls_back_to_confirmed_trunk() -> None:
    """旧数据优先使用已核实的主问题，而不是照抄宿主会话名。"""
    res = {
        "annotations": {0: {"name": "案例是否真的支撑核心概念"}},
        "analysis_threads": {0: {"is_trunk": True}},
    }
    identity_meta = report_identity("一个含糊的宿主标题", res)
    check("降级标题来自已核实主问题",
          identity_meta["title"] == "案例是否真的支撑核心概念",
          identity_meta["title"])


def t_false_merge_not_painted_as_confirmed_path() -> None:
    """证据在后半段时，不能把误合并的起点套上后来的语义标题。"""
    res = {
        "n": 4,
        "threads": {0: [1, 2, 4], 1: [3]},
        "thread_id": [0, 0, 1, 0],
        "steps": [],
        "texts_head": ["无关项目实现", "继续项目", "讨论产品", "修结果页"],
        "texts_full": ["无关项目实现", "继续项目", "讨论产品", "修结果页"],
        "timestamps": ["2026-01-01"] * 4,
    }
    ann = {
        0: {"name": "把产品定义落成结果页", "evidence_turn": 4},
        1: {"name": "讨论产品边界", "evidence_turn": 3},
    }
    path = discussion_path(res, ann)
    check("误合并起点保留用户原话", path[0]["label"] == "无关项目实现")
    check("误合并起点不画成已核实", path[0]["confirmed"] is False)
    check("到达证据后才使用语义名",
          path[-1]["label"] == "把产品定义落成结果页" and
          path[-1]["confirmed"] is True)


def t_install_keeps_recoverable_backup() -> None:
    """更新安装不原地覆盖，旧版必须能恢复。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "skills"
        first, backup1 = install_to(root)
        marker = first / "old-version-marker"
        marker.write_text("old", encoding="utf-8")
        second, backup2 = install_to(root)
        check("首次安装没有伪造备份", backup1 is None)
        check("更新后的目标仍完整", (second / "SKILL.md").exists())
        check("上一版被保留", backup2 is not None and
              (backup2 / "old-version-marker").read_text() == "old")
        check("QA 文件不进入安装包", not (second / "qa").exists())

        # 备份必须落在 skills 目录之外。
        # 实测教训: 备份原先叫 ~/.claude/skills/breadcrumbs.backup-<戳>,
        # 被 Claude Code 当成一个活的 skill 加载, 且其 frontmatter 里
        # name 与正式版相同 —— 两个同名 skill 争抢触发。
        # 原有断言只检查「备份存在」, 因此漏掉了这个 bug。
        check("备份不在 skills 目录内",
              backup2 is not None and root not in backup2.parents,
              f"备份落在 {backup2}")
        entries = sorted(p.name for p in root.iterdir())
        check("skills 目录下只有 breadcrumbs 一项", entries == ["breadcrumbs"],
              f"实际 {entries}")
        # 任何带 SKILL.md 的目录都会被当 skill 扫描, 这是判据本身
        loadable = [p.name for p in root.iterdir()
                    if (p / "SKILL.md").exists()]
        check("skills 目录下只有一个可加载 skill", loadable == ["breadcrumbs"],
              f"实际 {loadable}")


def t_codex_thread_normalization() -> None:
    """Codex turns 必须映射成与 Claude 相同的 prompt/stream 契约。"""
    thread = {
        "id": "task-1",
        "turns": [
            {
                "id": "turn-1", "startedAt": 1767225600,
                "items": [
                    {
                        "type": "userMessage", "id": "user-1",
                        "content": [{
                            "type": "text",
                            "text": (
                                "# Files mentioned by the user:\n\n"
                                "## A: /tmp/a\n\n"
                                "## My request for Codex:\n"
                                "先讨论真正的问题是什么"
                            ),
                        }],
                    },
                    {"type": "reasoning", "id": "reason-1", "content": "不可见"},
                    {"type": "agentMessage", "id": "agent-1",
                     "text": "这是第一轮回复"},
                ],
            },
            {
                "id": "turn-2", "startedAt": 1767225660,
                "items": [
                    {
                        "type": "userMessage", "id": "user-2",
                        "content": [{"type": "text", "text": "再看另一个角度"}],
                    },
                    {"type": "agentMessage", "id": "agent-2",
                     "text": "这是第二轮回复"},
                ],
            },
        ],
    }
    raw, stream = normalize_thread(thread)
    prompts = finalize_prompts(raw)
    check("Codex 两条用户输入均被抽取", len(prompts) == 2)
    check("附件外壳不污染原话锚点",
          prompts[0]["text"] == "先讨论真正的问题是什么",
          prompts[0]["text"])
    check("Codex source_id 使用 item id",
          prompts[0]["source_id"] == "user-1")
    check("reasoning 不进入按需回读消息流",
          all(text != "不可见" for _, text, _ in stream))
    check("AI 回复进入按需回读消息流",
          any(text == "这是第二轮回复" for _, text, _ in stream))


def t_codex_retry_stream_alignment() -> None:
    """prompt 折叠重发后，reply 的轮号必须同步折叠。"""
    raw = [
        {"line": 1, "source_id": "u1", "timestamp": "2026-01-01",
         "text": "这是一次足够长、稍后会原样重发的请求"},
        {"line": 2, "source_id": "u2", "timestamp": "2026-01-01",
         "text": "这是一次足够长、稍后会原样重发的请求"},
        {"line": 3, "source_id": "u3", "timestamp": "2026-01-01",
         "text": "现在进入另一个新的问题"},
    ]
    prompts = finalize_prompts(raw)
    stream = [
        ("human", raw[0]["text"], "u1"),
        ("assistant", "失败的半截回复", "a1"),
        ("human", raw[1]["text"], "u2"),
        ("assistant", "真正采用的回复", "a2"),
        ("human", raw[2]["text"], "u3"),
        ("assistant", "下一轮回复", "a3"),
    ]
    aligned = stream_for_prompts(stream, prompts)
    humans = [item for item in aligned if item[0] == "human"]
    check("重发折叠后消息流也只有两轮", len(humans) == 2)
    check("被替代的半截回复不再占用轮号",
          all(text != "失败的半截回复" for _, text, _ in aligned))
    check("最后一次重发及其回复被保留",
          any(text == "真正采用的回复" for _, text, _ in aligned))


def t_agent_view_is_slim_but_analysis_is_not() -> None:
    """agent view 瘦身不得波及完整 analysis.json。

    完整分析是 validate_annotations 校验 evidence_turn 归属、以及 render 出图的
    唯一依据；瘦的那份只喂给 agent。两者混淆过一次就会重演「标注错位照常出图」。
    """
    import analyze

    turns = [
        {"turn": i, "time": "2026-01-01 00:00:00", "chars": 10,
         "text": f"第{i}轮" + "字" * i, "truncated": False,
         "event": ("新线" if i == 5 else None),
         "links_to": None, "markers": []}
        for i in range(1, 21)
    ]
    turns[2]["text"] = "平台外壳" * 1000
    analysis = {
        "analysis_id": "sha256:x",
        "threads": [{"id": 0, "first_turn": 1, "last_turn": 20,
                     "turns": turns}],
        "_render": {"big": "x" * 5000},
    }
    view = analyze.agent_view(analysis)
    kept = view["threads"][0]["turns"]

    check("agent view 剥掉 _render", "_render" not in view)
    check("agent view 去掉渲染专用字段",
          all(k not in kept[0] for k in ("time", "chars", "truncated")),
          f"实际键 {list(kept[0])}")
    check("agent view 省略空值键",
          "links_to" not in kept[0] and "markers" not in kept[0])
    # 只写 len(kept) <= KEEP_TURNS 是恒真的: 把常量调大, 断言照样通过。
    # 必须同时钉住「确实折叠了」和一个与常量无关的绝对上限。
    check("中段确实被折叠", len(kept) < len(turns),
          f"20 轮里保留了 {len(kept)} 轮，等于没折叠")
    check("长线不会绕过折叠", len(kept) <= 12,
          f"保留 {len(kept)} 轮，长会话仍会灌爆上下文")
    check("折叠遵守设定的上限", len(kept) <= analyze.KEEP_TURNS,
          f"保留 {len(kept)} > 上限 {analyze.KEEP_TURNS}")
    check("首尾轮次永远保留",
          kept[0]["turn"] == 1 and kept[-1]["turn"] == 20)
    check("带事件的轮次优先保留",
          any(t["turn"] == 5 for t in kept))
    check("不再把最长文本直接当作最重要",
          all(t["turn"] != 3 for t in kept),
          f"实际保留 {[t['turn'] for t in kept]}")
    check("折叠数量如实告知 agent",
          view["threads"][0]["turns_elided"] == len(turns) - len(kept))
    check("完整 analysis 未被就地修改",
          "time" in analysis["threads"][0]["turns"][0] and
          len(analysis["threads"][0]["turns"]) == 20 and
          "_render" in analysis)


def t_sweep_plan_covers_every_thread_in_one_call() -> None:
    """整场回读必须一次调用取回。

    逐条回读会让每条线产生两次工具往返，而每次往返都要重新计费已累积的上下文。
    真正的 token 成本在往返次数，这条测的就是那个次数。
    """
    import fetch_reply

    view = {"threads": [
        {"id": 0, "first_turn": 1, "last_turn": 9},
        {"id": 1, "first_turn": 3, "last_turn": 7},
        {"id": 2, "first_turn": 5, "last_turn": 5},
    ]}
    plan = fetch_reply.sweep_plan(view)

    check("每条线两处位置", len(plan) == 6, f"实际 {len(plan)}")
    check("起点取之前的回复（判断由谁引出）",
          all(before for _, _, before, _ in plan[::2]))
    check("末次取之后的回复（判断是否收束）",
          all(not before for _, _, before, _ in plan[1::2]))
    check("覆盖到每一条线",
          {tid for tid, _, _, _ in plan} == {0, 1, 2})


def t_reply_budget_preserves_resolution_evidence() -> None:
    import fetch_reply

    text = "开头判断。" + "中" * 3000 + "最终完成：已经修复并验证。"
    clipped = fetch_reply.clip_reply(text, 300)
    check("超长回复仍保留开头", clipped.startswith("开头判断"))
    check("超长回复仍保留末尾结论", clipped.endswith("最终完成：已经修复并验证。"))
    plan = [
        (0, 3, True, "起点前"),
        (1, 3, True, "起点前"),
        (0, 8, False, "末次后"),
    ]
    compact = fetch_reply.compact_sweep_plan(plan)
    check("相同回复位置只输出一次", len(compact) == 2, f"实际 {compact}")
    check("合并后仍保留全部思路引用", compact[0][0] == [0, 1])


def t_codex_ambient_context_is_not_a_prompt() -> None:
    raw = """
<in-app-browser-context source="ambient-ui-state">
Current URL: http://127.0.0.1:8765/example.html
</in-app-browser-context>
<environment_context>
  <cwd>/tmp/project</cwd>
</environment_context>

真正的用户请求
"""
    check("浏览器与环境外壳被移除",
          clean_user_text(raw) == "真正的用户请求",
          clean_user_text(raw))
    check("无附件时请求标题也被移除",
          clean_user_text(
              "## My request for Codex:\n真正的用户请求"
          ) == "真正的用户请求")


def t_engineering_ids_stay_out_of_the_body() -> None:
    """agent_note 里的记录号不得出现在正文一级。

    实测标注里 11 条有 5 条 agent_note 写着「轮 3」「11 轮」，
    而它当时被渲染成思路卡片里的「核对备注」——用户可见层。
    """
    n = 2
    res = {
        "n": n, "theta": 0.03,
        "threads": {0: [1, 2]}, "thread_id": [0, 0],
        "steps": [{"to": 2, "from": 1, "link_to": 1, "link_sim": 0.8,
                   "sim_prev": 0.8, "kind": None, "is_short": False,
                   "l2_hits": {}, "l3_gap_min": 1.0, "thread": 0,
                   "text_head": "继续", "argmax_to": 1}],
        "sim_matrix": [[100] * n for _ in range(n)],
        "texts_head": ["主问题是什么", "继续主问题"],
        "texts_full": ["主问题是什么", "继续主问题"],
        "timestamps": ["2026-01-01T00:00:00Z"] * n,
        "prompt_anchors": [f"prompt-{i}" for i in range(n)],
        "sim_adj": [0.8], "sim_to_first": [1.0, 0.8],
        "trust": {"annotations": "confirmed", "forced": False},
        "analysis_meta": {"corpus": {"degraded": False}},
        "annotations": {0: {
            "name": "确认主问题", "resolved": True,
            "yield": "确认了讨论对象",
            "agent_note": "轮3 之前 AI 还没提到这个角度。11 轮，最长的支线。",
        }},
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "review.html"
        build_page([("复盘测试", {"cwd": "/x"}, res)], out)
        page = out.read_text(encoding="utf-8")

    check("正文不再出现「核对备注」字段", "核对备注" not in page)
    body, _, diagnostics = page.partition("方法与诊断")
    check("agent_note 内容不落在正文", "轮3 之前" not in body)
    check("agent_note 内容保留在诊断层", "轮3 之前" in diagnostics)


TEST_GROUPS = [
        ("P0-1 分析/标注错位", [t_thread_count_mismatch, t_signature_change,
                                t_analysis_id_mismatch, t_missing_relation,
                                t_valid_passes,
                                t_report_title_validation_is_backward_compatible,
                                t_evidence_turn_must_belong_to_thread]),
        ("P0-5 HTML 注入", [t_script_injection]),
        ("P1-1 静默过滤", [t_suspect_auto_not_silent, t_interrupt_is_auto]),
        ("P0-3 语料降级", [t_empty_corpus_is_degenerate]),
        ("可复现性", [t_determinism]),
        ("跨会话身份（前向兼容）", [t_cross_session_identity]),
        ("稳定原话锚点", [t_prompt_anchor_is_stable]),
        ("降级信息一致性", [t_degraded_metadata_is_honest]),
        ("三层产品结构", [t_product_language_and_layers,
                          t_report_title_has_its_own_semantics,
                          t_report_title_survives_annotation_handoff,
                          t_report_title_falls_back_to_confirmed_trunk,
                          t_false_merge_not_painted_as_confirmed_path,
                          t_engineering_ids_stay_out_of_the_body]),
        ("Agent 上下文成本", [t_agent_view_is_slim_but_analysis_is_not,
                              t_sweep_plan_covers_every_thread_in_one_call,
                              t_reply_budget_preserves_resolution_evidence]),
        ("可恢复安装与更新", [t_install_keeps_recoverable_backup]),
        ("产物体积与标注复用", [t_sim_matrix_is_opt_in,
                            t_realign_rescues_annotations]),
        ("Codex 输入适配", [t_codex_thread_normalization,
                            t_codex_ambient_context_is_not_a_prompt,
                            t_codex_retry_stream_alignment]),
]


def main() -> int:
    FAILURES.clear()
    for group, fns in TEST_GROUPS:
        print(f"\n[{group}]")
        for fn in fns:
            fn()

    print("\n" + "=" * 56)
    if FAILURES:
        print(f"✗ {len(FAILURES)} 项失败:")
        for f in FAILURES:
            print(f"  · {f}")
        return 1
    print("✓ 全部通过")
    return 0


class RegressionDiscovery(unittest.TestCase):
    """让 `python -m unittest discover` 真正执行这套回归，而不是显示 0 tests。"""

    def test_correctness_regressions(self) -> None:
        result = main()
        self.assertEqual(result, 0, "\n".join(FAILURES))


if __name__ == "__main__":
    sys.exit(main())

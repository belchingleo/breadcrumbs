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
from codex_source import normalize_thread          # noqa: E402
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
        "threads": {0: [1, 2], 1: [3, 4]}, "thread_id": [0, 0, 1, 1],
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
             "sim_prev": 0.8, "kind": None, "is_short": False,
             "l2_hits": {}, "l3_gap_min": 1.0, "thread": 1,
             "text_head": "继续案例", "argmax_to": 3},
        ],
        "sim_matrix": [[100] * n for _ in range(n)],
        "texts_head": ["主问题是什么", "继续主问题", "换个案例", "继续案例"],
        "texts_full": ["主问题是什么", "继续主问题", "换个案例", "继续案例"],
        "timestamps": ["2026-01-01T00:00:00Z"] * n,
        "prompt_anchors": [f"prompt-stable-{i}" for i in range(n)],
        "sim_adj": [0.8, 0.1, 0.8], "sim_to_first": [1.0, 0.8, 0.1, 0.1],
        "trust": {"annotations": "inferred", "forced": False},
        "analysis_meta": {"corpus": {"degraded": False}},
    }
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "review.html"
        build_page([("复盘测试", {"cwd": "/x"}, res)], out)
        page = out.read_text(encoding="utf-8")
    for phrase in ("讨论路径", "这次改变了什么", "建议优先继续",
                   "每条思路留下了什么", "方法与诊断"):
        check(f"页面包含「{phrase}」", phrase in page)
    check("旧仪表盘标题已移除", "注意力分叉可视化" not in page)
    check("稳定 prompt 锚点进入页面", "prompt-stable-0" in page)
    check("未核实状态使用虚线语言", "虚线表示仅按用户提问的结构推断" in page)


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
    check("宿主标题退到上下文层",
          '<p class="report-context">评估项目可行性与改进方向 · 对话复盘</p>' in page)
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
                          t_false_merge_not_painted_as_confirmed_path]),
        ("可恢复安装与更新", [t_install_keeps_recoverable_backup]),
        ("产物体积与标注复用", [t_sim_matrix_is_opt_in,
                            t_realign_rescues_annotations]),
        ("Codex 输入适配", [t_codex_thread_normalization,
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

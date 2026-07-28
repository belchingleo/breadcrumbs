"""渲染前校验标注与分析是否匹配。不匹配就拒绝出图。

为什么必须拒绝而不是警告（P0-1）
--------------------------------
一张错位但漂亮的路线图, 比一份承认不确定的台账危险得多:
视觉越完整, 错误越难被察觉, 而用户对这张图的信任是产品的全部根基。
所以任何身份不一致都是硬失败, 不允许「尽力渲染」。

用法
    python3 validate_annotations.py analysis.json annotations.json
    退出码 0 = 通过, 1 = 拒绝渲染
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

VALID_OUTCOME = {"conclusion", "assumption", "unresolved"}
VALID_SPAWNED = {"user", "assistant", "both", "unknown"}
VALID_RELATION = {"redirected", "supplied", "blocked", "tangent", "dropped"}

# 命名规则的主规则不是句法（对象+张力、8-18字）, 而是**用词来源**:
# 用用户自己的词造的名字, 用户是「认出」; 用别人的词造的漂亮名字,
# 用户要「再理解一次」。所以要求名字的实义字过半来自该线首轮原文。
# 这条可以机械检查, 因此写成校验而不是只写进文档。
# 语义压缩必然会引入少量原话外的概括词；阈值过高会让几乎所有好标题都报警。
# 25% 仍能抓住“标题与起点完全无关”的假合并，同时允许正常的语义命名。
NAME_GROUND_RATIO = 0.25
_FUNCTION_CHARS = set("的了是在和与及或对为把被从到与之其此该这那些个可不没有"
                      "很更最都也还就才又再吧呢啊呀嘛哦么吗你我他它们如果因为"
                      "所以但是而且然后现在已经还是一二三四五六七八九十（）()"
                      "，。？！、；：“”‘’…—-《》〈〉【】[]{}·  \t\n")


def name_grounding(name: str, anchor_text: str) -> float:
    """名字里有多大比例的实义字出现在首轮原文中。1.0 = 全部来自原话。"""
    if not name or not anchor_text:
        return 1.0
    src = set(anchor_text)
    content = [c for c in name if c not in _FUNCTION_CHARS]
    if not content:
        return 1.0
    return sum(1 for c in content if c in src) / len(content)


def validate(analysis: dict, ann: dict,
             warnings: list[str] | None = None) -> list[str]:
    """返回硬错误列表（非空即拒绝渲染）。软提示写入 warnings。

    区分二者是有意的: 身份错位是确定性事实, 必须拦；
    命名是否贴近原话是语言判断, 会有合理例外, 只提示不拦。
    """
    errs: list[str] = []
    warn = warnings if warnings is not None else []
    a_threads = {t["id"]: t for t in analysis.get("threads", [])}
    n_threads = ann.get("threads", [])

    # --- 报告级语义 ---
    # 旧标注没有 report，仍允许渲染并由页面降级命名；新标注一旦声明 report，
    # title 就必须完整，避免出现“有标题结构、没有实际标题”的半成品。
    report_meta = ann.get("report")
    if report_meta is None:
        warn.append(
            "标注未填写 report.title；报告将降级使用主问题名称或宿主会话标题")
    elif not isinstance(report_meta, dict):
        errs.append("report 必须是对象，至少包含 title")
    else:
        report_title = str(report_meta.get("title") or "").strip()
        report_subtitle = str(report_meta.get("subtitle") or "").strip()
        if not report_title:
            errs.append("report.title 不能为空")
        else:
            if len(report_title) < 8 or len(report_title) > 28:
                warn.append(
                    f"报告标题长度为 {len(report_title)} 字，建议控制在 8–28 字")
            if report_title.startswith("从") and "到" in report_title[1:]:
                warn.append(
                    "报告标题使用了“从 A 到 B”；建议让主标题说明问题对象，"
                    "把变化过程放进 subtitle")
        if report_subtitle and len(report_subtitle) > 80:
            warn.append(
                f"报告副标题长度为 {len(report_subtitle)} 字，建议控制在 80 字以内")

    # --- 分析级身份 ---
    aid = analysis.get("analysis_id")
    n_aid = ann.get("analysis_id")
    if n_aid and aid and n_aid != aid:
        errs.append(
            f"analysis_id 不一致：标注针对 {n_aid}，当前分析是 {aid}。\n"
            f"    → 分析被重跑过，旧标注不能套用到新线路划分上。")
    if not n_aid:
        errs.append("标注缺少 analysis_id。请在 annotations.json 顶层写入"
                    f" \"analysis_id\": \"{aid}\"")

    # --- 数量与重复 ---
    seen = set()
    for t in n_threads:
        tid = t.get("id")
        if tid in seen:
            errs.append(f"线{tid} 被标注了多次")
        seen.add(tid)

    extra = seen - set(a_threads)
    missing = set(a_threads) - seen
    if extra:
        errs.append(f"标注了不存在的线路: {sorted(extra)}"
                    f"（分析只有 {sorted(a_threads)}）")
    if missing:
        errs.append(f"以下线路没有标注: {sorted(missing)}")

    # --- 逐条内容校验 ---
    for t in n_threads:
        tid = t.get("id")
        src = a_threads.get(tid)
        if not src:
            continue
        tag = f"线{tid}"

        sig = t.get("thread_signature")
        if sig and sig != src.get("thread_signature"):
            # 签名不符有两种情况, 后果完全不同, 不该一律判死:
            #   ① 起点也变了 -> 这条标注对的是另一条线, 必须拒绝;
            #   ② 起点没变, 只是轮次构成变了（算法调参、重划边界）
            #      -> 标注的语义判断多半仍成立, 只是覆盖范围变了。
            # content_signature 只由首轮原文决定、与轮次无关, 正是用来区分
            # 这两种情况的。以前一律硬失败, 导致每次算法微调都要整批重标——
            # 实测重跑一份报告时只能手写脚本按 anchor_quote 重新对齐。
            same_origin = (
                t.get("content_signature")
                and t["content_signature"] == src.get("content_signature")
            ) or (
                t.get("anchor_quote") and src.get("anchor_quote")
                and t["anchor_quote"][:20] == src["anchor_quote"][:20]
            )
            if same_origin:
                warn.append(
                    f"{tag} 起点未变，但轮次构成变了"
                    f"（{src.get('turn_count','?')} 轮）。"
                    f"标注可能仍适用，请复核覆盖范围是否还对：\n"
                    f"      「{src.get('anchor_quote','')}」")
                t["thread_signature"] = src.get("thread_signature")
            else:
                errs.append(
                    f"{tag} 内容签名不符，且起点也不同——这条标注对的是另一条线。\n"
                    f"    分析起于：「{src.get('anchor_quote','')}」\n"
                    f"    标注认为：「{t.get('name') or t.get('anchor_quote') or '(无名)'}」")
        elif not sig:
            errs.append(f"{tag} 缺少 thread_signature（应为 "
                        f"{src.get('thread_signature')}）")

        # 人可读的兜底核验: 起点原话
        aq = t.get("anchor_quote")
        if aq and src.get("anchor_quote") and aq[:20] != src["anchor_quote"][:20]:
            errs.append(
                f"{tag} 起点原话对不上：\n"
                f"    分析：「{src['anchor_quote']}」\n"
                f"    标注：「{aq}」")

        for field in ("name", "topic", "yield"):
            if not t.get(field):
                errs.append(f"{tag} 缺少必填字段 {field}")

        oc = t.get("outcome")
        if oc not in VALID_OUTCOME:
            errs.append(f"{tag} outcome 非法: {oc!r}（应为 {VALID_OUTCOME}）")

        sb = t.get("spawned_by")
        if sb not in VALID_SPAWNED:
            errs.append(f"{tag} spawned_by 非法: {sb!r}")

        rel = t.get("relation_to_trunk")
        if not src.get("is_trunk"):
            if rel is None:
                errs.append(f"{tag} 未填 relation_to_trunk（支线必填）")
            elif rel not in VALID_RELATION:
                errs.append(f"{tag} relation_to_trunk 非法: {rel!r}")

        if t.get("resolved") not in (True, False):
            errs.append(f"{tag} resolved 必须是 true/false")

        # 线路起点不一定是最能支撑语义判断的那句话（尤其算法发生假合并时）。
        # evidence_turn 允许 agent 把“查看原话”指向更诚实的证据，但不得跨线。
        evidence_turn = t.get("evidence_turn")
        if evidence_turn is not None:
            allowed_turns = {turn.get("turn") for turn in src.get("turns", [])}
            if evidence_turn not in allowed_turns:
                errs.append(
                    f"{tag} evidence_turn={evidence_turn} 不属于这条线"
                    f"（可选 {sorted(x for x in allowed_turns if x is not None)}）"
                )

        # 「未解决」是否定性断言, 必须出示证据。
        # 实测教训: 曾有一轮标注把四条线的 yield/resolved 全部靠用户 prompt
        # 猜出来, 一次都没读线尾之后的 AI 回复, 结果把两条已经得到明确回答的
        # 线判成悬空——其中一条 AI 已给出「许可范围比提纲窄」的实质结论。
        # 光靠文档提醒挡不住这种省略, 只能让它在校验层无法通过。
        if t.get("resolved") is False or t.get("outcome") == "unresolved":
            ev = t.get("resolution_evidence")
            if not ev or not str(ev).strip():
                errs.append(
                    f"{tag} 声称未解决, 但没有 resolution_evidence。\n"
                    f"    → 必须先读线尾之后的 AI 回复再下这个结论:\n"
                    f"      fetch_reply.py <session> --reply {src.get('last_turn','<末轮>')}\n"
                    f"    → 然后在 resolution_evidence 写明你看到了什么"
                    f"（例如「轮{src.get('last_turn','N')} 之后 AI 只重述了问题，未给判断」）")

        # 命名接地度（软提示）
        name = t.get("name") or ""
        anchor = src.get("anchor_quote") or ""
        ratio = name_grounding(name, anchor)
        if ratio < NAME_GROUND_RATIO:
            warn.append(
                f"{tag} 名字「{name}」只有 {ratio:.0%} 的实义字来自原话，"
                f"用户可能认不出。原话：「{anchor[:40]}」")
        if len(name) > 24:
            warn.append(f"{tag} 名字过长（{len(name)} 字），建议 8–18 字")

    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("analysis")
    ap.add_argument("annotations")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    analysis = json.loads(Path(args.analysis).read_text(encoding="utf-8"))
    ann = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
    warns: list[str] = []
    errs = validate(analysis, ann, warns)
    if warns and not args.quiet:
        print(f"⚠ {len(warns)} 项命名提示（不阻断）：", file=sys.stderr)
        for w in warns:
            print(f"  · {w}", file=sys.stderr)

    if errs:
        print(f"✗ 标注校验失败（{len(errs)} 项），拒绝渲染：\n", file=sys.stderr)
        for e in errs:
            print(f"  · {e}", file=sys.stderr)
        print("\n请重新标注，或对当前 analysis.json 重跑第 3 步。", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"✓ 标注校验通过（{len(ann.get('threads', []))} 条线）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

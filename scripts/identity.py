"""分析结果与标注的身份标识。

存在的理由（P0-1）
------------------
分析重跑后线路编号会变（换算法、换语料、修 bug 都会变）。如果标注仍按
顺序编号套用, 就会出现最危险的一类错误: **报告看起来完整专业, 但语义
标签贴到了错误的线路上**。本项目实测发生过——线数从 9 变 8 之后, 旧标注
的线4~线7 全部错位一格, 线8 被静默丢弃, 而 report.py 照常出图不报错。

因此每份分析带三重身份:
  analysis_id        这次分析的唯一标识
  session_fingerprint 源会话内容指纹（换了会话立刻能发现）
  thread_signature   每条线的内容签名（不依赖顺序编号）

外加一条给人看的兜底: anchor_quote。哈希只能说「不匹配」,
起点原话能让人一眼看出**错在哪条线**。
"""
from __future__ import annotations

import hashlib

ALGORITHM_VERSION = "0.5.0"


def _sha(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def session_fingerprint(texts: list[str]) -> str:
    """源会话的内容指纹: 只取真人 prompt, 与解析细节无关。"""
    return "sha256:" + _sha(str(len(texts)), *texts)[:32]


def analysis_id(fingerprint: str, theta: float, corpus_digest: str) -> str:
    """同一会话 + 同一算法 + 同一语料 => 同一 id（可复现性检查）。"""
    return "sha256:" + _sha(fingerprint, ALGORITHM_VERSION,
                            f"{theta:.4f}", corpus_digest)[:32]


def thread_signature(turns: list[int], first_text: str) -> str:
    """线路内容签名。轮次集合 + 起点原文, 不含线路编号。

    线路划分只要变了（合并/拆分/边界移动）, 签名就会变, 旧标注自然失配。
    """
    return "sha256:" + _sha(",".join(map(str, turns)), first_text[:200])[:24]


def content_signature(first_text: str) -> str:
    """只由内容决定的签名, 不含会话相对轮次。

    thread_signature 里带 turn ids, 因此天然是会话内的——换个会话,
    同一个问题的签名必然不同。跨会话匹配（A 会话搁置的问题是否在 C 会话
    被处理过）需要一个与轮次无关的锚。现在不实现跨会话功能, 但身份体系
    必须先留好这个口子, 否则将来无法回溯匹配已有标注。
    """
    norm = " ".join(first_text.split())[:200]
    return "sha256:" + _sha("content", norm)[:24]


def corpus_digest(texts: list[str]) -> str:
    """语料摘要。语料变了相似度就会变, 必须参与 analysis_id。"""
    return "sha256:" + _sha(str(len(texts)),
                            *(t[:60] for t in texts[:200]))[:16]


def anchor_quote(text: str, limit: int = 60) -> str:
    """给人看的兜底校验: 这条线起于哪句话。"""
    one = " ".join(text.split())
    return one[:limit] + ("…" if len(one) > limit else "")


def prompt_anchor(text: str, timestamp: str = "", source_id: str = "") -> str:
    """返回可放进 HTML id 的稳定 prompt 锚点。

    用户认识的是自己当时说过的话，而不是「第 18 轮」。锚点因此以原文为
    主体，并用消息时间和源记录 id 区分内容相同的重发。它不依赖线路编号，
    重新划线后仍然能指向同一条用户输入。
    """
    normalized = " ".join(text.split())
    return "prompt-" + _sha(normalized, timestamp or "",
                            source_id or "")[:16]

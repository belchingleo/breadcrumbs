"""纯 Python 的字符 n-gram TF-IDF + 余弦相似度。零第三方依赖。

为什么不用 scikit-learn
----------------------
这个 skill 的目标用户是人文研究者/编辑, 不是工程师。要求他们先建 venv、
pip install numpy scikit-learn, 会在第一步就劝退大部分人。
而本项目对相似度的全部需求只有: 字符 n-gram TF-IDF + 余弦矩阵 + argmax/中位数。
语料规模也小（约 10^3 篇短文本）, 纯 Python 的 dict 完全够用（实测秒级）。
用零依赖换掉两个重量级包, 对分发是决定性的。

与 sklearn 的对齐
----------------
等价于 TfidfVectorizer(analyzer="char", ngram_range=(2,4),
                       min_df=2, sublinear_tf=True) + L2 归一化,
再取内积。数值上不会逐位相同（sklearn 的 idf 平滑略有差异）, 但
块结构与排序一致——本项目只依赖排序, 不依赖绝对值。
"""
from __future__ import annotations

import math
import re
from collections import Counter

_WS = re.compile(r"\s+")


def ngrams(text: str, lo: int = 2, hi: int = 4) -> Counter:
    """字符 n-gram 计数。空白归一, 不做分词（中文无需分词）。"""
    t = _WS.sub(" ", text).strip()
    c = Counter()
    L = len(t)
    for n in range(lo, hi + 1):
        if L < n:
            break
        for i in range(L - n + 1):
            c[t[i:i + n]] += 1
    return c


class TfidfModel:
    """在语料上拟合 IDF, 之后把任意文本转成 L2 归一的稀疏向量（dict）。"""

    def __init__(self, lo: int = 2, hi: int = 4, min_df: int = 2,
                 sublinear_tf: bool = True):
        self.lo, self.hi = lo, hi
        self.min_df = min_df
        self.sublinear_tf = sublinear_tf
        self.idf: dict[str, float] = {}

    def fit(self, docs: list[str]) -> "TfidfModel":
        df = Counter()
        for d in docs:
            df.update(ngrams(d, self.lo, self.hi).keys())
        n = max(1, len(docs))
        # 与 sklearn 同构的平滑 idf
        self.idf = {
            g: math.log((1 + n) / (1 + c)) + 1.0
            for g, c in df.items() if c >= self.min_df
        }
        return self

    def transform(self, text: str) -> dict[str, float]:
        tf = ngrams(text, self.lo, self.hi)
        vec = {}
        for g, c in tf.items():
            w = self.idf.get(g)
            if w is None:
                continue                       # 训练语料里没见过 -> 丢弃
            t = (1.0 + math.log(c)) if (self.sublinear_tf and c > 0) else c
            vec[g] = t * w
        norm = math.sqrt(sum(v * v for v in vec.values()))
        if norm > 0:
            for g in vec:
                vec[g] /= norm
        return vec


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    """两个已 L2 归一的稀疏向量的内积。遍历较短的那个。"""
    if len(a) > len(b):
        a, b = b, a
    s = 0.0
    for g, v in a.items():
        w = b.get(g)
        if w is not None:
            s += v * w
    return s


def sim_matrix(vecs: list[dict]) -> list[list[float]]:
    """对称相似度矩阵。n<=几百, O(n^2) 足够。"""
    n = len(vecs)
    m = [[0.0] * n for _ in range(n)]
    for i in range(n):
        m[i][i] = 1.0 if vecs[i] else 0.0
        for j in range(i + 1, n):
            v = cosine(vecs[i], vecs[j])
            m[i][j] = m[j][i] = v
    return m


# --- 少量数值辅助, 省掉 numpy -------------------------------------------

def argmax(xs) -> int:
    best, bi = None, 0
    for i, v in enumerate(xs):
        if best is None or v > best:
            best, bi = v, i
    return bi


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = len(s)
    return s[k // 2] if k % 2 else (s[k // 2 - 1] + s[k // 2]) / 2.0


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0

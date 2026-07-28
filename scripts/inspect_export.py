"""探测任意 JSON / JSONL 导出文件的结构, 用于在未知 schema 时快速摸清字段。

用途: claude.ai 的导出包内部结构官方未公开文档化, 拿到文件后先用它扫一遍,
再据此写针对性的 prompt 提取逻辑, 避免瞎猜 schema。

用法:
    python3 inspect_export.py <file.json|file.jsonl|导出目录>
    python3 inspect_export.py <file> --depth 4 --samples 2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_STR_PREVIEW = 60


def type_name(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def preview(v) -> str:
    if isinstance(v, str):
        one = " ".join(v.split())
        return f'"{one[:MAX_STR_PREVIEW]}{"…" if len(one) > MAX_STR_PREVIEW else ""}"'
    if isinstance(v, (int, float, bool)) or v is None:
        return str(v)
    return ""


def describe(node, depth: int, max_depth: int, indent: int = 0,
             samples: int = 1) -> None:
    pad = "  " * indent
    if depth > max_depth:
        print(f"{pad}…（已达深度上限）")
        return

    if isinstance(node, dict):
        print(f"{pad}dict, {len(node)} keys")
        for k, v in node.items():
            t = type_name(v)
            extra = ""
            if isinstance(v, list):
                extra = f"[{len(v)}]"
            elif isinstance(v, dict):
                extra = f"{{{len(v)}}}"
            pv = preview(v)
            print(f"{pad}  · {k}: {t}{extra} {pv}")
            if isinstance(v, (dict, list)) and depth < max_depth:
                describe(v, depth + 1, max_depth, indent + 2, samples)

    elif isinstance(node, list):
        print(f"{pad}list, {len(node)} items")
        for i, item in enumerate(node[:samples]):
            print(f"{pad}  [样本 {i}] {type_name(item)}")
            if isinstance(item, (dict, list)):
                describe(item, depth + 1, max_depth, indent + 2, samples)
            else:
                print(f"{pad}    {preview(item)}")
        if len(node) > samples:
            print(f"{pad}  …（另有 {len(node) - samples} 项，结构假定同上）")


def load_any(path: Path):
    """尽量宽容地读取 JSON 或 JSONL。返回 (data, kind)。"""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None, "empty"
    # 先试整体 JSON
    try:
        return json.loads(text), "json"
    except json.JSONDecodeError:
        pass
    # 再试 JSONL
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    if rows:
        return rows, "jsonl"
    return None, "unparseable"


def scan_for_text_fields(node, path: str = "", found: dict | None = None,
                         depth: int = 0) -> dict:
    """递归找出所有较长的字符串字段, 它们最可能承载对话正文。"""
    if found is None:
        found = {}
    if depth > 8:
        return found
    if isinstance(node, dict):
        for k, v in node.items():
            scan_for_text_fields(v, f"{path}.{k}" if path else k, found, depth + 1)
    elif isinstance(node, list):
        for item in node[:3]:
            scan_for_text_fields(item, f"{path}[]", found, depth + 1)
    elif isinstance(node, str) and len(node) > 40:
        rec = found.setdefault(path, {"count": 0, "max_len": 0, "sample": ""})
        rec["count"] += 1
        if len(node) > rec["max_len"]:
            rec["max_len"] = len(node)
            rec["sample"] = " ".join(node.split())[:80]
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="JSON/JSONL 文件, 或导出解压后的目录")
    ap.add_argument("--depth", type=int, default=3, help="展开深度 (默认 3)")
    ap.add_argument("--samples", type=int, default=1, help="list 取几个样本 (默认 1)")
    args = ap.parse_args()

    target = Path(args.target).expanduser()
    if not target.exists():
        print(f"不存在: {target}", file=sys.stderr)
        return 1

    files = []
    if target.is_dir():
        files = sorted(
            [p for p in target.rglob("*") if p.suffix.lower() in (".json", ".jsonl")]
        )
        print(f"目录内 JSON/JSONL 文件 {len(files)} 个:")
        for f in files:
            print(f"  {f.relative_to(target)}  ({f.stat().st_size/1024:.0f}K)")
        print()
    else:
        files = [target]

    for f in files:
        print("=" * 70)
        print(f"文件: {f.name}  ({f.stat().st_size/1024:.0f}K)")
        print("=" * 70)
        data, kind = load_any(f)
        if data is None:
            print(f"无法解析 ({kind})\n")
            continue
        print(f"格式: {kind}\n")
        print("--- 结构 ---")
        describe(data, 0, args.depth, 0, args.samples)

        print("\n--- 候选正文字段（长字符串所在路径）---")
        fields = scan_for_text_fields(data)
        if not fields:
            print("未发现长文本字段")
        for p, rec in sorted(fields.items(), key=lambda x: -x[1]["max_len"])[:12]:
            print(f"  {p}")
            print(f"    出现 {rec['count']} 次, 最长 {rec['max_len']} 字")
            print(f"    样本: {rec['sample']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

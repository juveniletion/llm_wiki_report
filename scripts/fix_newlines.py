# -*- coding: utf-8 -*-
"""
fix_newlines.py — **换行符卫生修复**（机械操作，不涉及语义）

修什么
------
把乘性重复的回车 `\\r\\r…\\n` 与孤立 `\\r` 规范化成单个 `\\n`。

**为什么会有这种文件**：`Path.write_text()` / `open()` 默认 `newline=None`，
写入时把文本里的 `\\n` 翻译成本平台换行（Windows = `\\r\\n`）；而读取工具是
**裸 decode**、不做逆翻译——于是每次"读→改→写"都多留一个 `\\r`：

    第 1 次 patch → `\\r\\n`      第 2 次 → `\\r\\r\\n`      第 9 次 → `\\r×8\\n`

本库实测：一次摄入做了 9 次 `patch_wiki_article`，`wiki/costs/三产品成本基线.md`
195 行的文件里塞进了 1299 个 `\\r`。`state.py` 按 `\\n` 切行时看到 1300 行，
markdown 表格解析整段失效，`state/metrics.json` 从 **555 条静默掉到 339 条**。

⚠️ **危险点是"静默"**：不报错、文件能正常打开、肉眼看着也对。
只有 `git diff`（整文件显示为改）和抽数（数量骤降）会露馅。

为什么"乘性"是 bug 而"CRLF"不是
--------------------------------
`\\r\\n` 单独出现只是平台差异，内容不变，容忍。
`\\r\\r` 一定是重复翻译，**内容层面多出了字符**，必须修。

用法
----
    python scripts/fix_newlines.py --check     # 只报告，不改（先跑这个）
    python scripts/fix_newlines.py --apply     # 就地修复
    python scripts/fix_newlines.py --apply --path wiki/costs/三产品成本基线.md

修完请务必重建派生视图（它们可能已被污染的内容带歪）：

    python scripts/state.py --build
    python scripts/finalize.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

TEXT_EXT = (".md", ".py", ".json", ".yaml", ".yml", ".txt", ".sql", ".csv")
SKIP_DIRS = {".git", ".index", "__pycache__", ".venv"}

# `\r+\n` → `\n`（覆盖 CRLF 与乘性重复）；再把残余孤立 `\r` → `\n`
_MULTI_CRLF = re.compile(rb"\r+\n")
_LONE_CR = re.compile(rb"\r+")


def normalize(b: bytes) -> bytes:
    """把任意 CR/LF 组合规范成单个 LF。bytes 级操作，不碰编码。"""
    return _LONE_CR.sub(b"\n", _MULTI_CRLF.sub(b"\n", b))


def scan(root: Path, only: Path | None = None) -> List[Tuple[Path, int, int, int]]:
    """
    返回 [(路径, 乘性重复数, 孤立CR数, 字节数)]，只含**需要修**的文件。
    乘性重复数是判据（`\\r\\r`），孤立 `\\r` 也一并列出。
    """
    out: List[Tuple[Path, int, int, int]] = []
    targets = [only] if only else sorted(root.rglob("*"))
    for p in targets:
        if not p.is_file():
            continue
        if any(s in p.parts for s in SKIP_DIRS):
            continue
        if p.suffix.lower() not in TEXT_EXT:
            continue
        try:
            b = p.read_bytes()
        except OSError:
            continue
        if b.count(b"\r") == 0:
            continue
        # 乘性重复的处数（`\r\r` 出现几次）
        mult = len(re.findall(rb"\r\r", b))
        # 不以 `\n` 结尾的回车数（`\r\r\r\n` 记 2）。这才是"多出来的字符"的量。
        off_tail = len(re.findall(rb"\r(?!\n)", b))
        if mult or off_tail:
            out.append((p, mult, off_tail, len(b)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="修复乘性重复的回车（换行符卫生）")
    ap.add_argument("--check", action="store_true", help="只报告，不修改")
    ap.add_argument("--apply", action="store_true", help="就地修复")
    ap.add_argument("--path", help="只处理某个文件（相对 wiki 根）")
    a = ap.parse_args()

    if not (a.check or a.apply):
        ap.print_help()
        return 1

    only = (WIKI_ROOT / a.path).resolve() if a.path else None
    hits = scan(WIKI_ROOT, only)

    if not hits:
        print("✅ 未发现乘性换行符污染。")
        return 0

    print(f"{'文件':<50}{'乘性':>6}{'多余CR':>7}{'大小':>9}")
    print("-" * 74)
    total = 0
    for p, mult, off_tail, size in hits:
        print(f"{p.relative_to(WIKI_ROOT).as_posix()[:50]:<50}{mult:>6}{off_tail:>7}{size:>9,}")
        total += off_tail

    print(f"\n共 {len(hits)} 个文件，{total} 个多余回车字符待清除。")

    if a.check:
        print("（--check 模式，未修改。加 --apply 修复）")
        return 1

    for p, _, _, _ in hits:
        before = p.read_bytes()
        after = normalize(before)
        if after == before:
            continue
        # 先写临时文件再原子替换：避免写一半崩掉毁掉源文件
        tmp = p.with_suffix(p.suffix + ".fixnl")
        tmp.write_bytes(after)
        tmp.replace(p)
        print(f"  修复 {p.relative_to(WIKI_ROOT).as_posix()}  "
              f"{len(before):,} → {len(after):,} B")

    print("\n⚠️ 现在必须重建派生视图（它们可能已按被污染的内容算过）：")
    print("     python scripts/state.py --build")
    print("     python scripts/finalize.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())

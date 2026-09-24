# -*- coding: utf-8 -*-
"""
check_math.py — **算术校验**（机械地复算 wiki 里的每个派生值）

为什么需要
----------
`check_evidence.py` 只能查"这个数在不在 raw 里"，**查不出算错**。
本库实际发生的两次事故都属于这一类：

  1. 对标词条产量比写 `84%`，实测 `82.89%`
  2. 六味同比写 `+4.08%`，实测 `4.085155%` → `+4.09%`（**截断**）

第二次直到 `report_facts.py` 用 `Decimal` 独立复算才暴露——
**"字面量在 raw 里"和"这个字面量算对了"是两件事。**

本脚本做第二件事：从 wiki 的 markdown 表格里抽 `列A ÷ 列B = 声称值` 形式的
派生断言，用 `Decimal` 复算，与声称值比对。

⚠️ **不是所有派生值都能机械复算**——加权平均、跨表引用、区间合并等
需要语义理解。本脚本只覆盖它能可靠识别的模式，**覆盖不到的不报**，
所以「无告警」≠「全对」。这是刻意的取舍：宁缺毋滥，不制造假阳性
（本库在 `conflict.py` 上就吃过"正则太猛导致满屏假冲突"的亏）。

用法
----
    python scripts/check_math.py            # 扫全库
    python scripts/check_math.py -v         # 显示通过项
"""
from __future__ import annotations

import argparse
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
WIKI = WIKI_ROOT / "wiki"

# ⚠️ 精确匹配，宁缺毋滥。踩过的坑（与本库 `conflict.py` 同病）：
#   宽松正则会：(1) 不认千分位逗号 → `247,000 ÷ 298,000` 被截成 `000 ÷ 298`；
#              (2) 误拆复合算式 → `(1 − 5%) ÷ 10 ÷ 12` 被当成 `10 ÷ 12`。
#    结果满屏假告警，而**会喊狼来了的校验器等于没有**——没人会再看它的输出。
#    所以这里只认两种**无歧义**形态，识别不了的**直接跳过**（记为"未能复算"）。

_NUM = r"\d[\d,]*(?:\.\d+)?"
# 形态 A：整句内联 `A ÷ B = X%`
_INLINE = re.compile(rf"(?<![\d.,])({_NUM})\s*÷\s*({_NUM})\s*=\s*`?([-+]?\d+(?:\.\d+)?)\s*%")
# 形态 B：表格里「算式独占一格，声称值独占后一格」
_CELL_FORMULA = re.compile(rf"^\s*`?({_NUM})\s*÷\s*({_NUM})\s*`?\s*$")
_CELL_PCT = re.compile(r"^\s*`?([-+]?\d+(?:\.\d+)?)\s*%`?\s*$")


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", ""))


def _close(a: Decimal, claim: Decimal) -> bool:
    """两位小数的声称值，允许 0.005 的舍入余量。"""
    return abs(a - claim) <= Decimal("0.005")


def scan(text: str) -> List[Tuple[int, str, str, str]]:
    """返回 (行号, 算式描述, 复算值, 声称值)。只收无歧义形态。"""
    out: List[Tuple[int, str, str, str]] = []
    for ln, line in enumerate(text.split("\n"), 1):
        if "÷" not in line:
            continue

        # 形态 A：内联。⚠️ 必须检查左边界——否则复合算式会被从尾巴上截走一段。
        #   例：`(1 − 5%) ÷ 10 ÷ 12 = 0.7917%` 会被误判成 `10 ÷ 12`（= 83.33%）。
        for m in _INLINE.finditer(line):
            prefix = line[:m.start()].rstrip()
            if prefix and (prefix[-1] in "÷%0123456789." or prefix.endswith("÷")):
                continue                    # 是算式链的一段，不是独立的 A ÷ B = X%
            try:
                a, b, claim = _dec(m.group(1)), _dec(m.group(2)), _dec(m.group(3))
            except InvalidOperation:
                continue
            if b == 0:
                continue
            out.append((ln, f"{m.group(1)} ÷ {m.group(2)}", f"{a/b*100:.4f}", str(claim)))

        # 形态 B / C：表格单元格。
        #
        # ⚠️ 本库的表格有两种**相反**的列序，都必须覆盖：
        #   B 算式在前：| 银黄口服液 | ... | `0.31 ÷ 10.90` | `+2.84%` |
        #   C 声称值在前：| 银黄口服液 | 10.71 | 11.21 | `+4.67%` | `0.50 ÷ 10.71` |
        #      ← **六味 `+4.08%` 那个错误就在 C 形态里**。只做 B 会漏掉它。
        #
        # 消歧规则：**该行恰好只有一个百分比格、且恰好只有一个算式格**时才配对。
        # 多于一个说明有歧义，跳过（宁缺毋滥）。
        if "|" not in line:
            continue
        cells = line.split("|")
        formulas = [(i, _CELL_FORMULA.match(c)) for i, c in enumerate(cells)]
        pcts = [(i, _CELL_PCT.match(c)) for i, c in enumerate(cells)]
        formulas = [(i, m) for i, m in formulas if m]
        pcts = [(i, m) for i, m in pcts if m]
        if len(formulas) != 1 or len(pcts) != 1:
            continue
        (fi, fm), (pi, cm) = formulas[0], pcts[0]
        try:
            a, b, claim = _dec(fm.group(1)), _dec(fm.group(2)), _dec(cm.group(1))
        except InvalidOperation:
            continue
        if b:
            out.append((ln, f"{fm.group(1)} ÷ {fm.group(2)}"
                            f"（{'算式在前' if fi < pi else '声称值在前'}）",
                        f"{a/b*100:.4f}", str(claim)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="wiki 算术校验（复算派生值）")
    ap.add_argument("-v", "--verbose", action="store_true", help="显示通过项")
    a = ap.parse_args()

    bad: List[Tuple[str, int, str, str, str]] = []
    ok = 0
    files = sorted(p for p in WIKI.rglob("*.md") if p.name != "log.md")
    for p in files:
        text = p.read_text(encoding="utf-8", errors="replace")
        for ln, desc, computed, claim in scan(text):
            c, k = _dec(computed), _dec(claim)
            if _close(c, k):
                ok += 1
                if a.verbose:
                    print(f"  ✓ {p.relative_to(WIKI_ROOT)}:{ln}  {desc} = {computed}%")
            else:
                bad.append((p.relative_to(WIKI_ROOT).as_posix(), ln, desc, computed, claim))

    print(f"扫描 {len(files)} 个词条")
    print(f"  通过 {ok} · 可疑 {len(bad)}")
    if bad:
        print("\n⚠️  复算与声称值不符（可能是截断/算错/口径不同）：")
        for rel, ln, desc, computed, claim in bad:
            print(f"  {rel}:{ln}")
            print(f"      {desc}")
            print(f"      复算 {computed}%  vs  声称 {claim}%")
    else:
        print("\n✅ 未发现可机械复算的算术错误。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

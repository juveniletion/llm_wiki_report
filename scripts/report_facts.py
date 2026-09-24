# -*- coding: utf-8 -*-
"""
report_facts.py — **报告事实引擎**（确定性取数，LLM 不参与）

设计原则
--------
> **能用脚本代码做的事情，就不用 LLM 做。**

月度成本报告里 **90% 的内容是数字**——而数字全部能从 raw CSV 直算。
把算术交给 LLM 是双输：既慢、又可能算错（本库实测就被 evidence checker
抓出过 3 处算错：产量比、截断精度、链接层级）。

所以本模块把**全部确定性内容**一次算清，并给每个值附上**溯源坐标**：

    fact = {
      "value": 11.21, "display": "11.21", "unit": "元/盒",
      "coord": "[中药一厂_成本汇总_2026年1-6月.csv:单位成本(元/盒):银黄口服液&2026-05]",
      "source_file": "raw/csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv",
      "formula": None,          # 原始值；派生值才有公式
    }

LLM 只拿到**算术结论**去写叙事段落，不参与任何计算。

数据缺失的处置
--------------
**缺失不是 0。** 找不到就记进 `missing` 并让 `value=None`，
由调用方决定显示为 `—` 还是显式说明。绝不静默填零——
那是本库最忌讳的"看起来很专业"的编造。

用法
----
    python scripts/report_facts.py --list-products
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05 --json
    python scripts/report_facts.py --product 银黄口服液 --month 2026-05 \
        --monthly 2026-05 --out facts.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
RAW = WIKI_ROOT / "raw"

UNIT_PER_BOX = {"银黄口服液": "支", "板蓝根颗粒": "袋", "六味地黄胶囊": "粒"}
BOX_PACK = {"银黄口服液": 10, "板蓝根颗粒": 20, "六味地黄胶囊": 60}

# 制造费用五类 ↔ 模板措辞。CSV 列名是「人工(间接)」，模板叫「间接人工」。
#   [报告模板契约.md:3.3] 明确提示过这处名称映射。
MFG_TEMPLATE_NAMES = {
    "折旧费": "折旧费",
    "动力费(水电气)": "动力费(水电气)",
    "人工(间接)": "间接人工",
    "检验费": "检验费",
    "其他制造费用": "其他制造费用",
}


def _coord(fname: str, col: str, rows: str) -> str:
    return f"[{fname}:{col}:{rows}]"


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").rstrip("%")
    if s in ("", "—", "-", "–", "N/A", "无"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt(v: Optional[float], nd: int = 2) -> str:
    """原样呈现，不截断（SKILL.md 铁律 2.1）。补零允许。"""
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def _pct(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    """环比/同比 %。分母为 0 或缺失时返回 None（缺失≠0）。"""
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / prev * 100.0


def _read(name: str) -> List[Dict[str, str]]:
    p = RAW / name
    if not p.exists():
        raise SystemExit(f"raw 文件缺失: {name}")
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _pick(rows: List[Dict[str, str]], **kw) -> Optional[Dict[str, str]]:
    for r in rows:
        if all((r.get(k) or "").strip() == str(v) for k, v in kw.items()):
            return r
    return None


@dataclass
class Fact:
    """一个可溯源的事实。`value=None` 表示数据缺失（不是 0）。"""
    value: Optional[float]
    display: str
    unit: str = ""
    coord: str = ""
    source_file: str = ""
    formula: Optional[str] = None
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FactSet:
    product: str
    month: str
    prev_month: str
    year_ago_month: str
    facts: Dict[str, Fact] = field(default_factory=dict)
    monthly: List[Dict[str, Any]] = field(default_factory=list)   # 近 6 个月趋势
    materials: List[Dict[str, Any]] = field(default_factory=list)  # 原材料明细
    mfg: List[Dict[str, Any]] = field(default_factory=list)        # 制造费用五类
    peer: List[Dict[str, Any]] = field(default_factory=list)       # 对标
    missing: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)             # 已知不可自洽点

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["facts"] = {k: v.to_dict() for k, v in self.facts.items()}
        return d


# ---------------------------------------------------------------------------

def list_products() -> List[str]:
    rows = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    seen: List[str] = []
    for r in rows:
        p = (r.get("产品名称") or "").strip()
        if p and p not in seen:
            seen.append(p)
    return seen


def _shift_month(month: str, delta: int) -> Optional[str]:
    """`2026-05` ± delta 个月。**不跨出数据窗口**时返回，否则 None。"""
    y, m = int(month[:4]), int(month[5:7])
    m += delta
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


def build(product: str, month: str) -> FactSet:
    """算出某产品某月的全部报告事实。"""
    m26 = _read("csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv")
    m25 = _read("csv/cost_data/中药一厂_成本汇总_2025年1-6月.csv")
    bud = _read("csv/cost_data/中药一厂_预算数据_2026年.csv")
    lab = _read("csv/cost_data/中药一厂_人工工时明细_2026年1-6月.csv")
    mfg = _read("csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv")
    mat = _read("csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv")
    p2 = _read("csv/cost_data/中药二厂_成本汇总_2026年1-6月.csv")
    wx = _read("csv/market_data/药材市场价格行情_2026年上半年.csv")

    F26, F25, FB = ("中药一厂_成本汇总_2026年1-6月.csv",
                    "中药一厂_成本汇总_2025年1-6月.csv",
                    "中药一厂_预算数据_2026年.csv")
    FL, FM, FT = ("中药一厂_人工工时明细_2026年1-6月.csv",
                  "中药一厂_制造费用明细_2026年1-6月.csv",
                  "中药一厂_原材料消耗明细_2026年1-6月.csv")

    fs = FactSet(product=product, month=month,
                 prev_month=_shift_month(month, -1) or month,
                 year_ago_month="2025-" + month[5:])
    add = fs.facts.__setitem__
    if product not in UNIT_PER_BOX:
        fs.missing.append(f"未知产品: {product}")
        return fs

    cur = _pick(m26, 产品名称=product, 月份=month)
    prev = _pick(m26, 产品名称=product, 月份=fs.prev_month)
    yago = _pick(m25, 产品名称=product, 月份=fs.year_ago_month)
    budr = _pick(bud, 产品名称=product, 月份=month)

    # 实际有 9/10 月简报（一厂），但该简报只含单位成本 → 单独处理，见 brief
    if cur is None:
        fs.missing.append(f"2026 年 {month} 成本汇总缺失（该窗口 raw 无数据）")

    def raw(table, row, col, fname, tag) -> Optional[float]:
        if row is None:
            return None
        return _num(row.get(col))

    # ---- 2.1 核心指标一览 ----
    def three(col: str, label: str, table_cur, row_cur, table_prev, row_prev):
        c = raw(table_cur, row_cur, col, F26, f"{product}&{month}")
        p = raw(table_cur, row_prev, col, F26, f"{product}&{fs.prev_month}")
        key = col
        rng = f"{product}&{month}"
        if c is not None:
            add(key, Fact(c, _fmt(c, 0), "", _coord(F26, col, rng), F26))
        else:
            fs.missing.append(f"{label}（本月）")
        if p is not None:
            add("上月" + key, Fact(p, _fmt(p, 0), "", _coord(F26, col, f"{product}&{fs.prev_month}"), F26))
        d = _pct(c, p)
        if d is not None:
            add(key + "环比", Fact(d, f"{d:+.2f}%", "%", _coord(F26, col, rng), F26,
                                   formula=f"({_fmt(c,0)} − {_fmt(p,0)}) ÷ {_fmt(p,0)}"))

    # 产量
    three("产量(盒)", "产量", m26, cur, m26, prev)
    # 单位成本
    c = raw(m26, cur, "单位成本(元/盒)", F26, "")
    p = raw(m26, prev, "单位成本(元/盒)", F26, "")
    ya = raw(m25, yago, "单位成本(元/盒)", F25, "")
    bv = raw(bud, budr, "预算单位成本(元/盒)", FB, "")
    if c is not None:
        add("本月单位成本", Fact(c, _fmt(c), "元/盒",
            _coord(F26, "单位成本(元/盒)", f"{product}&{month}"), F26))
    if p is not None:
        add("上月单位成本", Fact(p, _fmt(p), "元/盒",
            _coord(F26, "单位成本(元/盒)", f"{product}&{fs.prev_month}"), F26))
    if ya is not None:
        add("去年单位成本", Fact(ya, _fmt(ya), "元/盒",
            _coord(F25, "单位成本(元/盒)", f"{product}&{fs.year_ago_month}"), F25))
    if bv is not None:
        add("预算单位成本", Fact(bv, _fmt(bv), "元/盒",
            _coord(FB, "预算单位成本(元/盒)", f"{product}&{month}"), FB))
    for key, num, den in (("单位成本环比", c, p), ("单位成本同比", c, ya),
                          ("单位成本预算偏差", c, bv)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%",
                          _coord(F26, "单位成本(元/盒)", f"{product}&{month}"),
                          F26, formula=f"({_fmt(num)} − {_fmt(den)}) ÷ {_fmt(den)}"))

    # 总成本
    tc = raw(m26, cur, "总成本(元)", F26, "")
    tp = raw(m26, prev, "总成本(元)", F26, "")
    ty = raw(m25, yago, "总成本(元)", F25, "")
    tb = raw(bud, budr, "预算总成本(元)", FB, "")
    for key, val, unit, tbl, tag in (
            ("本月总成本", tc, "元", F26, f"{product}&{month}"),
            ("上月总成本", tp, "元", F26, f"{product}&{fs.prev_month}"),
            ("去年总成本", ty, "元", F25, f"{product}&{fs.year_ago_month}"),
            ("预算总成本", tb, "元", FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), unit,
                          _coord(tbl, "总成本(元)" if "预算" not in key else "预算总成本(元)", tag), tbl))
    for key, num, den in (("总成本环比", tc, tp), ("总成本同比", tc, ty),
                          ("总成本预算偏差", tc, tb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(F26, "总成本(元)", f"{product}&{month}"),
                          F26, formula=f"({_fmt(num,0)} − {_fmt(den,0)}) ÷ {_fmt(den,0)}"))

    # 产量同比/预算偏差（产量环比已由 three() 产出）
    pv = raw(m26, cur, "产量(盒)", F26, "")
    pp = raw(m26, prev, "产量(盒)", F26, "")
    py = raw(m25, yago, "产量(盒)", F25, "")
    pb = raw(bud, budr, "预算产量(盒)", FB, "")
    for key, val, tbl, tag in (("去年同月产量", py, F25, f"{product}&{fs.year_ago_month}"),
                               ("预算产量", pb, FB, f"{product}&{month}")):
        if val is not None:
            add(key, Fact(val, _fmt(val, 0), "盒", _coord(tbl, "产量(盒)" if tbl == F25 else "预算产量(盒)", tag), tbl))
    for key, num, den in (("产量同比", pv, py), ("产量预算偏差", pv, pb)):
        v = _pct(num, den)
        if v is not None:
            add(key, Fact(v, f"{v:+.2f}%", "%", _coord(F26, "产量(盒)", f"{product}&{month}"),
                          F26, formula=f"({_fmt(num,0)} − {_fmt(den,0)}) ÷ {_fmt(den,0)}"))

    # 三要素（本月/上月/环比/预算/偏差）
    for col, tag, name in (("直接材料(元/盒)", "材料", "材料"), ("直接人工(元/盒)", "人工", "人工"),
                           ("制造费用(元/盒)", "制造", "制造费用")):
        cc = raw(m26, cur, col, F26, "")
        cp = raw(m26, prev, col, F26, "")
        cb = raw(bud, budr, "预算" + col, FB, "")
        if cc is not None:
            add(f"本月{tag}成本", Fact(cc, _fmt(cc), "元/盒",
                _coord(F26, col, f"{product}&{month}"), F26))
        if cp is not None:
            add(f"上月{tag}成本", Fact(cp, _fmt(cp), "元/盒",
                _coord(F26, col, f"{product}&{fs.prev_month}"), F26))
        if cb is not None:
            add(f"预算{tag}成本", Fact(cb, _fmt(cb), "元/盒",
                _coord(FB, "预算" + col, f"{product}&{month}"), FB))
        v = _pct(cc, cp)
        if v is not None:
            add(f"{name}成本环比" if name != "制造费用" else "制造费用环比",
                Fact(v, f"{v:+.2f}%", "%", _coord(F26, col, f"{product}&{month}"), F26,
                     formula=f"({_fmt(cc)} − {_fmt(cp)}) ÷ {_fmt(cp)}"))
        v = _pct(cc, cb)
        if v is not None:
            add(f"{name}预算偏差", Fact(v, f"{v:+.2f}%", "%",
                _coord(F26, col, f"{product}&{month}"), F26,
                formula=f"({_fmt(cc)} − {_fmt(cb)}) ÷ {_fmt(cb)}"))

    # ---- 2.2 成本结构（占比 + 贡献度）----
    # 贡献度 = 该要素的「单位成本变动额」÷ 「单位成本变动总额」
    #   [成本异动登记册] 已核验：三项贡献度之和必须 = 100%
    dc = raw(m26, cur, "直接材料(元/盒)", F26, "")
    dp = raw(m26, prev, "直接材料(元/盒)", F26, "")
    lc = raw(m26, cur, "直接人工(元/盒)", F26, "")
    lp = raw(m26, prev, "直接人工(元/盒)", F26, "")
    mc = raw(m26, cur, "制造费用(元/盒)", F26, "")
    mp = raw(m26, prev, "制造费用(元/盒)", F26, "")
    if None not in (dc, lc, mc):
        tot = dc + lc + mc
        for tag, val in (("材料", dc), ("人工", lc), ("制造费用", mc)):
            add(f"{tag}金额", Fact(val, _fmt(val), "元/盒",
                _coord(F26, f"直接{'材料' if tag=='材料' else '人工' if tag=='人工' else ''}(元/盒)"
                       if tag != "制造费用" else "制造费用(元/盒)", f"{product}&{month}"), F26))
            if tot:
                add(f"{tag}占比", Fact(val / tot * 100, f"{val/tot*100:.2f}%", "%",
                    _coord(F26, "单位成本(元/盒)", f"{product}&{month}"), F26,
                    formula=f"{_fmt(val)} ÷ {_fmt(tot)}"))
        delta_tot = (dc + lc + mc) - ((dp or 0) + (lp or 0) + (mp or 0)) if None not in (dp, lp, mp) else None
        if delta_tot:
            for tag, c0, p0 in (("材料", dc, dp), ("人工", lc, lp), ("制造费用", mc, mp)):
                if c0 is None or p0 is None:
                    continue
                d = c0 - p0
                add(f"{tag}贡献度", Fact(d / delta_tot * 100, f"{d/delta_tot*100:.2f}%", "%",
                    _coord(F26, "单位成本(元/盒)", f"{product}&{month}"), F26,
                    formula=f"({_fmt(c0)} − {_fmt(p0)}) ÷ {_fmt(delta_tot)}"))

    # ---- 3.2 直接人工四指标 ----
    lc_row = _pick(lab, 产品名称=product, 月份=month)
    lp_row = _pick(lab, 产品名称=product, 月份=fs.prev_month)
    if lc_row and lp_row and pv:
        def lab_calc(row, tag, rng):
            tot_l = _num(row.get("直接人工总额(元)"))
            hours = _num(row.get("总工时(小时)"))
            people = _num(row.get("生产人数(人)"))
            days = _num(row.get("工作天数(天)"))
            prod = _num(row.get("产量(盒)"))
            if None in (tot_l, hours, prod):
                return
            add(f"人工单位成本{tag}", Fact(tot_l / prod, _fmt(tot_l / prod), "元/盒",
                _coord(FL, "直接人工总额(元),产量(盒)", f"{product}&{rng}"), FL,
                formula=f"{_fmt(tot_l,0)} ÷ {_fmt(prod,0)}"))
            if hours:
                v = hours / prod * 10000
                add(f"本月工时" if tag == "" else f"上月工时",
                    Fact(v, _fmt(v, 2), "h/万盒",
                         _coord(FL, "总工时(小时),产量(盒)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(hours,0)} ÷ {_fmt(prod,0)} × 10000"))
                add(f"本月时薪" if tag == "" else f"上月时薪",
                    Fact(tot_l / hours, _fmt(tot_l / hours), "元/h",
                         _coord(FL, "直接人工总额(元),总工时(小时)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(tot_l,0)} ÷ {_fmt(hours,0)}"))
            if people and days:
                v = prod / (people * days)
                add(f"本月效率" if tag == "" else f"上月效率",
                    Fact(v, _fmt(v, 2), "盒/人·日",
                         _coord(FL, "产量(盒),生产人数(人),工作天数(天)", f"{product}&{rng}"), FL,
                         formula=f"{_fmt(prod,0)} ÷ ({_fmt(people,0)} × {_fmt(days,0)})"))
        lab_calc(lc_row, "", month)
        lab_calc(lp_row, "上月", fs.prev_month)
        for k in ("工时", "时薪", "效率"):
            a, b = fs.facts.get(f"本月{k}"), fs.facts.get(f"上月{k}")
            if a and b:
                v = _pct(a.value, b.value)
                if v is not None:
                    add(f"{k}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FL,
                        formula=f"({a.display} − {b.display}) ÷ {b.display}"))

    # ---- 3.3 制造费用五类 ----
    for cat, tname in MFG_TEMPLATE_NAMES.items():
        r_c = _pick(mfg, 产品名称=product, 月份=month, 费用类别=cat)
        r_p = _pick(mfg, 产品名称=product, 月份=fs.prev_month, 费用类别=cat)
        if r_c:
            v = _num(r_c.get("单位费用(元/盒)"))
            if v is not None:
                add(f"本月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{month}"), FM))
        if r_p:
            v = _num(r_p.get("单位费用(元/盒)"))
            if v is not None:
                add(f"上月{tname}", Fact(v, _fmt(v), "元/盒",
                    _coord(FM, "单位费用(元/盒)", f"{product}&{cat}&{fs.prev_month}"), FM))
        a, b = fs.facts.get(f"本月{tname}"), fs.facts.get(f"上月{tname}")
        if a and b:
            v = _pct(a.value, b.value)
            if v is not None:
                add(f"{tname}环比", Fact(v, f"{v:+.2f}%", "%", a.coord, FM,
                    formula=f"({a.display} − {b.display}) ÷ {b.display}"))
        fs.mfg.append({"类别": tname, "csv类别": cat,
                       "本月": a.display if a else "—",
                       "上月": b.display if b else "—",
                       "环比": fs.facts.get(f"{tname}环比").display if fs.facts.get(f"{tname}环比") else "—"})
    # 合计校验：五类之和应与成本汇总的「制造费用(元/盒)」一致
    if mc is not None:
        s = sum(fs.facts[f"本月{n}"].value for n in MFG_TEMPLATE_NAMES.values()
                if fs.facts.get(f"本月{n}") and fs.facts[f"本月{n}"].value is not None)
        if abs(s - mc) > 0.02:
            fs.conflicts.append(
                f"制造费用五类之和 {_fmt(s)} ≠ 成本汇总的制造费用 {_fmt(mc)}"
                f"（差 {_fmt(s-mc)}）——需人工判读是否为舍入或口径差异")

    # ---- 3.1 原材料明细 ----
    mats_c = [r for r in mat if (r.get("产品名称") or "").strip() == product
              and (r.get("月份") or "").strip() == month]
    for r in mats_c:
        name = (r.get("原材料名称") or "").strip()
        cu = _num(r.get("单位消耗成本(元/盒)"))
        rp = _pick(mat, 产品名称=product, 月份=fs.prev_month, 原材料名称=name)
        pu = _num(rp.get("单位消耗成本(元/盒)")) if rp else None
        d = _pct(cu, pu)
        fs.materials.append({
            "原材料名称": name,
            "本月": _fmt(cu), "上月": _fmt(pu),
            "环比": f"{d:+.2f}%" if d is not None else "—",
            "占比": (r.get("占总材料成本比例") or "—").strip(),
            "单价来源": "中药材市场",
        })

    # ---- 4.1 近 6 个月趋势 ----
    for r in m26:
        if (r.get("产品名称") or "").strip() != product:
            continue
        fs.monthly.append({
            "月份": r["月份"],
            "产量": (r.get("产量(盒)") or "").strip(),
            "单位材料": _fmt(_num(r.get("直接材料(元/盒)"))),
            "单位人工": _fmt(_num(r.get("直接人工(元/盒)"))),
            "单位制造费用": _fmt(_num(r.get("制造费用(元/盒)"))),
            "单位成本": _fmt(_num(r.get("单位成本(元/盒)"))),
        })
    fs.monthly.sort(key=lambda x: x["月份"])
    for i, row in enumerate(fs.monthly):
        if i == 0:
            row["环比"] = "—"
            continue
        prev_v = _num(fs.monthly[i - 1]["单位成本"])
        cur_v = _num(row["单位成本"])
        d = _pct(cur_v, prev_v)
        row["环比"] = f"{d:+.2f}%" if d is not None else "—"

    # ---- 4.3 原材料价格跟踪 ----
    for r in wx:
        nm = (r.get("药材名称") or "").strip()
        jan, jun = _num(r.get("1月价格")), _num(r.get("6月价格"))
        d = _pct(jun, jan)
        r["_涨幅"] = f"{d:+.2f}%" if d is not None else "—"
    fs.market = []  # type: ignore[attr-defined]
    for r in wx:
        nm = (r.get("药材名称") or "").strip()
        used = next((m["原材料名称"] for m in fs.materials if m["原材料名称"] == nm), None)
        jan, jun = _num(r.get("1月价格")), _num(r.get("6月价格"))
        d = _pct(jun, jan)
        fs.market.append({  # type: ignore[attr-defined]
            "原材料": nm, "单位": (r.get("单位") or "").strip(),
            "年初价": _fmt(jan, 1), "6月价": _fmt(jun, 1),
            "涨幅": f"{d:+.2f}%" if d is not None else "—",
            "市场趋势": (r.get("趋势分析") or "").strip(),
            "用于本产品": "✓" if used else "",
        })

    # ---- 5 对标（一厂 vs 二厂）----
    p2c = _pick(p2, 产品名称=product, 月份=month)
    for dim, col in (("单位成本", "单位成本(元/盒)"), ("直接材料", "直接材料(元/盒)"),
                     ("直接人工", "直接人工(元/盒)"), ("制造费用", "制造费用(元/盒)")):
        a = raw(m26, cur, col, F26, "")
        b = raw(p2, p2c, col, "中药二厂_成本汇总_2026年1-6月.csv", "")
        if a is None or b is None:
            continue
        fs.peer.append({
            "维度": dim, "中药一厂": _fmt(a), "中药二厂": _fmt(b),
            "差异": _fmt(a - b), "差异率": f"{(a-b)/b*100:+.2f}%" if b else "—",
            "方向": "一厂高" if a > b else ("二厂高" if b > a else "持平"),
        })

    # ---- 已知不可自洽点（来自 wiki 的 Disputed 登记）----
    fs.conflicts.extend([
        "官方称金银花涨「12%」，但任何可复现口径都算不出 12%（复算 5 月较 1 月 `+10.40%`）"
        "——报告若照抄会引用一个无法验证的数字",
        "银黄工艺人工定额 `1.72` vs 成本表实际 `1.48~1.55`（口径差异，非矛盾）",
    ])

    return fs


# ---------------------------------------------------------------------------

def _print(fs: FactSet) -> None:
    print("=" * 74)
    print(f"报告事实  {fs.product}  {fs.month}  （对比 {fs.prev_month} / {fs.year_ago_month}）")
    print("=" * 74)
    for k, v in fs.facts.items():
        f = f"   = {v.formula}" if v.formula else ""
        print(f"  {k:<22} {v.display:>12}{(' ' + v.unit) if v.unit else ''}{f}")
        if v.coord:
            print(f"  {'':<22} ↳ {v.coord}")
    if fs.missing:
        print("\n⚠️  数据缺失（**不是 0**，不得填零）:")
        for m in fs.missing:
            print(f"   · {m}")
    if fs.conflicts:
        print("\n⚠️  已知不可自洽点（写报告时须回避或标注）:")
        for c in fs.conflicts:
            print(f"   · {c}")


def main() -> int:
    ap = argparse.ArgumentParser(description="报告事实引擎（确定性取数）")
    ap.add_argument("--product", help="产品名")
    ap.add_argument("--month", default="2026-05", help="分析月份 YYYY-MM")
    ap.add_argument("--list-products", action="store_true")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--out", help="写入 JSON 文件")
    a = ap.parse_args()

    if a.list_products:
        for p in list_products():
            print(p)
        return 0
    if not a.product:
        ap.print_help()
        return 1

    fs = build(a.product, a.month)
    d = fs.to_dict()
    if a.out:
        Path(a.out).write_text(json.dumps(d, ensure_ascii=False, indent=2),
                               encoding="utf-8", newline="\n")
        print(f"已写出 {a.out}（{len(fs.facts)} 条事实）")
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    elif not a.out:
        _print(fs)
    return 0


if __name__ == "__main__":
    sys.exit(main())

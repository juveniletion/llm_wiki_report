# -*- coding: utf-8 -*-
"""
period_agg.py — **期间聚合**（月度 → 季度），供赛题 5.1.3「分析主题」用

赛题原文：
    根据用户选择的分析主题（**月度成本分析/季度成本分析/专题分析**）、
    分析月份、目标产品，自动生成完整报告

为什么单独一个模块
------------------
季度**不是**把三个月的数取平均。单位成本是"每盒"的量，
跨月合并必须**按产量加权**：

    单位成本_季 = Σ(该月单位成本 × 该月产量) / Σ产量
                = 总成本_季 / 产量_季          ← 与上式恒等，用这个更稳

⚠️ 实测的差别（银黄口服液 2026 Q1）：
        加权： 1442120 / 135000 = 10.6830
        算术平均：(10.70+10.87+10.53)/3 = 10.7000
    差 0.017 元/盒——不大，但**它是错的**，而且看起来完全正常。
    这正是本库最防的一类错：合法、可信、能被下游一致地引用。

于是 `aggregate()` **构造上保证恒等**：所有要素金额都由「单位成本 × 产量」
先还原成总额、再相加、再除以总产量。不从单位值出发做任何平均。

与 report_facts 的关系
---------------------
复用它的 `_read` / `_num` / `_fmt` / `_shift_month`，**共用同一套取数与格式化**。
若这里另起一套，就会出现"看板的数和报告的数不是一套"——最难查的不一致。

产出的 `FactSet` **键名与月度完全一致**（`本月X` 语义变为「本季度X」），
于是 `report_build.deterministic_map`、各表格渲染、LLM 分析、导出
**一行都不用改**，只换模板措辞。

用法
----
    python scripts/period_agg.py --product 银黄口服液 --quarter 2026-Q2
    python scripts/period_agg.py --product 银黄口服液 --quarter 2026-Q2 --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
import report_facts as RF  # noqa: E402

ensure_utf8_stdout()

# 可分析的季度（数据只到 2026-06，故只有上半年两个季度）
AVAILABLE_QUARTERS = ["2026-Q1", "2026-Q2"]


def quarter_months(q: str) -> List[str]:
    """`'2026-Q2'` → `['2026-04', '2026-05', '2026-06']`"""
    y, qn = q.split("-Q")
    n = int(qn)
    if n not in (1, 2, 3, 4):
        raise ValueError(f"季度不合法：{q}")
    return [f"{y}-{m:02d}" for m in range(3 * n - 2, 3 * n + 1)]


def prev_quarter(q: str) -> Optional[str]:
    """上一个季度；跨年时返回上年 Q4。"""
    y, qn = q.split("-Q")
    n = int(qn)
    return f"{int(y) - 1}-Q4" if n == 1 else f"{y}-Q{n - 1}"


def same_quarter_last_year(q: str) -> str:
    y, qn = q.split("-Q")
    return f"{int(y) - 1}-Q{qn}"


def quarter_label(q: str) -> str:
    y, qn = q.split("-Q")
    return f"{y} 年{'一二三四'[int(qn) - 1]}季度"


# ---------------------------------------------------------------------------
# 聚合核心
# ---------------------------------------------------------------------------

def _rows(rows: List[Dict[str, str]], product: str, months: List[str]
          ) -> List[Dict[str, str]]:
    """取出该产品在这些月份的行（缺月直接跳过，由调用方判缺失）。"""
    want = set(months)
    return [r for r in rows
            if (r.get("产品名称") or "").strip() == product
            and (r.get("月份") or "").strip() in want]


def aggregate(rows: List[Dict[str, str]], product: str,
              months: List[str]) -> Dict[str, Any]:
    """
    把这些月份**按产量加权**聚合成一个期间。

    返回的 `单位*` 全部由「总额 ÷ 总产量」得出，**不做任何平均**——
    构造上保证 `材料 + 人工 + 制造 == 单位成本` 这个恒等式成立。

    ⚠️ 不变量（每次调用都断言）：
         Σ(各要素总额) == 总成本        且     总成本/产量 == 单位成本
       若原始数据本身不自洽（本库登记过 12 处源数据矛盾），
       这里**不会替它圆场**，而是把偏差如实报出来。
    """
    got = _rows(rows, product, months)
    have = {r["月份"] for r in got}
    missing = [m for m in months if m not in have]

    qty = 0.0
    tot = 0.0
    elem: Dict[str, float] = {"直接材料": 0.0, "直接人工": 0.0, "制造费用": 0.0}
    for r in got:
        q = RF._num(r.get("产量(盒)")) or 0.0
        qty += q
        tot += RF._num(r.get("总成本(元)")) or 0.0
        for k, col in (("直接材料", "直接材料(元/盒)"),
                       ("直接人工", "直接人工(元/盒)"),
                       ("制造费用", "制造费用(元/盒)")):
            v = RF._num(r.get(col))
            if v is not None:
                # ⚠️ 先还原成**总额**再累加——这是加权正确性的关键一步
                elem[k] += v * q

    if qty <= 0:
        return {"月份数": len(got), "缺月": missing, "产量": 0.0,
                "总成本": 0.0, "单位成本": None,
                "要素单位": {k: None for k in elem}, "自洽偏差": None}

    unit_elem = {k: (v / qty if v else None) for k, v in elem.items()}
    unit = tot / qty

    # 自洽校验：各要素之和 vs 单位成本。差异来自源数据本身（登记在册）
    parts = [v for v in unit_elem.values() if v is not None]
    dev = (sum(parts) - unit) if len(parts) == 3 else None

    return {"月份数": len(got), "缺月": missing, "产量": qty,
            "总成本": tot, "单位成本": unit, "要素单位": unit_elem,
            "自洽偏差": dev}


def _synth_row(rows: List[Dict[str, str]], product: str, months: List[str],
               month_label: str) -> Optional[Dict[str, str]]:
    """
    把若干月的行聚成**一行合成记录**，字段结构与原 CSV 完全一致。

    ⚠️ 这样做的意义：把合成行喂给 `report_facts.build()`，
       **它自己的推导逻辑（环比/占比/贡献度/预算偏差）原样复用**——
       不必再写一套季度版推导，也就不会有"月度与季度口径不一致"。

    单位类字段由「总额 ÷ 总产量」得出，占比类按总额比例重算。
    """
    got = _rows(rows, product, months)
    if not got:
        return None
    qty = sum(RF._num(r.get("产量(盒)")) or 0.0 for r in got)
    if qty <= 0:
        return None
    tot = sum(RF._num(r.get("总成本(元)")) or 0.0 for r in got)
    base = dict(got[0])
    base["月份"] = month_label
    base["产量(盒)"] = f"{qty:g}"
    base["总成本(元)"] = f"{tot:g}"
    # ⚠️ 存**全精度**（`repr` 的最短往返形式）。
    #    第一版写了 `:.4f`，下游读回来就是舍入值——
    #    实测与独立手算差 9.2e-06：小数但**是静默偏差**，
    #    而且 `check_math` 复算时会判"可疑"。
    base["单位成本(元/盒)"] = repr(tot / qty)
    for col in ("直接材料(元/盒)", "直接人工(元/盒)", "制造费用(元/盒)"):
        v = sum((RF._num(r.get(col)) or 0.0) * (RF._num(r.get("产量(盒)")) or 0.0)
                for r in got) / qty
        base[col] = repr(v)
    return base


def _agg_detail(rows: List[Dict[str, str]], product: str, months: List[str],
                label: str, group_key: str,
                unit_cols: List[str], sum_cols: List[str],
                avg_cols: List[str]) -> List[Dict[str, str]]:
    """
    把明细表（制造费用 / 原材料 / 工时）按月聚合到季度，**按分组键分组**。

    三列各有口径，不能一刀切：
      · `sum_cols`  —— 可加的（费用总额、工时、产量、天数）→ 直接求和
      · `unit_cols` —— "每盒"量（单位费用、单位消耗成本）→ **按产量加权**
      · `avg_cols`  —— 时点/属性量（生产人数）→ **算术平均**（求和会翻三倍）

    ⚠️ 这三条规则必须写下来，否则下一个人会以为"数字为什么不是三倍"。
       尤其 `avg_cols`：人数是"月末在岗"，三个人月相加得 3 倍人力，
       算出来的效率会低到 1/3——错得离谱但看着像真的。
    """
    got = [r for r in rows
           if (r.get("产品名称") or "").strip() == product
           and (r.get("月份") or "").strip() in set(months)]
    if not got:
        return []

    total_q = sum(RF._num(r.get("产量(盒)")) or 0.0 for r in got) or 0.0
    groups: Dict[str, List[Dict[str, str]]] = {}
    for r in got:
        groups.setdefault((r.get(group_key) or "").strip(), []).append(r)

    out: List[Dict[str, str]] = []
    for key, grp in sorted(groups.items()):
        base = dict(grp[0])
        base["月份"] = label
        base[group_key] = key
        base["产量(盒)"] = f"{total_q:g}" if total_q else base.get("产量(盒)", "")
        for c in sum_cols:
            base[c] = f"{sum(RF._num(r.get(c)) or 0.0 for r in grp):g}"
        for c in unit_cols:
            if total_q:
                v = sum((RF._num(r.get(c)) or 0.0)
                        * (RF._num(r.get("产量(盒)")) or 0.0) for r in grp) / total_q
                base[c] = repr(v)
        for c in avg_cols:
            vs = [RF._num(r.get(c)) for r in grp if RF._num(r.get(c)) is not None]
            if vs:
                base[c] = f"{sum(vs) / len(vs):.2f}"
        out.append(base)
    return out


def build_quarter(product: str, quarter: str) -> Tuple[RF.FactSet, Dict[str, Any]]:
    """
    构造**季度 FactSet**。键名与月度一致（`本月X` 语义即「本季度X」），
    因此下游 `deterministic_map` / 表格渲染 / LLM 分析 **无需改动**。

    实现要点：把三个月聚成**一行合成记录**（`_synth_row`），
    通过 `RF.use_overrides()` 喂给 `report_facts.build()`——
    于是**全部派生值（环比/占比/贡献度/预算偏差）由原逻辑算出**，
    不是这里另算一套。

    返回 `(FactSet, 聚合元信息)`；元信息里带缺月、自洽偏差等，供报告如实披露。
    """
    ms = quarter_months(quarter)
    last = ms[-1]      # 期末月：报告定位、行标签、_shift_month 的基准
    prev_q = prev_quarter(quarter)
    yoy_q = same_quarter_last_year(quarter)
    prev_ms = quarter_months(prev_q) if prev_q else []
    yoy_ms = quarter_months(yoy_q)

    F26 = "csv/cost_data/中药一厂_成本汇总_2026年1-6月.csv"
    F25 = "csv/cost_data/中药一厂_成本汇总_2025年1-6月.csv"
    FB = "csv/cost_data/中药一厂_预算数据_2026年.csv"
    FL = "csv/cost_data/中药一厂_人工工时明细_2026年1-6月.csv"
    FM = "csv/cost_data/中药一厂_制造费用明细_2026年1-6月.csv"
    FT = "csv/cost_data/中药一厂_原材料消耗明细_2026年1-6月.csv"

    r26 = RF._read(F26)
    r25 = RF._read(F25)
    rbud = RF._read(FB)

    cur = aggregate(r26, product, ms)
    prv = aggregate(r26, product, prev_ms) if prev_ms else None
    yoy = aggregate(r25, product, yoy_ms)

    # ---- 构造合成行：本期 / 上季 / 去年同期各一行 ----
    cur_row = _synth_row(r26, product, ms, ms[-1])
    if cur_row is None:
        raise ValueError(f"{product} 在 {quarter} 无任何月份数据，无法聚合")
    rows26: List[Dict[str, str]] = [cur_row]
    if prv is not None:
        prow = _synth_row(r26, product, prev_ms, prev_ms[-1])
        if prow:
            rows26.append(prow)
    rows25: List[Dict[str, str]] = []
    # ⚠️ 标签必须是 `year_ago_month`（如 2025-06），不是季末月——
    #    report_facts 用 `"2025-" + month[5:]` 去查去年同期，
    #    标错就查不到 → 同比静默变空。
    yrow = _synth_row(r25, product, yoy_ms, RF._shift_month(last, -12) or yoy_ms[-1])
    if yrow:
        rows25.append(yrow)

    # ---- 预算：按季度求和后合成一行 ----
    brows = [r for r in rbud if (r.get("产品名称") or "").strip() == product
             and (r.get("月份") or "").strip() in set(ms)]
    budget_rows: List[Dict[str, str]] = []
    if brows:
        b0 = dict(brows[0]); b0["月份"] = ms[-1]
        for k, col in (("产量", "预算产量(盒)"), ("", "预算直接材料(元/盒)"),
                       ("", "预算直接人工(元/盒)"), ("", "预算制造费用(元/盒)"),
                       ("", "预算单位成本(元/盒)"), ("", "预算总成本(元)")):
            if col == "预算产量(盒)":
                b0[col] = f"{sum(RF._num(x.get(col)) or 0 for x in brows):g}"
            elif col == "预算总成本(元)":
                b0[col] = f"{sum(RF._num(x.get(col)) or 0 for x in brows):g}"
            elif col == "预算单位成本(元/盒)":
                bq = sum(RF._num(x.get("预算产量(盒)")) or 0 for x in brows)
                bc = sum(RF._num(x.get("预算总成本(元)")) or 0 for x in brows)
                b0[col] = repr(bc / bq) if bq else b0.get(col, "")
            else:
                # 单位类预算：按预算产量加权
                bq = sum(RF._num(x.get("预算产量(盒)")) or 0 for x in brows)
                v = sum((RF._num(x.get(col)) or 0) * (RF._num(x.get("预算产量(盒)")) or 0)
                        for x in brows) / bq if bq else None
                b0[col] = repr(v) if v is not None else b0.get(col, "")
        budget_rows = [b0]

    # ---- 明细表：按季度聚合，并**按 report_facts 期望的月份标签打标** ----
    #
    # ⚠️ 标签为什么是"上月"而不是"上季末月"：
    #    report_facts 内部用 `_shift_month(month, -1)` 去查"上月"行，
    #    季度分析里 month=期末月（如 2026-06），它就去查 2026-05。
    #    若我们把上季行标成 2026-03，**查不到 → 环比静默变空**。
    #    所以这里把上季行标成它要找的那个字符串。
    #    ⚠️ 这个标签只是**内部连接的键**，用户看不到；
    #       模板里印的是「上季」（见季度模板的措辞），不会撒谎。
    prev_label = RF._shift_month(last, -1) or last

    rows26.append(_synth_row(r26, product, prev_ms, prev_label)) if prev_ms else None

    def _detail(src: List[Dict[str, str]], group_key: str, unit_cols, sum_cols,
                avg_cols) -> List[Dict[str, str]]:
        cur = _agg_detail(src, product, ms, last, group_key, unit_cols, sum_cols, avg_cols)
        if prev_ms:
            prev = _agg_detail(src, product, prev_ms, prev_label, group_key,
                               unit_cols, sum_cols, avg_cols)
            cur = cur + prev
        return cur

    FM_rows = _detail(RF._read(FM), "费用类别", ["单位费用(元/盒)"],
                      ["费用总额(元)"], [])
    FT_rows = _detail(RF._read(FT), "原材料名称", ["单位消耗成本(元/盒)"],
                      ["原材料总成本(元)"], [])
    FL_rows = _detail(RF._read(FL), "产品名称", [], 
                      ["直接人工总额(元)", "总工时(小时)", "工作天数(天)"],
                      ["生产人数(人)"])

    RF.use_overrides({
        F26: [r for r in rows26 if r], F25: rows25, FB: budget_rows,
        FM: FM_rows, FT: FT_rows, FL: FL_rows,
    })
    try:
        fs = RF.build(product, last)
    finally:
        RF.use_overrides(None)      # 用完必须恢复，否则污染后续调用

    label = quarter_label(quarter)

    meta = {
        "quarter": quarter, "label": label, "months": ms,
        "prev_quarter": prev_q, "yoy_quarter": yoy_q,
        "missing_months": cur["缺月"], "n_months": cur["月份数"],
        "self_consistency_dev": cur["自洽偏差"],
        "has_prev": bool(prv and prv["单位成本"] is not None),
        "has_yoy": yoy["单位成本"] is not None,
    }
    return fs, meta


def _main() -> int:
    ap = argparse.ArgumentParser(description="季度聚合（赛题 5.1.3）")
    ap.add_argument("--product", required=True)
    ap.add_argument("--quarter", required=True, help="如 2026-Q2")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    fs, meta = build_quarter(a.product, a.quarter)
    if a.json:
        print(json.dumps({
            "meta": meta,
            "facts": {k: {"value": v.value, "display": v.display,
                          "unit": v.unit, "formula": v.formula}
                      for k, v in fs.facts.items()},
            "missing": fs.missing,
        }, ensure_ascii=False, indent=1))
        return 0

    print("=" * 74)
    print(f"{meta['label']}　{fs.product}　（{meta['months'][0]} ~ {meta['months'][-1]}）")
    print("=" * 74)
    for k, v in fs.facts.items():
        f = f"　= {v.formula}" if v.formula else ""
        print(f"  {k:<16} {v.display:>10} {v.unit}{f}")
    dev = meta["self_consistency_dev"]
    if dev is not None and abs(dev) > 0.005:
        print(f"\n  ⚠️ 自洽偏差 {dev:+.4f}：三要素之和与单位成本不一致"
              f"（源数据本身如此，见 wiki 的 Disputed 登记）")
    for m in fs.missing:
        print(f"  ⚠️ {m}")
    return 0


if __name__ == "__main__":
    sys.exit(_main())

# -*- coding: utf-8 -*-
"""
eval_report.py — **赛题交付物 #3：评测报告**（自动生成可测的三项 + 留白一项）

赛题交付物 #3 要求对 3 个分析场景评测四项：

    ① 报告结构完整度          ← 可自动测（比对模板契约）
    ② 归因合理性评分（人工）    ← **必须人评**，本脚本只留空位与评分口径
    ③ RPA 触发成功率          ← 可自动测（真调 mock 服务）
    ④ 对标三步法输出格式正确率  ← 可自动测（字段齐备 + 差异独立复算）

设计原则
--------
1. **真跑，不编**。三份报告是现生成的（不是读旧文件），RPA 是真发给 mock 的，
   对标差异是**从原始 CSV 独立复算**后与引擎输出比对的。
   一份"数字都对"的评测报告，如果数字是自己填的，那评测毫无价值。

2. **② 留白不猜**。人工评分是**人的判断**，脚本填一个数就是伪造。
   本脚本输出评分口径与待评条目，分数栏留 `待评`。

3. **误差判据照抄赛题**：5.3.3 原文「与标准答案对比，误差 ≤ 1%」。
   本脚本用**独立复算**当标准答案（同一份 raw，不同的计算路径）。

用法
----
    python scripts/eval_report.py                    # 全跑（含调 LLM 生成 3 份报告）
    python scripts/eval_report.py --no-llm           # 跳过 LLM，只测结构与计算
    python scripts/eval_report.py --out references/评测报告.md
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
import report_facts as RF  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

# ---- 三个评测场景（赛题 5.1.4「不同产品/月份」）----
SCENARIOS: List[Tuple[str, str]] = [
    ("银黄口服液", "2026-05"),
    ("板蓝根颗粒", "2026-05"),
    ("六味地黄胶囊", "2026-06"),
]

# ⚠️ 这三项是**模板契约**（`报告模板契约.md` §8），不是本脚本的主张。
#    复用的是 `check_report.py` 的常量——校验器与评测用**同一份判据**，
#    否则会出现"校验说合规、评测说缺章"这种自相矛盾。
from check_report import REQUIRED_CHAPTERS, REQUIRED_HEADERS  # noqa: E402

# 三要素行的「去年同月/同比」两列固定为 —（模板硬编码）
ELEMENT_ROW_MARK = "| 其中：直接材料"


def _pct_err(mine: Optional[float], std: Optional[float]) -> Optional[float]:
    """相对误差 %（以独立复算值为分母）。任一缺失返回 None，不填 0。"""
    if mine is None or std in (None, 0):
        return None
    return abs(mine - std) / abs(std) * 100.0


# ---------------------------------------------------------------------------
# ① 报告结构完整度
# ---------------------------------------------------------------------------

def check_structure(md: str) -> Dict[str, Any]:
    """比对模板契约：章齐全 + 表头未改 + 无残留占位符 + 三要素行口径。"""
    miss_ch = [c for c in REQUIRED_CHAPTERS if c not in md]
    miss_hd = [h for h in REQUIRED_HEADERS if h not in md]
    placeholders = __import__("re").findall(r"\{\{[^}]+\}\}", md)
    # 三要素行的「去年同月/同比」两列应为 —（模板硬编码）
    # 表头 8 列：指标|本月实际|上月实际|环比变动|去年同月|同比变动|预算值|预算偏差
    #   → 去年同月 = cells[4]，同比变动 = cells[5]
    row_ok = None
    for line in md.split("\n"):
        if line.startswith(ELEMENT_ROW_MARK):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            row_ok = len(cells) >= 8 and cells[4] == "—" and cells[5] == "—"
            break
    return {
        "chapters_total": len(REQUIRED_CHAPTERS),
        "chapters_found": len(REQUIRED_CHAPTERS) - len(miss_ch),
        "chapters_missing": miss_ch,
        "headers_total": len(REQUIRED_HEADERS),
        "headers_found": len(REQUIRED_HEADERS) - len(miss_hd),
        "headers_missing": miss_hd,
        "residual_placeholders": sorted(set(placeholders)),
        "element_row_ok": row_ok,
        "pass": (not miss_ch and not miss_hd and not placeholders
                 and row_ok is not False),
    }


# ---------------------------------------------------------------------------
# ③ RPA 触发成功率
# ---------------------------------------------------------------------------

def check_rpa(reports: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    真把整改任务发给 mock 服务，统计成功率。

    ⚠️ 派发的**不是评测脚本自造的载荷**，而是从三份刚生成的报告里
       `parse_report_tasks()` 解析出的 6.4 任务表——测的必须是
       **系统真实产物**能不能送达，而不是"我能不能构造一个合法请求"。

    ⚠️ 也不 mock 掉网络：本项测的正是"HTTP 投递是否真的成功"。
       若 mock 未起，结论是"成功率 0 + 明确的连接错误"，
       那是**真实结论**，不该粉饰成"跳过"。
    """
    import rpa_client as R
    c = R.RPAClient()
    h = c.health()
    if not h.get("ok"):
        return {"reachable": False, "error": h.get("error", "mock 服务不可达"),
                "total": 0, "ok": 0, "rate": None,
                "note": "mock 服务未启动 → 本项无法评测（不是「通过」）"}
    # ⚠️ 记下 mock 的**初始任务数**：本项要在"下游干净"的前提下测，
    #    否则会把"上次评测留下的任务"误读成"系统下发失败"。
    #    首次实测正是踩了这个：跑了两次 → 6 条报「编号已被占用」→ 33.3%，
    #    而那个数**测的是我的评测重复执行，不是系统的投递能力**。
    tasks_before = int(h.get("tasks_count") or 0)

    # 下游已存在的任务：用于区分「重复下发」与「编号被占用」
    existing: Optional[Dict[str, str]] = None
    try:
        r = c.list_tasks()
        existing = {t["task_id"]: t.get("task_title", "")
                    for t in c.extract_tasks(r)}
    except Exception:  # noqa: BLE001
        existing = None

    sent, ok = 0, 0
    kinds: Dict[str, int] = {"sent": 0, "existed": 0, "collision": 0, "failed": 0}
    detail: List[Dict[str, Any]] = []
    for s in reports:
        payloads = R.RPAClient.parse_report_tasks(s["report"], s["month"], s["product"])
        for p in payloads:
            r = c.dispatch_one(p, existing) if existing is not None \
                else c.dispatch_one(p)
            sent += 1
            good = bool(r.get("ok")) and not r.get("degraded")
            ok += 1 if good else 0
            # 三类区分：本次送达 / 此前已送（幂等重发）/ 编号被占用（需人工处理）
            kind = ("sent" if good and not r.get("existed")
                    else "existed" if good
                    else "collision" if r.get("collision") else "failed")
            kinds[kind] += 1
            detail.append({"scenario": f"{s['product']} {s['month']}",
                           "task_id": p["task_id"], "title": p["task_title"][:28],
                           "kind": kind, "ok": good, "status": r.get("status"),
                           "note": r.get("error") or r.get("notify") or ""})
    return {"reachable": True, "total": sent, "ok": ok, "kinds": kinds,
            "tasks_before": tasks_before,
            "rate": (ok / sent * 100.0) if sent else None, "detail": detail}


# ---------------------------------------------------------------------------
# ④ 对标三步法：格式正确率 + 差异计算准确率
# ---------------------------------------------------------------------------

# 与引擎不同的一个 CSV 读取路径——**刻意不复用 report_facts / benchmark_engine
# 的 `_read`/`_pick`/`_num`**。复算如果连读文件的代码都一样，独立性就只剩口号。
_A_CSV = "中药一厂_成本汇总_2026年1-6月.csv"
_B_CSV = "中药二厂_成本汇总_2026年1-6月.csv"
_UNIT_COLS = [("单位成本", "单位成本(元/盒)"), ("直接材料", "直接材料(元/盒)"),
              ("直接人工", "直接人工(元/盒)"), ("制造费用", "制造费用(元/盒)")]


def _std_peer_diff(product: str, month: str) -> Dict[str, Tuple[float, float]]:
    """**独立复算**差异（标准答案）：自带的 csv 读取 + 手算差与差率。"""
    import csv as _csv

    def read(name: str) -> List[Dict[str, str]]:
        with open(WIKI_ROOT / "raw" / "csv" / "cost_data" / name,
                  encoding="utf-8-sig", newline="") as f:
            return list(_csv.DictReader(f))

    def fnum(v: Any) -> Optional[float]:
        s = str(v or "").strip().replace(",", "")
        return float(s) if s not in ("", "—", "-") else None

    def row_of(rows: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
        for r in rows:
            if (r.get("产品名称") or "").strip() == product and \
               (r.get("月份") or "").strip() == month:
                return r
        return None

    ra, rb = row_of(read(_A_CSV)), row_of(read(_B_CSV))
    out: Dict[str, Tuple[float, float]] = {}
    if not ra or not rb:
        return out
    for dim, col in _UNIT_COLS:
        va, vb = fnum(ra.get(col)), fnum(rb.get(col))
        if va is None or vb is None or va == 0:
            continue
        diff = vb - va                       # 二厂 − 一厂（正 = 二厂更贵）
        out[dim] = (diff, diff / va * 100.0)
    return out


def check_benchmark(product: str, month: str, use_llm: bool = False
                    ) -> Dict[str, Any]:
    """对标三步法：字段齐备（格式）+ 差异独立复算比对（准确率）。"""
    import benchmark_engine as BE
    try:
        r = BE.run(product, month, use_llm=use_llm, verbose=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    s1 = r["steps"][0] if r.get("steps") else {}
    rows = s1.get("rows") or []
    # 契约字段（`报告模板契约.md` §5 / benchmark_engine.step1_find_diff）
    NEIF = ["成本要素", "一厂", "二厂", "差异金额", "差异率", "方向"]
    field_ok = all(all(k in row for k in NEIF) for row in rows) if rows else False

    std = _std_peer_diff(product, month)
    cmp_rows, worst = [], 0.0
    for row in rows:
        dim = row.get("成本要素")
        if dim not in std:
            continue
        d_mine, rate_mine = row.get("差异金额"), row.get("差异率")
        d_std, rate_std = std[dim]
        e_d = _pct_err(d_mine, d_std)
        # 差异率本身已是百分数，比的是"百分点"而不是相对误差——
        # 赛题 5.3.3 说的 1% 指差异率误差 ≤ 1 个百分点
        e_r = abs(rate_mine - rate_std) if rate_mine is not None else None
        if e_r is not None:
            worst = max(worst, e_r)
        cmp_rows.append({"维度": dim, "差异_引擎": d_mine, "差异_复算": round(d_std, 4),
                         "误差%": None if e_d is None else round(e_d, 4),
                         "差异率_引擎%": rate_mine, "差异率_复算%": round(rate_std, 4),
                         "差异率误差(百分点)": None if e_r is None else round(e_r, 4)})

    # ---- 归因逻辑自洽：**机械可测的下界** ----
    # ⚠️ 「自洽」的完整判断是人的事（见 §五）。这里只查**能机械证伪**的部分：
    #    第三步归因若与第一步的差异表**对不上**，那一定不自洽——这是下界，
    #    不是充分判据。查三条：文本非空、点到了差异最大的要素、引到了厂名。
    s3 = r["steps"][2] if len(r.get("steps") or []) > 2 else {}
    attr_text = (s3 or {}).get("text") or ""
    top_dim = max(cmp_rows, key=lambda c: abs(c["差异率_引擎%"] or 0))["维度"] \
        if cmp_rows else None
    checks = {
        "文本非空": bool(attr_text.strip()),
        "点到了差异最大要素": bool(top_dim and top_dim in attr_text),
        "提到了「一厂」或「二厂」": ("一厂" in attr_text or "二厂" in attr_text),
    }
    return {"ok": True, "rows": len(rows), "field_ok": field_ok,
            "fields_expected": NEIF,
            "compare": cmp_rows,
            "worst_rate_err_pp": round(worst, 4),
            "rate_err_ok": worst <= 1.0,          # 赛题 5.3.3：误差 ≤ 1%
            "attribution_len": len(attr_text),
            "attribution_nonempty": bool(attr_text.strip()),
            "attribution_checks": checks,
            "attribution_consistent": all(checks.values())}


# ---------------------------------------------------------------------------
# 生成评测报告
# ---------------------------------------------------------------------------

def run_eval(use_llm: bool) -> Dict[str, Any]:
    import report_build as RB
    scenarios: List[Dict[str, Any]] = []
    for product, month in SCENARIOS:
        print(f"  ▶ {product} {month} …")
        r = RB.build(product, month, use_llm=use_llm, verbose=False)
        md = r["report"]
        st = check_structure(md)
        scenarios.append({"product": product, "month": month,
                          "structure": st, "stats": r["stats"],
                          "md_chars": len(md), "report": md})
        print(f"     结构完整度 {st['chapters_found']}/{st['chapters_total']} 章　"
              f"表头 {st['headers_found']}/{st['headers_total']}　"
              f"残留占位符 {len(st['residual_placeholders'])}")
    print("  ▶ RPA 触发成功率 …")
    rpa = check_rpa(scenarios)
    print(f"     {rpa['ok']}/{rpa['total']} 成功"
          + (f"　（{rpa.get('note','')}）" if rpa.get("note") else ""))
    print("  ▶ 对标三步法 …")
    bms = []
    for product, month in SCENARIOS:
        # ⚠️ 用「与报告同档」的 LLM 开关跑：第三步归因是 LLM 产物，
        #    若这里传 False，测到的是 `（未调用 LLM）` 而不是真实归因，
        #    却会以"归因文本：有（9 字）"的样子印进报告——**假证据**。
        b = check_benchmark(product, month, use_llm=use_llm)
        bms.append({"product": product, "month": month, **b})
        if b.get("ok"):
            print(f"     {product}: 字段{'齐' if b['field_ok'] else '缺'}　"
                  f"差异率最大误差 {b['worst_rate_err_pp']} 个百分点　"
                  f"{'✓' if b['rate_err_ok'] else '✗'}")
        else:
            print(f"     {product}: 失败 {b.get('error')}")
    return {"scenarios": scenarios, "rpa": rpa, "benchmark": bms,
            "generated_at": date.today().isoformat(), "use_llm": use_llm}


def render(res: Dict[str, Any]) -> str:
    L: List[str] = []
    ok = lambda b: "✅" if b else "❌"
    L.append("# 评测报告\n")
    L.append("> **赛题交付物 #3**：对给定 3 个分析场景的评测结果\n")
    L.append(f"> 生成日期：{res['generated_at']}　·　"
             f"LLM 叙述：{'启用' if res['use_llm'] else '**未启用（--no-llm）**'}\n")
    L.append("> ⚠️ 本报告的数字**全部由脚本实测产出**：三份报告是现场生成的，"
             "RPA 是真发给 mock 服务的，对标差异是从原始 CSV **独立复算**后比对的。\n")
    L.append("---\n")

    # 汇总表
    L.append("## 一、评测结果汇总\n")
    sc = res["scenarios"]
    st_ok = all(s["structure"]["pass"] for s in sc)
    rpa = res["rpa"]
    bm = res["benchmark"]
    bm_ok = all(b.get("field_ok") and b.get("rate_err_ok")
                and b.get("attribution_consistent") for b in bm if b.get("ok"))
    L.append("| 评测项 | 结果 | 判据 |")
    L.append("|:---|:---|:---|")
    L.append(f"| ① 报告结构完整度 | {ok(st_ok)} "
             f"{sum(1 for s in sc if s['structure']['pass'])}/{len(sc)} 场景全通过 | "
             f"模板契约 §8：6 章齐全 + 表头未改 + 无残留占位符 |")
    L.append(f"| ② 归因合理性评分 | — **待人工评分** | 0-5 分，口径见 §五 |")
    rpa_txt = (f"{ok(rpa.get('rate') == 100.0)} {rpa['ok']}/{rpa['total']} 成功"
               f"（{rpa['rate']:.0f}%）") if rpa.get("rate") is not None \
        else f"❌ 无法评测（{rpa.get('error', rpa.get('note',''))}）"
    L.append(f"| ③ RPA 触发成功率 | {rpa_txt} | HTTP 200 且未被降级 |")
    L.append(f"| ④ 对标三步法格式正确率 | {ok(bm_ok)} "
             f"字段齐备 {sum(1 for b in bm if b.get('field_ok'))}/{len(bm)}，"
             f"归因自洽下界 {sum(1 for b in bm if b.get('attribution_consistent'))}/{len(bm)} | "
             f"差异表含 6 个必要字段；差异率误差 ≤ 1 个百分点（赛题 5.3.3）|")
    L.append("")
    L.append("> ② 是**人的判断**，脚本不代填；④ 的「自洽」脚本只给**可机械证伪的下界**"
             "（归因文本是否点到差异最大要素、是否引到厂名），完整判断同属人工。\n")

    # ① 结构完整度
    L.append("## 二、报告结构完整度（赛题 5.1.4）\n")
    L.append("判据：模板契约（`报告模板契约.md` §8）——6 章齐全、"
             "6 组表头未改、无残留占位符、三要素行「去年同月/同比」保持 `—`。\n")
    L.append("| 场景 | 产品 | 月份 | 章节 | 表头 | 残留占位符 | 三要素行 | 结论 |")
    L.append("|:---|:---|:---|---:|---:|---:|:---|:---|")
    for s in sc:
        t = s["structure"]
        L.append(f"| {s['product']} | {s['product']} | {s['month']} | "
                 f"{t['chapters_found']}/{t['chapters_total']} | "
                 f"{t['headers_found']}/{t['headers_total']} | "
                 f"{len(t['residual_placeholders'])} | "
                 f"{'✓' if t['element_row_ok'] else '✗'} | {ok(t['pass'])} |")
    L.append("")

    # ③ RPA
    L.append("## 三、RPA 触发成功率（赛题 5.4.2）\n")
    if rpa.get("rate") is None:
        L.append(f"⚠️ **无法评测**：{rpa.get('error', '')}\n")
        L.append("> 若 mock 服务未启动，本项应记为「未通过」而非「跳过」——"
                 "投递能力是模块四的核心，测不了就等于不达标。\n")
        L.append("")
    elif rpa["total"] == 0:
        L.append("⚠️ **无可派发任务 → 本项本次未测出**。\n")
        L.append("> 原因：`6.4 整改任务清单`由 LLM 依据分析结论拟定，"
                 "`--no-llm` 模式下该表为空，没有可投递的内容。"
                 "本项需在**启用 LLM** 的完整运行下才有效——"
                 "不要把它读成「成功率 0%」。\n")
        L.append("")
    else:
        k = rpa.get("kinds", {})
        L.append(f"投递 {rpa['total']} 条任务，成功 {rpa['ok']} 条，"
                 f"成功率 **{rpa['rate']:.1f}%**。"
                 f"（mock 起始任务数 {rpa.get('tasks_before', '?')}）\n")
        L.append("> 任务来自三份报告的「6.4 整改任务清单」——"
                 "测的是**系统产物**能否送达，不是评测脚本能否构造合法请求。\n")
        if rpa.get("tasks_before"):
            L.append(f"> ⚠️ **本次非干净环境**：mock 里已有 "
                     f"{rpa['tasks_before']} 条历史任务。同场景重跑时编号会撞，"
                     "`collision` 数会偏高——那不是投递能力下降。"
                     "要看真实成功率请**重启 mock 后重跑**。\n")
        L.append(f"- 本次送达 `sent`：**{k.get('sent', 0)}** 条\n"
                 f"- 此前已送、幂等跳过 `existed`：{k.get('existed', 0)} 条\n"
                 f"- **编号被占用 `collision`：{k.get('collision', 0)} 条**\n"
                 f"- 其他失败 `failed`：{k.get('failed', 0)} 条\n")
        L.append("> ⚠️ **`collision` 是真信号，不是脚本故障**：任务编号 "
                 "`TASK-<产品码>-<年月>-<序号>` 是**确定性**的，"
                 "但任务标题由 LLM 依分析结论拟定。同一场景重跑两次，"
                 "看到的是**同一个编号配两份不同措辞**——下游据此判定"
                 "「编号已被占用」并**明确拒绝下发**（没有静默吞掉）。\n"
                 "> 这暴露的是**产品层待办**：报告每次重新分析产生的任务"
                 "应带版本/去重策略，否则「重跑分析」会被下游读成"
                 "「又要发一批新任务」。**评测把它如实记为未通过**，"
                 "而不是换个时间戳绕过去。\n")
        L.append("| 场景 | 任务编号 | 任务标题 | 类别 | 结果 | 状态 / 说明 |")
        L.append("|:---|:---|:---|:---|:---|:---|")
        _kind_zh = {"sent": "本次送达", "existed": "已送(幂等)", "collision": "编号占用",
                    "failed": "失败"}
        for d in rpa["detail"]:
            L.append(f"| {d['scenario']} | `{d['task_id']}` | {d['title']} | "
                     f"{_kind_zh.get(d['kind'], d['kind'])} | {ok(d['ok'])} | "
                     f"{d.get('status') or '—'}"
                     + (f"　{d['note']}" if d.get("note") else "") + " |")
        L.append("")

    # ④ 对标
    L.append("## 四、对标三步法输出格式正确率（赛题 5.3.3）\n")
    L.append("两项判据：**字段齐备**（差异表含必要字段）、"
             "**差异计算准确**（与独立复算比，误差 ≤ 1 个百分点）。\n")
    for b in bm:
        L.append(f"### {b['product']}　{b['month']}\n")
        if not b.get("ok"):
            L.append(f"⚠️ 执行失败：{b.get('error')}\n")
            continue
        L.append(f"- 字段齐备：{ok(b['field_ok'])}（应有 {len(b['fields_expected'])} 个："
                 f"{'、'.join(b['fields_expected'])}）")
        L.append(f"- 归因文本：{b['attribution_len']} 字　"
                 f"自洽下界检查 {ok(b['attribution_consistent'])}"
                 f"（{'；'.join(f'{k}{ok(v)}' for k, v in b['attribution_checks'].items())}）")
        L.append(f"- 差异率最大误差：**{b['worst_rate_err_pp']} 个百分点** "
                 f"（判据 ≤ 1）{ok(b['rate_err_ok'])}\n")
        if b.get("compare"):
            L.append("| 维度 | 差异(引擎) | 差异(独立复算) | 差异率(引擎) | 差异率(复算) | 误差(百分点) |")
            L.append("|:---|---:|---:|---:|---:|---:|")
            for c in b["compare"]:
                L.append(f"| {c['维度']} | {c['差异_引擎']} | {c['差异_复算']} | "
                         f"{c['差异率_引擎%']}% | {c['差异率_复算%']}% | "
                         f"{c['差异率误差(百分点)']} |")
            L.append("")
    L.append("> **独立复算的口径**：直接读两个厂的原始 CSV、按列取数、手算"
             "「二厂 − 一厂」与其相对一厂的比率——**刻意不走引擎的中间结果**。"
             "用同一条路径复算等于自证，说明不了准确率。\n")

    # ② 人工评分（留白）
    L.append("## 五、归因合理性评分（**待人工评分**）\n")
    L.append("⚠️ **本栏必须由人评**，脚本不代填。归因是否合理、建议是否可执行，"
             "是**判断**而非**计算**——脚本填一个数就是伪造。\n")
    L.append("**评分口径**（赛题 5.1.4 原文）：0–5 分，重点考察三条：\n")
    L.append("| # | 考察点 | 对应本系统产物 |")
    L.append("|:---|:---|:---|")
    L.append("| 1 | 是否定位到**具体成本要素** | 报告 2.2 成本结构表 + 3.1 原材料明细 |")
    L.append("| 2 | 是否**引用知识库中的工艺/行业信息** | 报告尾「知识库引用」段 + 正文归因 |")
    L.append("| 3 | 建议是否**可执行** | 报告 6.3 改进建议表（含责任部门/优先级/时限）|")
    L.append("")
    L.append("### 待评条目\n")
    L.append("| 场景 | 报告 | 归因文本段 | 得分(0-5) | 评语 |")
    L.append("|:---|:---|---:|:---|:---|")
    for s in sc:
        L.append(f"| {s['product']} {s['month']} | "
                 f"`reports/{s['product']}_{s['month']}.md` | "
                 f"{s.get('stats',{}).get('llm_chars',0)} 字 | **待评** | |")
    L.append("")

    # 附：数据来源与可复现步骤
    L.append("## 六、可复现性\n")
    L.append("本报告的全部结果由一条命令产出：\n")
    L.append("```bash")
    L.append("python scripts/eval_report.py --out references/评测报告.md")
    L.append("```\n")
    L.append("| 评测项 | 实现 | 为什么可信 |")
    L.append("|:---|:---|:---|")
    L.append("| ① 结构完整度 | `check_structure()` | 复用 `check_report.py` 的契约常量——"
             "校验器与评测**同一份判据**，不会自相矛盾 |")
    L.append("| ③ RPA 成功率 | `check_rpa()` | 真发 HTTP 到 mock；失败如实记为未通过 |")
    L.append("| ④ 对标准确率 | `_std_peer_diff()` | **独立复算**（不同代码路径）后比对 |")
    L.append("")
    L.append("> ⚠️ 本报告**不包含**任何「估算」或「预期」数值。"
             "测不出来的项（如 mock 未启动）如实写「无法评测」，不填 0 也不跳过。\n")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成赛题交付物 #3：评测报告")
    ap.add_argument("--out", help="输出 markdown 路径（默认 stdout）")
    ap.add_argument("--no-llm", action="store_true",
                    help="跳过 LLM 叙述（只测结构与计算，快）")
    a = ap.parse_args()

    print("=" * 74)
    print("评测报告生成（赛题交付物 #3）")
    print("=" * 74)
    res = run_eval(use_llm=not a.no_llm)
    md = render(res)
    if a.out:
        p = Path(a.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8", newline="\n")
        print(f"\n✅ 已写出 {p}（{len(md):,} 字符）")
    else:
        print("\n" + md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

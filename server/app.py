# -*- coding: utf-8 -*-
"""
app.py — 制药成本分析**看板后端**（FastAPI，只读）

设计原则
--------
1. **只读**。看板不修改任何知识库内容——它消费 wiki/state/reports。
   要改数据，走摄入流程（`ingest_agent.py`），不从这里点按钮。
2. **复用，不重造**。数据层直接 import 现成模块：
     数字/趋势 → `scripts/report_facts.py`（确定性取数，已带溯源坐标）
     数值快照 → `scripts/state.py`
     报告     → `reports/*.md`
   后端**自己不算任何数**——算数只有一处，就是 report_facts。
3. **派生视图不落地**。看板数据每次实时读文件，不建缓存的"第二真相"。

端点
----
    GET /api/health                      健康检查
    GET /api/products                    产品列表
    GET /api/overview?month=2026-05      三产品关键指标
    GET /api/trend?product=银黄口服液     逐月成本趋势
    GET /api/conflicts                   全库 Status 块（数据源自相矛盾处）
    GET /api/timeline                    摄入活动时间线（来自 log.md）
    GET /api/reports                     已生成的报告清单
    GET /api/report/{product}/{month}    单份报告 markdown
    GET /api/search?q=...                混合检索（BM25+向量+RRF）

运行
----
    python server/app.py                 # 默认 127.0.0.1:8765
    python -m uvicorn app:app --reload   # 在 server/ 目录下开发
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Cookie, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
sys.path.insert(0, str(WIKI_ROOT / "scripts"))

import report_facts as RF  # noqa: E402
from errors import AgentError, ConfigError, DataMissing  # noqa: E402

app = FastAPI(title="制药成本分析看板 API", version="1.0.0",
              description="只读。数据来自 llm-wiki 知识库（证据可溯）。")


# ---------------------------------------------------------------------------
# 统一错误响应
# ---------------------------------------------------------------------------
# ⚠️ 为什么必须有这一段（本库踩过的真实事故）：
#
#   数据缺失曾经用 `raise SystemExit(...)` 报。`SystemExit` 继承 **BaseException**，
#   而本文件所有兜底都是 `except Exception`（见 chat / search / decide / export
#   等处）——**一处都拦不住**。
#
#   后果不是"报错"而是**请求直接断连**：HTTP 层没有响应，浏览器只看到
#   connection reset，前端把"缺个 CSV"显示成"网络错误"，
#   排查时看不出是哪个请求、缺哪个文件。
#
#   ⇒ 可预期的失败（缺数据 / 缺配置）必须给出**结构化 JSON + 明确的下一步**。

@app.exception_handler(DataMissing)
async def _on_data_missing(_req: Request, exc: DataMissing) -> JSONResponse:
    # 503 而非 500：这是"暂时取不到数"，不是"服务写错了"。
    return JSONResponse(status_code=503, content=exc.as_dict())


@app.exception_handler(ConfigError)
async def _on_config_error(_req: Request, exc: ConfigError) -> JSONResponse:
    # 501 语义上不贴切，但这里要的是"能区分"：缺 Key 与缺数据是两回事，
    # 排查动作完全不同（配 .env vs 放数据），状态码分开更好定位。
    return JSONResponse(status_code=501, content=exc.as_dict())


@app.exception_handler(AgentError)
async def _on_agent_error(_req: Request, exc: AgentError) -> JSONResponse:
    return JSONResponse(status_code=500, content=exc.as_dict())

STATIC = HERE / "static"
WIKI = WIKI_ROOT / "wiki"
REPORTS = WIKI_ROOT / "reports"


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

@app.get("/api/health", summary="健康检查与知识库规模")
def health() -> Dict[str, Any]:
    """看板与知识库的连通性——同时暴露知识库规模，便于排查"数据为空"。"""
    n_wiki = len([p for p in WIKI.rglob("*.md")
                  if p.name not in ("index.md", "log.md")])
    raw_dir = WIKI_ROOT / "raw"
    raw_files = [p for p in raw_dir.rglob("*")
                 if p.is_file() and not p.name.startswith(".")]

    # **原始资料数 ≠ 文件数**。
    # PDF/Word/Excel 会各生成一个同名 `.txt` 派生缓存（供检索用）。
    # 对用户说"基于 N 份原始资料"时**必须排除**它们，否则是虚报。
    # 判据：某 .txt 与一个同名二进制件（.pdf/.docx/.xlsx/.xls）并存 → 它是派生缓存。
    BINARY = {".pdf", ".docx", ".xlsx", ".xls", ".doc"}
    sources = [
        p for p in raw_files
        if not (p.suffix.lower() == ".txt"
                and any(p.with_suffix(e).exists() for e in BINARY))
    ]
    return {
        "ok": True,
        "articles": n_wiki,
        "sources": len(sources),          # 用户视角：多少份原始资料
        "raw_files": len(raw_files),
        "reports": len(list(REPORTS.glob("*.md"))) if REPORTS.exists() else 0,
    }


@app.get("/api/products", summary="产品列表")
def products() -> Dict[str, Any]:
    return {"products": RF.list_products()}


# ---------------------------------------------------------------------------
# 关键指标 / 趋势
# ---------------------------------------------------------------------------

# 看板关心的指标（report_facts 每条都带坐标）
#
# ⚠️ 上月值也要暴露：**瀑布图**要展示"上月 → 本月"的逐要素拆解，
#    只有本月值画不出瀑布。这些量 report_facts 里本来就有，只是原先没往外给。
OVERVIEW_KEYS = [
    "本月单位成本", "上月单位成本",
    "单位成本环比", "单位成本同比", "单位成本预算偏差",
    "本月材料成本", "上月材料成本",
    "本月人工成本", "上月人工成本",
    "本月制造成本", "上月制造成本",
    "材料占比", "人工占比", "制造费用占比",
    "材料贡献度", "人工贡献度", "制造费用贡献度",
]


def _overview_one(product: str, month: str) -> Dict[str, Any]:
    fs = RF.build(product, month)
    out: Dict[str, Any] = {"product": product, "month": month, "facts": {}, "coords": {}}
    for k in OVERVIEW_KEYS:
        f = fs.facts.get(k)
        if f is None:
            continue
        out["facts"][k] = f.value
        out["coords"][k] = f.coord
        out.setdefault("display", {})[k] = f.display
    out["missing"] = fs.missing
    return out


@app.get("/api/overview", summary="三产品关键指标")
def overview(month: str = Query("2026-05", description="分析月份 YYYY-MM")) -> Dict[str, Any]:
    return {"month": month,
            "items": [_overview_one(p, month) for p in RF.list_products()]}


@app.get("/api/trend", summary="逐月成本趋势")
def trend(product: str = Query(...), months: int = Query(6, ge=1, le=24)) -> Dict[str, Any]:
    """逐月单位成本趋势。直接来自 report_facts 的 monthly（已算好环比）。"""
    fs = RF.build(product, "2026-05")
    rows = fs.monthly[-months:]
    return {"product": product,
            "series": [
                {"月份": r["月份"],
                 "单位成本": RF._num(r["单位成本"]),
                 "单位材料": RF._num(r["单位材料"]),
                 "单位人工": RF._num(r["单位人工"]),
                 "单位制造费用": RF._num(r["单位制造费用"]),
                 "产量": RF._num(r["产量"]),
                 "环比": r["环比"]}
                for r in rows]}


@app.get("/api/forecast", summary="成本趋势预测（加分项）")
def forecast_api(product: str = Query(...), horizon: int = Query(2, ge=1, le=6),
                 authorization: str = Header(default=""),
                 lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    下月/未来数月单位成本预测（Holt 线性趋势）。

    ⚠️ **预测值不是"事实"**：它没有 raw 坐标、`check_math` 复算不了它。
       所以本接口的返回里**显式带 `warnings` 与 `trustworthy`**，
       前端必须把它们显示出来——一个孤零零的"下月 11.17 元/盒"
       会被当成承诺读，而它不是。

    本端点只读，不需要登录（与其余看板数据一致）。
    """
    import forecast as FC
    fc = FC.forecast(product, horizon)
    return {
        "product": fc.product,
        "n_samples": fc.n_samples,
        "rmse": round(fc.rmse, 4),
        "alpha": fc.alpha, "beta": fc.beta,
        "trustworthy": fc.trustworthy,
        "history": [{"月份": m, "单位成本": v} for m, v in fc.history],
        "predictions": fc.predictions,
        "warnings": fc.warnings,
        # 让前端不必自己拼这句话——口径统一由服务端给
        "method": "Holt 线性趋势（非季节性模型）",
    }


# ---------------------------------------------------------------------------
# 冲突登记表 —— 解析全库 Status 块
# ---------------------------------------------------------------------------

_STATUS_RE = re.compile(r"^>\s*\*\*Status:\s*(Disputed|Outdated|Update)\*\*(.*)$")


def _parse_conflicts() -> List[Dict[str, Any]]:
    """
    从 wiki 词条里抽 `> **Status: Disputed**` 块。

    ⚠️ 只抽**块**，不抽行——一个 Status 块由连续的 `>` 行组成，
    必须整块取出来才有"信源A / 信源B / 工程判定"的完整信息。
    """
    out: List[Dict[str, Any]] = []
    for p in sorted(WIKI.rglob("*.md")):
        if p.name in ("index.md", "log.md"):
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        i = 0
        while i < len(lines):
            m = _STATUS_RE.match(lines[i].strip())
            if not m:
                i += 1
                continue
            kind, headline = m.group(1), m.group(2).strip(" ——-—")
            body: List[str] = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith(">"):
                body.append(lines[j].lstrip()[1:].strip())
                j += 1
            body = [b for b in body if b]
            # 多数 Status 块的描述在**下一行**（`> - 信源A...`），标题行本身是空的。
            # 空标题回退到正文首行，否则看板会显示一片空白。
            #
            # ⚠️ 回退时要**跳过两类无信息量的行**：
            #    · 「信源X（…）」——经用户化转换后变成
            #      「原始说明文档 声明： 如上表 《原始说明文档》」，读不懂
            #    · 「工程判定/处理方式：…」——是**结论**不是**标题**，
            #      拿来当标题会变成「处理方式: 仅人工一条吻合…」
            #    两类都跳过还找不到，就回退到**所在章节名**（比正文首行更像标题）。
            if not headline and body:
                cand = next(
                    (b for b in body
                     if not re.match(r"^[-–—\s]*信源", b)
                     and not re.match(r"^[-–—\s]*(工程判定|处理方式|判定)", b)),
                    None)
                if cand is None:
                    cand = ""          # 交给下面的章节名兜底
                headline = re.sub(r"^[-–—\s]+", "", cand)[:120]
            # 定位所在章节
            sec = ""
            for k in range(i, -1, -1):
                if lines[k].startswith("#"):
                    sec = lines[k].lstrip("# ").strip()
                    break
            # 章节名兜底：这类块**本来就没有标题行**（整块都是信源+判定），
            # 硬拿正文行当标题只会得到一句读不通的话。用「某节 · 数值待核」更诚实。
            if not headline:
                headline = f"{sec}：数字待核对" if sec else "数字待核对"
            out.append({
                "article": p.relative_to(WIKI_ROOT).as_posix(),
                "section": sec,
                "kind": kind,
                "headline": headline,
                "body": body,
                "line": i + 1,
            })
            i = j
    return out


# ---- 去重与分级 -------------------------------------------------------------
#
# 为什么在**服务端**做，而不去改 wiki：
#   同一条发现分散在多个章节里各登记一次，在**它所在章节的上下文中是有意义的**
#   （读者在那一节就需要知道"这里的数字要小心"）。所以知识层不动。
#   而看板要的是"一共有几件事"，属于**呈现口径**——在出口处统一收敛。
#
# 实测的重复有两类：
#   ① 同文回指：章节里明确写「（见 2.2）」，是同一件事的另一处登记
#   ② 跨文重复：同一条发现在两篇文章各记一次
#
# ⚠️ **刻意用显式规则表，不用模糊相似度**：
#   宁可漏并（少合一条，用户多看一条），**不可错并**——
#   把两个不同的发现合成一条 = 凭空丢信息，且看不出来。

# 回指标记：标题里带「（见 X.Y）」说明是别处的重复登记
_BACKREF_RE = re.compile(r"[（(]见\s*\d+(?:\.\d+)*\s*[)）]")

# 主题归并：正则 → 规范主题名。命中同一主题名的合并为一条。
# 顺序有意义：先匹配到的先用（把更具体的放前面）。
_SUBJECT_RULES: List[tuple] = [
    (r"金银花.{0,10}12%|12%.{0,10}金银花",       "金银花涨幅 12% 不可复现"),
    (r"板蓝根.{0,10}10%|10%.{0,10}板蓝根",       "板蓝根涨幅 10% 只在单耗口径成立"),
    (r"处方量.{0,12}(单粒|单袋)含量|(单粒|单袋)含量.{0,12}处方量",
                                                 "处方量与单含量的量级不符"),
    (r"(占总材料成本比|材料成本占比).{0,6}表|配方.{0,8}成本占比表",
                                                 "配方成本占比表不可用"),
    (r"配方理论材料成本|理论材料成本",              "配方理论成本与实际的倍率口径"),
    # ⚠️ 不能要求出现「不符/差」：实测同一条发现有两种写法——
    #    「工艺人工定额与成本明细不符」与「工艺定额 vs 实际直接人工」，
    #    后者没有"不符"二字。要求关键词会**漏并**，两条并列显示。
    (r"(工艺|人工).{0,10}定额|定额.{0,8}(与|vs).{0,8}(实际|明细|成本)",
                                                 "工艺人工定额与成本明细不符"),
    (r"加班.{0,14}(工时|数据)|工时数据.{0,10}加班",  "加班因素缺乏工时数据支持"),
    (r"收率.{0,10}(波动|分离)|无法与价格效应分离",   "收率波动无法与价格效应分离"),
    # 同一条发现的两种写法：一处叫「对标差异率区间」，另一处正文说「超出声明区间」
    (r"对标差异率.{0,10}区间|差异率.{0,6}区间|声明(的)?区间|超出.{0,4}区间",
                                                 "对标差异率区间与实测不符"),
    (r"本厂水平.{0,10}(无数据|无法核验)|表中.{0,6}本厂水平",
                                                 "「本厂水平」无数据支撑"),
    (r"逐章条文区间|条文区间.{0,10}不符",           "GMP 章节条文区间与全文不符"),
    (r"请求路径|示例.{0,10}路径",                  "接口文档示例路径与实现不符"),
    (r"overdue",                                  "mock 未实现 overdue 状态"),
    (r"资产编号.{0,10}示例|编号示例",              "模板资产编号示例与实际不符"),
    (r"公式与表格|声明的公式.{0,10}不符",           "声明公式与表格数值不符"),
    (r"理由是错的|理由.{0,4}错",                   "原给出的理由已订正"),
]

# 影响分级：决定置顶还是折叠。
#   A = 影响**数字可信度**（用户据此判断结论能不能信）→ 置顶展开
#   B = **资料自身前后不一致**（不影响分析结论，供了解）→ 折叠
_SEVERITY_RULES: List[tuple] = [
    (r"涨幅|上涨|不可复现|倍率|口径|无数据|不可用", "A"),
]


def _subject_of(headline: str, body: List[str]) -> Optional[str]:
    """把一条冲突归到一个规范主题；归不上返回 None（**不合并**）。"""
    hay = headline + " " + " ".join(body[:4])
    for pat, name in _SUBJECT_RULES:
        if re.search(pat, hay):
            return name
    return None


def _severity_of(headline: str, body: List[str], kind: str) -> str:
    """A = 影响结论可信度；B = 资料内部不一致。"""
    if kind == "Update":
        return "A"          # 已订正的数值直接关系到结论，归 A
    hay = headline + " " + " ".join(body[:4])
    for pat, sev in _SEVERITY_RULES:
        if re.search(pat, hay):
            return sev
    return "B"


def _dedupe_and_grade(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    合并重复登记 + 标注影响级别。

    合并策略：同主题只保留**信息最全**的那条（body 最长）作为主条，
    其余合并进来——但**不丢信息**：

      · `merged_count` 记下合并了几条（**数条目，不数文章**——
        同一条可能在同文里登记两次，数文章会漏报）
      · 被合并条目的标题追加到主条正文，写清「另处登记」
        （否则板蓝根 1.8 倍这类细节会凭空消失，
         而"少显示"和"丢信息"是两回事）
    """
    groups: Dict[str, Dict[str, Any]] = {}
    passthrough: List[Dict[str, Any]] = []

    for it in items:
        subj = _subject_of(it.get("headline", ""), it.get("body", []))
        if subj is None:
            passthrough.append(it)
            continue
        cur = groups.get(subj)
        if cur is None:
            it["_subject"] = subj
            it["_merged_titles"] = []
            groups[subj] = it
            continue
        # 已有一组：body 更长的那条当主条（信息更全）
        if len(it.get("body", [])) > len(cur.get("body", [])):
            it["_subject"] = subj
            it["_merged_titles"] = cur["_merged_titles"] + [cur.get("headline", "")]
            groups[subj] = it
        else:
            cur["_merged_titles"].append(it.get("headline", ""))

    merged: List[Dict[str, Any]] = []
    for it in list(groups.values()) + passthrough:
        it["severity"] = _severity_of(it.get("headline", ""), it.get("body", []),
                                      it.get("kind", "Disputed"))
        titles = [t for t in it.get("_merged_titles", []) if t]
        it["merged_count"] = len(titles)
        # ⚠️ **只记标题，不搬被合并条目的正文**。
        #    曾经把它们的 body 也附上来"防止丢信息"，结果**串了主题**：
        #    处方量那条把「配方成本占比表不可用」「配方理论成本 2.20 vs 12.18」
        #    的正文也吸了过来——那是**另外两条独立发现**，不是同一件事。
        #    而标题本身已经带足了细节（"…差 1.8 倍"），搬正文纯属多余。
        if titles:
            it["body"] = list(it.get("body", [])) + [
                "另处登记：" + "；".join(titles[:5])
                + ("…" if len(titles) > 5 else "")]
        for k in ("_subject", "_merged_titles"):
            it.pop(k, None)
        merged.append(it)

    # A 类在前（影响可信度的先看到），同级按原文顺序稳定排序
    merged.sort(key=lambda x: (0 if x["severity"] == "A" else 1))
    return merged


@app.get("/api/conflicts", summary="数据口径说明（已知冲突）")
def conflicts() -> Dict[str, Any]:
    raw = [_polish(i) for i in _parse_conflicts()]
    items = _dedupe_and_grade(raw)
    by: Dict[str, int] = {"Disputed": 0, "Outdated": 0, "Update": 0}
    sev: Dict[str, int] = {"A": 0, "B": 0}
    for it in items:
        by[it["kind"]] = by.get(it["kind"], 0) + 1
        sev[it["severity"]] = sev.get(it["severity"], 0) + 1
    return {"count": len(items), "raw_count": len(raw),
            "by_kind": by, "by_severity": sev, "items": items}


# ---- 用户化转换 -------------------------------------------------------------
# 后端直接抽的是 wiki 原文，里面混着面向内部写作的措辞：
#   `信源A（README 声明）`、`[README.md:3.2 ...]`、`Status: Disputed`……
# 这些对最终使用者没有意义。**在数据出口处统一转一遍**，
# 比在前端到处补正则更可靠（前端只负责渲染）。
_SOURCE_WORDS = {
    "readme": "原始说明文档", "问题检查报告": "数据质量检查记录",
    "成本汇总": "成本汇总表", "原材料消耗明细": "原材料消耗明细表",
    "制造费用明细": "制造费用明细表", "人工工时明细": "人工工时明细表",
    "预算数据": "预算数据表", "药材市场价格行情": "药材市场行情表",
    "行业成本基准数据": "行业基准数据", "车间设备清单": "设备清单",
    "产品配方文档": "产品配方文档", "生产工艺文档": "生产工艺文档",
    "GMP法规核心摘要": "GMP 法规摘要", "药品生产质量管理规范": "GMP 全文",
    "月度成本分析报告模板": "报告模板", "模拟RPA接口文档": "工单接口文档",
    "药材行情月报": "行情月报",
    # 中药二厂后匹配，否则会被"成本汇总"先抓走、丢掉厂别
    "中药一厂": "一厂", "中药二厂": "二厂",
}

# 写作时用的内部标记，出现在正文里时按语义清掉
_STRIP_PHRASES = (
    "违反 SKILL.md 铁律 2.1（截断禁止）", "违反 SKILL.md 铁律 2.1", "SKILL.md 铁律 2.1",
    "（报告事实引擎）", "报告事实引擎", "scripts/report_facts.py", "report_facts.py",
    "scripts/check_math.py", "check_math.py", "scripts/", "SKILL.md",
)


_CITE = re.compile(r"\[([^\[\]]+?)\]")
_FILE_TOKEN = re.compile(r"\.(?:csv|md|pdf|txt|docx|xlsx|xls|py|json)\b", re.I)


def _friendly_file(fname: str) -> str:
    """
    文件名 → 使用者能懂的资料名。

    ⚠️ **必须带上年份**：`中药一厂_成本汇总_2025年…` 与 `…2026年…` 是两份不同的资料，
    都映射成「成本汇总表」会变成「《成本汇总表》+《成本汇总表》」，
    读者无法分辨对比的两边各是什么。
    """
    f = fname.strip()
    year = re.search(r"(20\d{2})", f)
    # 厂别先取出来——`中药二厂` 里也含"成本汇总"之后的匹配会被吞掉，
    # 导致两厂的同名表映射成一模一样的名字，读者分不清对比的两边。
    plant = "二厂" if "中药二厂" in f else ("一厂" if "中药一厂" in f else "")
    base = None
    for k, v in _SOURCE_WORDS.items():
        if k in ("中药一厂", "中药二厂"):
            continue                      # 厂别单独处理
        if k.lower() in f.lower():
            base = v
            break
    if base is None:
        base = re.sub(r"\.(csv|md|pdf|txt|docx|xlsx|xls|py|json)$", "", f, flags=re.I)
    base = f"{plant}{base}" if plant and plant not in base else base
    if year and "年" not in base:
        base = f"{year.group(1)} 年{base}"
    return base


def _userize(s: str) -> str:
    """把一句面向内部的措辞，转成使用者看得懂的话。"""
    t = str(s)
    # `[文件.csv:列名:筛选条件]` → 资料名（保留括号形态，读起来像"依据…"）
    def _cite(m: re.Match) -> str:
        inner = m.group(1)
        if not _FILE_TOKEN.search(inner):
            return m.group(0)          # 不是引用坐标，别动
        return f"《{_friendly_file(inner.split(':')[0])}》"
    # markdown 链接：`[文字](../costs/xx.md)` → 文字（保留可读部分，丢掉路径）
    t = re.sub(r"\[([^\[\]]+?)\]\([^)]*\)", r"\1", t)
    t = _CITE.sub(_cite, t)
    # 写作时的内部标记：清掉，但留下括号里真正有信息量的部分
    for ph in _STRIP_PHRASES:
        t = t.replace(ph, "")
    t = re.sub(r"S?KILL\.md\s*铁律\s*[\d.]+", "数值精度要求", t)
    t = re.sub(r"\b铁律\s*[\d.]+", "数值精度要求", t)
    # 「信源A（X）」→「X」；「信源B（本库的数据）」→「本系统数据」
    t = re.sub(r"信源\s*[A-Za-z]\s*[（(]([^）)]+)[）)]\s*[:：]?", r"\1：", t)
    t = t.replace("**", "").replace("`", "")
    t = re.sub(r"Status:\s*(Disputed|Outdated|Update)\s*(——|--|—)?\s*", "", t)
    t = t.replace("raw/", "").replace("raw 源数据自身", "原始资料本身").replace("raw", "原始资料")
    # 裸词也得换掉——它们没有被 `[…]` 包着，前面的 _cite 抓不到
    t = re.sub(r"\bREADME(\.md)?\b", "原始说明文档", t)
    t = t.replace("本库", "本系统").replace("本词条", "本项").replace("本 agent", "")
    t = t.replace("工程判定", "处理方式")
    t = re.sub(r"\s{2,}", " ", t)
    # 中英混排遗留的空格：`原始说明文档 的` → `原始说明文档的`。
    # ⚠️ 只压**汉字之间**的空格——ASCII 两边要保留，否则
    #    `工艺定额 vs 实际直接人工` 会变成 `定额vs实际`（读不断句）。
    t = re.sub(r"(?<=[一-鿿])\s+(?=[一-鿿])", "", t)
    # 去掉重复引用的同一份资料：`《A》、《A》` → `《A》`
    t = re.sub(r"(《[^》]+》)(?:\s*[、,]\s*\1)+", r"\1", t)
    return t.strip(" -—:：")


def _plain_section(s: str) -> str:
    """章节名去掉编号与内部用语。"""
    t = str(s).strip()
    # ⚠️ 编号可能带小数：`3.2 与 README …` —— 只削一层 `3.` 会留下 `2 与 …`
    t = re.sub(r"^\d+(?:\.\d+)*\s*[\.、]?\s*", "", t)
    t = re.sub(r"\bREADME(\.md)?\b", "原始说明文档", t)
    for bad in ("核验中发现的不可自洽之处", "核验中发现的问题", "不可自洽之处", "核验"):
        t = t.replace(bad, "数据核对说明")
    return t.strip() or "数据核对说明"


def _polish(it: Dict[str, Any]) -> Dict[str, Any]:
    o = dict(it)
    o["headline"] = _userize(it.get("headline", ""))
    o["section"] = _plain_section(it.get("section", ""))
    o["body"] = [_userize(b) for b in it.get("body", [])]
    return o


# ---------------------------------------------------------------------------
# 摄入活动时间线 —— 解析 log.md
# ---------------------------------------------------------------------------

_LOG_HDR = re.compile(r"^##\s*\[(\d{4}-\d{2}-\d{2})\]\s*([^|]+?)\s*\|\s*(.+?)\s*$")


def _parse_timeline() -> List[Dict[str, Any]]:
    p = WIKI / "log.md"
    if not p.exists():
        return []
    lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
    entries: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for ln in lines:
        m = _LOG_HDR.match(ln.strip())
        if m:
            if cur:
                entries.append(cur)
            cur = {"date": m.group(1), "kind": m.group(2).strip(),
                   "title": m.group(3).strip(), "body": []}
            continue
        if cur is not None:
            cur["body"].append(ln)
    if cur:
        entries.append(cur)

    for e in entries:
        text = "\n".join(e["body"])
        e["disposition"] = ""
        dm = re.search(r"Disposition:\s*(.+)", text)
        if dm:
            # 同样清掉 markdown 记号（实测 `Update; **Outdated**` 会原样透出）
            e["disposition"] = dm.group(1).replace("**", "").replace("`", "").strip()
        e["files"] = sorted(set(re.findall(r"raw/[\w/一-鿿.\-（）]+", text)))[:6]

        # 摘要必须是**干净正文**——不能把 markdown 记号透给前端。
        # 实测踩过：直接 join 原文，前端显示成
        # `**这是本库第一次...** 验收「...」时（`finalize.py` 无条件...）`
        # 满屏 `**` 和反引号，还混进了代码围栏里的内容。
        # 元数据行不是正文，摘要里不该出现（否则每条 ingest 的摘要都是
        # "Disposition: New; Disputed Raw: raw/...csv; ..." —— 全是噪音）
        META = ("disposition:", "raw:", "updated:", "updated：", "产出:", "产出：")

        clean: List[str] = []
        in_fence = False
        for ln in e["body"]:
            s = ln.strip()
            if s.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not s or s.startswith((">", "#", "|", "---")):
                continue
            s = re.sub(r"^[-*]\s+", "", s)              # 去列表符号
            s = re.sub(r"^\[[ x]\]\s*", "", s)          # 去复选框
            if s.lower().startswith(META):
                continue
            # 兜底：清掉任何残留的 markdown 记号字符。
            # 前面按对剥 `**x**` / `` `x` ``，但截断、嵌套、有意转义
            # （如 `\r`）都可能留下孤立记号——直接抹掉所有反引号与星号最稳。
            s = s.replace("`", "").replace("**", "")
            s = re.sub(r"^\s*[-*]\s*", "", s)
            s = s.strip()
            if s:
                clean.append(s)
        e["excerpt"] = " ".join(clean)[:190]
        e.pop("body", None)
    return entries


@app.get("/api/timeline", summary="数据更新记录")
def timeline() -> Dict[str, Any]:
    """
    数据更新记录。

    ⚠️ 只保留**与数据有关的**事件。日志里还有大量纯工程活动
    （部署、测试、后端搭建、依赖修复），那是开发记录，不是用户关心的
    "数据发生了什么"。把它们混进来会让这一页变成开发者变更日志。
    """
    # 面向用户的事件类型：纳入资料 / 校核 / 修正 / 规则调整
    USER_FACING = {"ingest", "lint", "fix", "skill"}
    items = [i for i in _parse_timeline() if i.get("kind") in USER_FACING]
    items = [_polish_timeline(i) for i in items]
    return {"count": len(items), "items": items}


# 工程活动里常见的词——出现即说明这条是开发记录而非数据记录
_DEV_NOISE = re.compile(
    r"(Dockerfile|docker|npm|node_modules|__pycache__|\.pyc|py_compile|"
    r"requirements\.txt|\.env|\.gitignore|\.dockerignore|PATH|"
    r"def |import |py\b|\.js\b|\.css\b|API 端点|FastAPI|React|"
    r"端口|进程|镜像|容器|卷|部署|构建|编译|重构|"
    r"[a-z_]+\.py\b|scripts/|"
    # 内部技术事故（用户不关心换行符、编码、依赖这类问题）
    r"换行符|污染|编码|依赖|超时|并发|性能|缓存|死循环|"
    r"根因|三层防御)", re.I)

# 摘要要不要展示的**严格判据**：
#   日志正文本质是开发记录，里面混着路径、英文标识符、命令片段。
#   与其写一堆正则把它们逐类擦掉（永远擦不干净，且会擦出病句），
#   不如**反向判断**——清洗后若仍含任何"像代码的东西"，就整条不显示。
#   宁可少给一句话，也不要给用户一段天书。
_LOOKS_LIKE_CODE = re.compile(
    r"([\w一-鿿\-]+/[\w一-鿿\-./]+"   # 路径 market/xxx.md
    r"|\b[A-Za-z_][A-Za-z0-9_]*\.(md|py|csv|json|txt|js|css|sql|yaml)\b"
    r"|\bStatus:\s*\w+"                                 # Status: Disputed
    r"|\b[A-Z][A-Za-z]{2,}(?=[\s,，。；])"              # 孤立英文词 Disputed/Update
    r"|\bpython\s|\bpip\s|\bgit\s|\bdocker\s"           # 命令
    r")")


def _safe_excerpt(s: str) -> str:
    """只有确认"不像代码"的摘要才放行，否则返回空。"""
    t = _dejargon(_userize(str(s or "")))
    if _DEV_NOISE.search(t) or _LOOKS_LIKE_CODE.search(t):
        return ""
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t if len(t) >= 12 else ""


def _polish_timeline(it: Dict[str, Any]) -> Dict[str, Any]:
    o = dict(it)
    o["title"] = _userize(_dejargon(clean_timeline_title(it.get("title", ""))))
    o["excerpt"] = _safe_excerpt(it.get("excerpt", ""))
    o["disposition"] = _dejargon(_userize(it.get("disposition", "")))
    # 处置结论里也只留"新增/修订/存疑"这类词，去掉括号里的技术细节
    d = o["disposition"]
    d = re.sub(r"[（(][^）)]*(\.md|\.py|\.csv|/[^）)]*)[）)]", "", d)
    o["disposition"] = re.sub(r"\s{2,}", " ", d).strip(" ;；,，")
    return o


def clean_timeline_title(t: str) -> str:
    """日志标题 → 用户标题。"""
    s = str(t)
    s = re.sub(r"（骨架 [A-G]'?）", "", s)
    s = re.sub(r"^(no material|未纳入)[：:]\s*", "未带来新信息：", s, flags=re.I)
    s = re.sub(r"\braw/[\w/一-龥.\-（）]+", "某份资料", s)
    # 测试题号（Q1/Q3/Q5）是内部验证用的编号 → 说成"一次提问"
    s = re.sub(r"^Q\d+\s*", "一次提问", s)
    # 工程向的标题直接换成中性说法
    if _DEV_NOISE.search(s):
        return "系统能力更新"
    return s.strip()


# 日志正文里频繁出现的规制用语 → 用户语言
_DISPOSITION_RE = re.compile(r"Disposition:\s*(.+)")
_JARGON = [
    (r"\breferences/[\w一-龥.\-]+\.md\b", "规则文档"),
    (r"\bscripts/[\w.\-]+\.py\b", "分析工具"),
    (r"\bSKILL\.md\b", "规则文档"),
    (r"\bingest_raw\.py\b", "资料收录工具"),
    (r"\braw/\S+", "原始资料"),
    (r"\bDisputed\b", "存在口径存疑"),
    (r"\bNo material\b", "未带来新信息"),
    (r"\bOutdated\b", "已过期"),
    (r"\bUpdate\b", "修订"),
    (r"\bNew\b", "新增"),
    (r"约束层", "规则"),
    (r"词条骨架", "文章模板"),
    (r"词条", "分析条目"),
    (r"级联", "关联更新"),
    (r"编译", "整理"),
    (r"入库", "纳入"),
]


def _dejargon(s: str) -> str:
    t = str(s)
    for pat, rep in _JARGON:
        t = re.sub(pat, rep, t)
    return t


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

@app.get("/api/reports", summary="已生成的报告清单")
def reports_list() -> Dict[str, Any]:
    if not REPORTS.exists():
        return {"count": 0, "items": []}
    items = []
    for p in sorted(REPORTS.glob("*.md")):
        m = re.match(r"(.+)_(\d{4}-\d{2})\.md$", p.name)
        items.append({
            "file": p.name,
            "product": m.group(1) if m else p.stem,
            "month": m.group(2) if m else "",
            "size": p.stat().st_size,
        })
    return {"count": len(items), "items": items}


@app.get("/api/report/{product}/{month}", summary="读取单份报告")
def report_get(product: str, month: str) -> Dict[str, Any]:
    p = REPORTS / f"{product}_{month}.md"
    if not p.exists():
        raise HTTPException(404, f"报告不存在: {p.name}")
    return {"file": p.name, "product": product, "month": month,
            "markdown": p.read_text(encoding="utf-8")}


# ---------------------------------------------------------------------------
# 检索 —— 复用混合检索（BM25 + 向量 + RRF）
# ---------------------------------------------------------------------------

@app.get("/api/search", summary="混合检索（BM25+向量+RRF）")
def search(q: str = Query(..., min_length=1), top_k: int = Query(6, ge=1, le=20),
           authorization: str = Header(default=""),
           lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    混合检索。

    ⚠️ 登录用户的检索范围是「共享基线 + **他自己的**个人资料」；
       未登录只搜共享基线（未登录时没有"他"可言）。
       个人工作区**只取当前会话用户的那一个**——传错就是跨用户泄漏。
    """
    import paths
    info = None
    try:
        info = _whoami(authorization, lw_sid)
    except HTTPException:
        info = None                      # 未登录：只用共享基线，不是错误
    ov = ({"shared_root": WIKI_ROOT, "personal_root": paths.ensure_workspace(info["external_id"])}
          if info else {})

    try:
        import retrieval
        # 先确保索引覆盖当前语料（含个人层）。hybrid_search 内部也会查覆盖率，
        # 这里显式建一次，让"个人块缺向量"在入口就被修掉。
        idx = None
        try:
            idx = retrieval.build_index(verbose=False, root=WIKI_ROOT, **ov)
        except Exception:  # noqa: BLE001
            idx = None               # 离线/无 key 时退回只用 BM25
        hits = retrieval.hybrid_search(q, top_k=top_k, root=WIKI_ROOT,
                                       index=idx, allow_online=idx is None, **ov)
        return {"query": q, "count": len(hits),
                "scope": "shared+personal" if info else "shared",
                "hits": [{"article": getattr(h.chunk, "article", ""),
                          "section": getattr(h.chunk, "section", ""),
                          "source": getattr(h.chunk, "source", "shared"),
                          "text": getattr(h.chunk, "text", "")[:400],
                          "score": getattr(h, "rrf", None)} for h in hits]}
    except Exception as e:  # noqa: BLE001
        return {"query": q, "count": 0, "hits": [], "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 知识图谱（赛题 6.2「知识图谱增强 RAG」的前端呈现）
#
# 图谱是**派生视图**（`graph/graph.json`，由 graph_build.py 从 wiki 抽取）。
# 这里只读、不重建——重建归 finalize.py。读不到就如实报，别假装有图。
# ---------------------------------------------------------------------------

_GRAPH_TYPE_ZH = {
    "product": "产品", "material": "原料", "process": "工序",
    "equipment": "设备", "incident": "故障", "anomaly": "成本异动",
    "peer": "对标",
}


@app.get("/api/heatmap", summary="产品×月份×成本要素 热力图数据")
def heatmap(elements: str = Query("单位材料,单位人工,单位制造费用"),
            months: int = Query(6, ge=3, le=12)) -> Dict[str, Any]:
    """
    赛题 5.2.2 的**可选加分项**：三维交叉分析。

    ⚠️ 热力图画的是**环比变动率**，不是绝对值。
       绝对值做热力图没有意义——三个产品的单价量级差 2.5 倍
       （7.47 / 11.21 / 18.09），跨产品比"颜色深浅"只会得到
       "六味最深"这种废话。**变动率才是可比的**。
    """
    keys = [k.strip() for k in elements.split(",") if k.strip()]
    products = RF.list_products()
    out_products: List[Dict[str, Any]] = []
    all_vals: List[float] = []

    for p in products:
        fs = RF.build(p, "2026-05")
        rows = fs.monthly[-months:]
        cells = []          # [要素, 月份, 环比%] —— ECharts heatmap 的格式
        for i, r in enumerate(rows):
            for k in keys:
                raw = r.get(k)
                if raw in (None, "", "—"):
                    continue
                cur = RF._num(raw)
                if cur is None:
                    continue
                # 环比 = (本月 - 上月) / 上月，需要上一个月同要素的值
                if i == 0:
                    continue                    # 首月没有上月，没有环比
                prev_raw = rows[i - 1].get(k)
                prev = RF._num(prev_raw) if prev_raw not in (None, "", "—") else None
                if not prev:
                    continue
                pct = round((cur - prev) / prev * 100, 2)
                cells.append([k, r["月份"], pct])
                all_vals.append(pct)
        out_products.append({
            "product": p, "cells": cells,
            "months": [r["月份"] for r in rows],
        })

    bound = round(max((abs(v) for v in all_vals), default=1.0), 2)
    return {
        "elements": keys, "products": out_products,
        "bound": bound,                     # 对称的色阶边界（±bound）
        "unit": "%",
        "note": "数值为逐月环比变动率；色阶以 0 为中心对称，便于一眼看出涨跌",
    }


@app.get("/api/decide", summary="自主决策：生成报告 or 只更新看板")
def decide_endpoint(month: str = Query("2026-05")) -> Dict[str, Any]:
    """
    赛题 6.2「Agent自主决策」。

    ⚠️ **判断由脚本做，不是 LLM**——"知识库有没有变"是可判定的，
       交给模型去"理解意图"就变成不可复算：判错的代价不是报错，
       而是**该出的报告没出**（用户以为看板刷新了就没事）。
    返回带 `reasons`，每个信号都能查、能审计。
    """
    import decide as D
    d = D.decide(WIKI_ROOT, month)
    return d.to_dict()


@app.get("/api/graph", summary="知识图谱（节点与边）")
def graph(focus: str = Query("", description="只取该实体 N 跳内的子图，空=全图"),
          hops: int = Query(2, ge=1, le=4)) -> Dict[str, Any]:
    """
    返回图谱。`focus` 给定时只返回该实体 **hops 跳内的子图**。

    ⚠️ 全图 83 节点直接铺到前端会变成一团麻（力导向图节点一多就看不清）。
       按实体聚焦是**可读性**的必要条件，不是可选优化。
    """
    p = WIKI_ROOT / "graph" / "graph.json"
    if not p.exists():
        return {"ok": False, "error": "图谱尚未构建（跑 python scripts/graph_build.py）",
                "nodes": [], "edges": []}
    try:
        g = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error": f"图谱不可读：{type(e).__name__}",
                "nodes": [], "edges": []}

    nodes = {n["id"]: n for n in g.get("nodes", [])}
    edges = g.get("edges", [])

    if focus:
        if focus not in nodes:
            near = [i for i in nodes if focus and focus in i][:8]
            return {"ok": False, "error": f"图谱中没有「{focus}」",
                    "near": near, "nodes": [], "edges": []}
        # 双向 BFS 取子图
        keep, frontier = {focus}, {focus}
        for _ in range(hops):
            nxt = set()
            for e in edges:
                if e["src"] in frontier and e["dst"] not in keep:
                    nxt.add(e["dst"])
                if e["dst"] in frontier and e["src"] not in keep:
                    nxt.add(e["src"])
            keep |= nxt
            frontier = nxt
        edges = [e for e in edges if e["src"] in keep and e["dst"] in keep]
        nodes = {i: n for i, n in nodes.items() if i in keep}

    return {
        "ok": True,
        "focus": focus or None,
        "stats": {"nodes": len(nodes), "edges": len(edges)},
        "type_zh": _GRAPH_TYPE_ZH,
        # 前端只需要 id/label/type 画点，边只留 src/dst/relation
        # 坐标（coord）带上——点开节点时用它跳到对应的知识页面
        "nodes": [{"id": n["id"], "label": n.get("label") or n["id"],
                   "type": n.get("type", ""), "coord": n.get("coord", ""),
                   "attrs": n.get("attrs") or {}} for n in nodes.values()],
        "edges": [{"src": e["src"], "dst": e["dst"],
                   "relation": e.get("relation", "")} for e in edges],
    }


# ---------------------------------------------------------------------------
# 对标分析三步法（赛题模块三）
# ---------------------------------------------------------------------------

@app.get("/api/benchmark", summary="对标分析三步法")
def benchmark(product: str = Query(...), month: str = Query("2026-05"),
              llm: bool = Query(False, description="是否调用 LLM 生成第三步归因")) -> Dict[str, Any]:
    """
    三步法：找差异 → 拆结构 → 拆原因。

    前两步是确定性计算（快，随时可跑）；第三步要调 LLM（慢，默认关闭）。
    看板默认只跑前两步，用户点「生成归因」时才带 `llm=1`。
    """
    import benchmark_engine as BE
    import report_facts as RF

    # ⚠️ 先校验产品名。没有这道关，打错一个字（或客户端用 GBK 传中文）
    #    会匹配不到任何数据行，一路走到引擎深处才炸出晦涩的
    #    `KeyError: 'input'`——用户完全看不出是自己产品名写错了。
    known = RF.list_products()
    if product not in known:
        raise HTTPException(
            404, f"未知产品「{product}」。可选：{'、'.join(known)}")

    try:
        r = BE.run(product, month, use_llm=llm, verbose=False)
    except Exception as e:  # noqa: BLE001
        import traceback as _tb
        _tb.print_exc()
        raise HTTPException(500, f"三步法执行失败：{type(e).__name__}: {e}")
    s1, s2, s3 = r["steps"]
    return {
        "product": product, "month": month,
        "steps_meta": r["steps_meta"],
        "step1": s1,
        "step2": s2,
        "step3": s3,
    }


# ---------------------------------------------------------------------------
# 对话 —— 这是看板**唯一**有副作用的端点
#
# ⚠️ 它写的是 **agent 的记忆（权威区）**，不是知识库。
#    wiki/ 与 raw/ 依然只读，看板不会因为聊天而改数据。
# ---------------------------------------------------------------------------

from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


class ChatIn(BaseModel):
    question: str
    new: bool = False
    # 指定在哪个会话里继续（多会话切换）。**归属由服务端校验**：
    # 客户端只能"说出一个 id"，不能决定它属不属于自己。
    conversation_id: Optional[int] = None
    # ⚠️ 这里**故意没有 `user` 字段**。
    #    身份只能来自 Authorization 头，客户端无权自报。
    #    （旧版有 `user: str = "web"`，那是身份冒充的入口。）


def _session(user: str, new: bool = False,
             conversation_id: Optional[int] = None):
    """
    打开某用户的会话。

    ⚠️ 必须把**个人工作区**传下去。不传的话，AgentSession 只会读共享基线：
       用户上传的资料能编译进他的工作区，但**问答时检索不到**——
       上传功能实际是白做的。（这是本函数此前的真实缺陷。）

    `conversation_id` 指定切换到哪个已有会话。**先在这里验归属**，
    不通过直接 404——不把这个判断推给运行时，因为它不持有鉴权上下文。
    """
    import agent_runtime
    import paths
    ws = paths.ensure_workspace(user)          # 幂等建骨架
    # ⚠️ 两个根不能相同：个人工作区若等于代码仓库根，
    #    用户上传就能写进共享基线（越权）。这里显式拦一道。
    if Path(ws).resolve() == Path(WIKI_ROOT).resolve():
        raise HTTPException(500, "配置错误：个人工作区与共享基线指向同一目录")

    if conversation_id is not None:
        # 归属校验放在鉴权边界（这一层），与其余端点一致：不通过就是 404
        s = agent_runtime.AgentSession.open(
            user, root=WIKI_ROOT, personal=ws, new=new)
        if not s.mem.owns_conversation(s.user_id, conversation_id):
            raise HTTPException(404, f"会话不存在：{conversation_id}")

    return agent_runtime.AgentSession.open(
        user, root=WIKI_ROOT, personal=ws, new=new,
        conversation_id=conversation_id)


# ---------------------------------------------------------------------------
# 鉴权 —— 把"你是谁"从**客户端自报**改为**服务端认定**
#
# 修的是什么：原实现 `?user=demo` 这种参数是客户端随便填的，
# 改个名字就能读到那个人的全部对话历史与偏好（实测确认无任何校验）。
#
# 现在客户端只能**出示令牌**，说不出自己是谁：
#   Authorization: Bearer lw_xxx   →   auth.db 查表   →   得到用户标识
# 令牌对不上就 401，不存在"填个名字"这条路。
# ---------------------------------------------------------------------------


SESSION_COOKIE = "lw_sid"
CSRF_COOKIE = "lw_csrf"          # 刻意**不设 HttpOnly**：前端要读出来放进请求头
CSRF_HEADER = "X-CSRF-Token"

# 本地开发没有 HTTPS，`Secure` 会直接让 cookie 发不出去。
# ⚠️ 生产部署必须改成 True（见 references/技术方案.md 的部署章节）。
COOKIE_SECURE = os.getenv("LW_COOKIE_SECURE", "0") == "1"


def _whoami(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    认定当前用户。**两条路都认**，返回同一种身份结构。

      ① Cookie `lw_sid`      → 浏览器（人用；HttpOnly，JS 偷不到）
      ② `Authorization: Bearer lw_…` → 命令行 / 脚本 / 自动化截图

    ⚠️ 两路合一的好处：**13 个已有端点一行都不用改**——
       它们只认这个函数返回的身份，不关心凭证从哪来。

    保留 Bearer 的理由：`agent_auth.py --issue` 对 CI、自动化截图、
       外部集成仍有用，且改造成本为零。
    """
    import agent_auth
    a = agent_auth.Auth()
    if lw_sid:
        info = a.resolve_session(lw_sid)
        if info:
            return info
    tok = ""
    if authorization.lower().startswith("bearer "):
        tok = authorization[7:].strip()
    info = a.resolve(tok) if tok else None
    if not info:
        raise HTTPException(
            401, "请先登录。浏览器端用登录后的会话 Cookie；"
                 "命令行/脚本可带 `Authorization: Bearer <令牌>`"
                 "（令牌由 `python scripts/agent_auth.py --issue <用户>` 签发）。")
    return info


# ---- CSRF ----------------------------------------------------------------
# 用 Cookie 做会话就必然面对 CSRF：浏览器会自动带上本站 cookie，
# 所以"别的站点伪造一个表单"也能带着你的身份发请求。
#
# 三层防护（成本都很低，叠起来）：
#   ① Cookie 上的 `SameSite=Lax` —— 跨站 POST 不带 cookie（主力）
#   ② Origin/Referer 校验        —— 老旧浏览器不支持 SameSite 时的兜底
#   ③ Double-submit token        —— 防"同站子域被攻破"的情况
#
# ⚠️ 只对**写操作**（非 GET/HEAD/OPTIONS）检查：
#    GET 不应有副作用，检查它只会让公开数据接口无谓地变脆。

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _check_csrf(request: Request) -> None:
    """写操作的来源校验。不通过就 403。"""
    if request.method in _SAFE_METHODS:
        return

    # ② Origin / Referer 同源校验
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    host = request.headers.get("host") or ""
    if origin:
        # Origin 形如 http://127.0.0.1:8765
        if origin.rstrip("/").split("://")[-1] != host:
            raise HTTPException(403, "请求来源不合法（CSRF 防护）")
    elif referer:
        if referer.split("://")[-1].split("/")[0] != host:
            raise HTTPException(403, "请求来源不合法（CSRF 防护）")
    # 两者都没有：命令行工具（curl 等）不发这两个头。
    # 这类客户端用 Bearer 令牌，不依赖 cookie，所以不受 CSRF 影响——
    # 放行，否则 CLI 全用不了。

    # ③ Double-submit token（浏览器会带 cookie，故必然能比对）
    cookie_tok = request.cookies.get(CSRF_COOKIE, "")
    header_tok = request.headers.get(CSRF_HEADER, "")
    if cookie_tok:
        if not header_tok or not hmac.compare_digest(cookie_tok, header_tok):
            raise HTTPException(403, "缺少或错误的 CSRF 令牌，请刷新页面重试")


CSRF_EXEMPT = {"/api/auth/login", "/api/auth/register", "/api/auth/logout"}


@app.middleware("http")
async def csrf_middleware(request: Request, call_next):
    """
    写操作统一过 CSRF 检查。

    ⚠️ 三个 auth 端点是**豁免**的：
       登录/注册时用户还没有 csrf cookie——那个 cookie 正是**它们发下去的**，
       要求它等于"要求先登录才能登录"。豁免它们不降低安全性：
       攻击者伪造登录请求，最坏是"把受害者的浏览器登到攻击者的账号上"，
       而那需要攻击者知道自己的密码，且受害者能立刻察觉（界面变成了别人的）。
    """
    if request.url.path not in CSRF_EXEMPT:
        try:
            _check_csrf(request)
        except HTTPException as e:
            return JSONResponse({"detail": e.detail}, status_code=e.status_code)
    return await call_next(request)


def _set_session_cookies(resp: JSONResponse, sid: str) -> None:
    """下发会话 cookie + CSRF cookie。"""
    resp.set_cookie(
        SESSION_COOKIE, sid,
        max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, path="/")
    # ⚠️ CSRF cookie 故意**不加 HttpOnly**——前端要读它放进 X-CSRF-Token。
    #    它本身不是凭证（攻击者拿到也做不了什么），配合服务端比对才有意义。
    resp.set_cookie(
        CSRF_COOKIE, secrets.token_urlsafe(24),
        max_age=60 * 60 * 24 * 30, httponly=False, samesite="lax",
        secure=COOKIE_SECURE, path="/")


def _client_ip(request: Request) -> str:
    """取客户端 IP。反代后面取 X-Forwarded-For 的第一段。"""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


# ---- 注册 / 登录 / 登出 ---------------------------------------------------

class CredIn(BaseModel):
    username: str
    password: str
    name: str = ""


@app.post("/api/auth/register", summary="注册新用户")
def auth_register(body: CredIn, request: Request) -> JSONResponse:
    """注册并**直接登录**（不用再输一遍）。"""
    import agent_auth
    import paths

    a = agent_auth.Auth()
    r = a.register(body.username, body.password, name=body.name)
    if not r["ok"]:
        raise HTTPException(400, r["error"])

    ext = r["external_id"]
    try:
        paths.ensure_workspace(ext)          # 建个人工作区（inbox/raw/wiki/state）
    except Exception:                        # noqa: BLE001
        pass                                 # 建不出来不影响注册，首次上传会再建

    sid = a.create_session(r["user_id"], ua=request.headers.get("user-agent", ""))
    resp = JSONResponse({
        "ok": True, "external_id": ext,
        "name": a.get_user(ext).get("name") or ext,
    })
    _set_session_cookies(resp, sid)
    return resp


@app.post("/api/auth/login", summary="登录")
def auth_login(body: CredIn, request: Request) -> JSONResponse:
    import agent_auth
    a = agent_auth.Auth()
    r = a.authenticate(body.username, body.password, ip=_client_ip(request),
                       ua=request.headers.get("user-agent", ""))
    if not r["ok"]:
        # 429 表示"被限流了"，与 401"凭证不对"区分开，前端好给不同提示
        raise HTTPException(429 if r.get("locked") else 401, r["error"])

    sid = a.create_session(r["user_id"], ua=request.headers.get("user-agent", ""))
    resp = JSONResponse({"ok": True, "external_id": r["external_id"],
                         "name": r["name"]})
    _set_session_cookies(resp, sid)
    return resp


@app.post("/api/auth/logout", summary="退出登录")
def auth_logout(lw_sid: str = Cookie(default="")) -> JSONResponse:
    import agent_auth
    if lw_sid:
        agent_auth.Auth().revoke_session(lw_sid)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")
    return resp


@app.get("/api/auth/me", summary="当前登录者（未登录也返回 200）")
def auth_me(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    当前登录者。

    ⚠️ **未登录时返回 200 + `logged_in: false`，不返回 401。**
       前端每次启动都调它；返回 401 会让浏览器控制台每次都留一条红色错误，
       用户看到会以为"网站坏了"。**未登录是一种正常状态，不是错误。**
    """
    import agent_auth
    import paths
    a = agent_auth.Auth()
    info = a.resolve_session(lw_sid) if lw_sid else None
    if not info and authorization.lower().startswith("bearer "):
        info = a.resolve(authorization[7:].strip())
    if not info:
        return {"logged_in": False}
    return {
        "logged_in": True,
        "external_id": info["external_id"],
        "name": info["name"],
        "role": info["role"],
        # ⚠️ 只回文件名，不回完整路径——路径属内部信息
        "user_db": paths.user_db(info["external_id"]).name,
    }


@app.get("/api/profile", summary="个人中心：账号 + 会话 + 个人知识库")
def profile(authorization: str = Header(default=""),
            lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    个人中心一屏所需的数据，**一次返回**。

    ⚠️ 为什么不拆成三个端点：个人中心开页会同时要账号、会话数、工作区清单。
       三次往返里任何一次慢或失败，页面就半残。合成一个原子快照更好。

    ⚠️ 数据全部取自**当前会话用户**——复用 `_session()`（按会话身份建库）
       与 `_ws()`（按会话身份定位工作区），两者都已在别处验证过隔离性。
    """
    import paths
    info = _whoami(authorization, lw_sid)
    ext = info["external_id"]

    out: Dict[str, Any] = {
        "external_id": ext,
        "name": info["name"],
        "role": info["role"],
        "user_db": paths.user_db(ext).name,
        "conversations": [], "workspace": None,
    }

    # 会话历史（复用已有的会话对象——它就是按 external_id 打开自己的库）
    try:
        s = _session(ext)
        convs = s.mem.list_conversations(s.user_id)
        out["conversations"] = [
            {"id": c["id"], "title": c.get("title") or "未命名",
             "created_at": c.get("created_at", "")} for c in convs
        ]
        out["stats"] = s.mem.stats()
    except Exception as e:  # noqa: BLE001
        out["conversations_error"] = f"{type(e).__name__}: {e}"

    # 个人工作区清单
    try:
        ws = _ws(ext)
        def _ls(sub: str) -> List[Dict[str, Any]]:
            d = ws / sub
            if not d.exists():
                return []
            return [{"path": p.relative_to(ws).as_posix(), "size": p.stat().st_size}
                    for p in sorted(d.rglob("*")) if p.is_file()
                    and not p.name.startswith(".")
                    and not (p.suffix.lower() == ".txt" and p.with_suffix(".pdf").exists())]
        raw, wiki, inbox = _ls("raw"), _ls("wiki"), _ls("inbox")
        out["workspace"] = {
            "raw": raw, "wiki": wiki,
            # ⚠️ inbox（待编译）也要列。上传先落 inbox，跑完摄入才进 raw——
            #    不列的话，用户刚传完打开个人中心**什么都看不到**，
            #    会以为上传失败了（实测踩到）。
            "inbox": inbox,
            "counts": {"raw": len(raw), "wiki": len(wiki), "inbox": len(inbox)},
        }
    except Exception as e:  # noqa: BLE001
        out["workspace_error"] = f"{type(e).__name__}: {e}"

    return out


@app.get("/api/me", summary="当前令牌身份（命令行兼容）")
def me(authorization: str = Header(default=""),
       lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """保留旧路径：`_whoami` 现在两条路都认，行为与 `/api/auth/me` 一致但要求已登录。"""
    info = _whoami(authorization, lw_sid)
    import paths
    return {"external_id": info["external_id"], "name": info["name"],
            "role": info["role"],
            "user_db": paths.user_db(info["external_id"]).name}


@app.get("/api/chat/history", summary="当前用户的会话历史")
def chat_history(authorization: str = Header(default=""),
                 lw_sid: str = Cookie(default=""),
                 limit: int = Query(30, ge=1, le=200)) -> Dict[str, Any]:
    """
    当前令牌所属用户的会话列表 + 消息（供前端恢复界面）。

    ⚠️ **不再接受客户端传的 `user` 参数**——那是身份冒充的入口。
    """
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    convs = s.mem.list_conversations(s.user_id)
    msgs = [{"id": m["id"], "role": m["role"], "content": m["content"],
             "tool_name": m["tool_name"], "created_at": m["created_at"]}
            for m in s.mem.load(s.conv_id)]
    ctx = s.mem.get_context(s.conv_id)
    return {"me": {"external_id": info["external_id"], "name": info["name"]},
            "user_db": __import__("paths").user_db(info["external_id"]).name,
            "conversation_id": s.conv_id, "conversations": convs,
            "messages": msgs,
            "summary": ctx.get("summary", ""),
            "summary_upto": ctx.get("summary_upto", 0),
            "compactions": ctx.get("compactions", 0),
            "prefs": s.mem.all_prefs(s.user_id)}


# ---------------------------------------------------------------------------
# 会话管理（多会话：新建 / 切换 / 删除）
# ---------------------------------------------------------------------------
# ⚠️ **每个按 id 操作的端点都必须先校验归属**（`mem.owns_conversation`）。
#    跨用户读不到别人的数据是靠"一人一个物理库文件"挡住的，但**同一用户库内
#    有多个会话**——不校验归属，前端传一个别的 conv_id 就能读到同库内其他会话。
#
#    校验不过一律返回 **404 而不是 403**：403 等于告诉对方"这个 id 存在，
#    只是不属于你"，那是信息泄露（能据此枚举出别人有哪些会话 id）。
#    对调用方而言，"不存在"与"不是你的"应当是**同一种回答**。

def _conv_guard(info: Dict[str, Any], cid: int):
    """取某个会话，**先验归属**。不通过 → 404（见上方说明）。"""
    s = _session(info["external_id"])
    if not s.mem.owns_conversation(s.user_id, cid):
        raise HTTPException(404, f"会话不存在：{cid}")
    return s


@app.get("/api/conversations", summary="当前用户的会话列表")
def conversations_list(authorization: str = Header(default=""),
                       lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """只列会话本身，**不含消息**——列表要轻，消息在切进去时才拉。"""
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    return {"conversation_id": s.conv_id,        # 当前活跃的那个，前端用来选中
            "conversations": s.mem.list_conversations(s.user_id, limit=100)}


class ConvIn(BaseModel):
    title: str = ""


@app.post("/api/conversations", summary="新建会话")
def conversations_new(body: ConvIn = ConvIn(),
                      authorization: str = Header(default=""),
                      lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    显式新建一个会话。

    ⚠️ 前端**通常在首次提问时**才建（带 `new: true` 走 `/api/chat`），
       而不是点"新对话"按钮就建——否则用户点开看看又关掉，会攒一堆空会话。
       这个端点留给"想先建好再慢慢问"的场景，以及脚本/自动化。
    """
    info = _whoami(authorization, lw_sid)
    s = _session(info["external_id"])
    cid = s.mem.new_conversation(s.user_id, title=(body.title or "").strip()[:80])
    return {"conversation_id": cid}


@app.get("/api/conversations/{cid}", summary="取指定会话的消息与摘要")
def conversations_get(cid: int,
                      authorization: str = Header(default=""),
                      lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """切进某个会话时拉它的消息与压缩摘要。**先验归属。**"""
    info = _whoami(authorization, lw_sid)
    s = _conv_guard(info, cid)
    ctx = s.mem.get_context(cid)
    return {"conversation_id": cid,
            "messages": s.mem.load(cid),
            "summary": ctx.get("summary", ""),
            "summary_upto": ctx.get("summary_upto", 0),
            "compactions": ctx.get("compactions", 0)}


@app.delete("/api/conversations/{cid}", summary="删除会话")
def conversations_delete(cid: int,
                         authorization: str = Header(default=""),
                         lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    删除一个会话及其全部消息。**先验归属**（写操作，走 CSRF 中间件）。

    ⚠️ 消息由 `mem.delete_conversation()` 显式删除，不依赖外键级联——
       `PRAGMA foreign_keys` 是连接级开关，裸连接下默认关。
    """
    info = _whoami(authorization, lw_sid)
    s = _conv_guard(info, cid)
    ok = s.mem.delete_conversation(s.user_id, cid)
    return {"deleted": bool(ok), "conversation_id": cid}


@app.post("/api/chat", summary="智能问答（SSE 流式）")
def chat(body: ChatIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> StreamingResponse:
    """
    SSE 流式对话。事件与 `agent_runtime.AgentSession.chat()` 一一对应：
        start / compact / tool_call / tool_result / token / done / error
    **前端不需要懂 agent 内部结构**——照着 type 渲染即可。

    ⚠️ 身份取自 `Authorization` 头，**不接受请求体里的 user**。
    """
    import json as _json
    info = _whoami(authorization, lw_sid)          # 令牌无效 → 401（在流开始前就返回）

    def gen():
        try:
            s = _session(info["external_id"], new=body.new,
                         conversation_id=body.conversation_id)
            # 首轮提问时给会话起个能认出来的名字（纯脚本截取，不调 LLM）。
            # 只改仍是默认名的会话——用户/之前已命名过的不动。
            try:
                cur = [c for c in s.mem.list_conversations(s.user_id, limit=100)
                       if c["id"] == s.conv_id]
                if cur and (cur[0].get("title") or "") in ("", "成本分析问答"):
                    t = " ".join(body.question.split())[:24]
                    if t:
                        s.mem.set_title(s.conv_id, t)
            except Exception:  # noqa: BLE001
                pass          # 起标题失败不该影响问答本身
            for ev in s.chat(body.question):
                yield f"data: {_json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield ("data: " + _json.dumps({"type": "error",
                                           "message": f"{type(e).__name__}: {e}"},
                                          ensure_ascii=False) + "\n\n")
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------------
# 多模型路由（赛题 6.2「多模型协作」）
#
# 三层成本阶梯：state 直答（0 token）→ 小模型 → 大模型，带校验与级联升级。
# 路由信号全部确定性，见 scripts/router.py。
# ---------------------------------------------------------------------------

class RouteIn(BaseModel):
    question: str


@app.post("/api/route", summary="多模型路由问答（含决策轨迹）")
def route_ask(body: RouteIn, dry_run: bool = Query(False)) -> Dict[str, Any]:
    """
    走完整阶梯并返回**决策轨迹**（为什么用了/没用大模型）。

    ⚠️ 这个端点是**公开**的（不要求令牌）：它只读 state 与共享知识，
       不写任何东西、不碰个人数据。若要接个人工作区再另说。

    `dry_run=1` 只看路由裁决，不调模型——用于演示与测试。
    """
    import router as RT
    if dry_run:
        d = RT.route(body.question)
        return {"level": d.level, "trace": [d.explain()], "dry_run": True}
    return RT.answer(body.question, verbose=False)


# ---------------------------------------------------------------------------
# 控制面板：个人工作区（上传 → 摄入 → 我的词条）
#
# ⚠️ 这里的写操作**只落在当前用户自己的工作区**（`data/users/<id>/ws/`）。
#    共享基线（代码仓库里的 wiki/ 与 raw/）**永不被用户上传改动**——
#    这是"唯一事实源"的底线。
#
# 摄入是**长任务**（LLM 编译 30-120 秒），故：
#    POST /api/control/ingest  →  SSE 流式返回进度（不是一个转圈等到超时）
# 与 `/api/chat` 同一套 SSE 模式，前端解析逻辑可复用。
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import uuid  # noqa: E402

from fastapi import File, Form, UploadFile  # noqa: E402

# 允许上传的后缀 —— 与 `ingest_raw.py` 的采集能力一致，且**白名单**而非黑名单
ALLOWED_EXT = {".md", ".txt", ".csv", ".xlsx", ".xls", ".docx", ".pdf", ".json"}
MAX_UPLOAD_BYTES = 40 * 1024 * 1024          # 单文件 40MB
MAX_FILES_PER_REQ = 200                       # 单次请求文件数上限


def _ws(external_id: str) -> Path:
    import paths
    return paths.ensure_workspace(external_id)


@app.get("/api/control/workspace", summary="个人工作区清单")
def control_workspace(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """当前用户的个人工作区清单（上传的资料 + 编译出的词条）。"""
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])

    def listing(sub: str) -> List[Dict[str, Any]]:
        d = ws / sub
        if not d.exists():
            return []
        out = []
        for p in sorted(d.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.suffix.lower() == ".txt" and p.with_suffix(".pdf").exists():
                continue                        # PDF 派生缓存，不重复列
            out.append({"path": p.relative_to(ws).as_posix(),
                        "size": p.stat().st_size,
                        "mtime": int(p.stat().st_mtime)})
        return out

    raw, wiki = listing("raw"), listing("wiki")
    return {"external_id": info["external_id"], "workspace": str(ws.name),
            "raw": raw, "wiki": wiki,
            "counts": {"raw": len(raw), "wiki": len(wiki)}}


@app.post("/api/control/upload", summary="上传文件到个人工作区")
async def control_upload(authorization: str = Header(default=""),
                         lw_sid: str = Cookie(default=""),
                         files: List[UploadFile] = File(default=[]),
                         paths: str = Form(default="")) -> Dict[str, Any]:
    """
    收文件到**个人工作区**的 inbox/。

    用 multipart 而非 base64：文件可达几十 MB，base64 会膨胀 33% 且更吃内存。
    `paths` 是前端传的、与 files 一一对应的相对路径（选文件夹时保留目录语义），
    只用来做**提示**，实际落位仍由采集器按内容推断主题。
    """
    import json as _json
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])

    if len(files) > MAX_FILES_PER_REQ:
        raise HTTPException(400, f"单次最多 {MAX_FILES_PER_REQ} 个文件，收到 {len(files)}")

    try:
        rel_paths = _json.loads(paths) if paths else []
    except _json.JSONDecodeError:
        rel_paths = []
    if not isinstance(rel_paths, list):
        rel_paths = []

    inbox = ws / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)

    saved, rejected = [], []
    for i, uf in enumerate(files):
        # ⚠️ 文件名只取 basename —— 防止 `../../etc/passwd` 写到工作区外
        name = Path(uf.filename or "").name.strip()
        if not name:
            rejected.append({"file": uf.filename or "(无名)", "reason": "文件名为空"})
            continue
        ext = Path(name).suffix.lower()
        if ext not in ALLOWED_EXT:
            rejected.append({"file": name,
                             "reason": f"不支持的类型 {ext or '(无后缀)'}；"
                                       f"允许：{'、'.join(sorted(ALLOWED_EXT))}"})
            continue
        data = await uf.read()
        if len(data) > MAX_UPLOAD_BYTES:
            rejected.append({"file": name,
                             "reason": f"超过 {MAX_UPLOAD_BYTES // 1024 // 1024}MB 上限"})
            continue
        # 重名时加序号后缀，不覆盖（工作区里宁可多留一份也别静默替换）
        dest = inbox / name
        if dest.exists():
            dest = inbox / f"{Path(name).stem}_{uuid.uuid4().hex[:6]}{ext}"
        dest.write_bytes(data)
        saved.append({"name": dest.name, "size": len(data),
                      "hint_path": rel_paths[i] if i < len(rel_paths) else ""})

    return {"saved": saved, "rejected": rejected,
            "inbox": saved and str((inbox).name) or "",
            "message": (f"已接收 {len(saved)} 个文件"
                        + (f"，拒绝 {len(rejected)} 个" if rejected else ""))}


def _sse(obj: Dict[str, Any]) -> str:
    import json as _json
    return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"


@app.post("/api/control/ingest", summary="对个人工作区跑摄入（SSE 流式）")
def control_ingest(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> StreamingResponse:
    """
    对个人工作区跑一次摄入（采集 → 独立 LLM 编译 → 个人收尾）。SSE 流式返回。

    ⚠️ 整个过程**只读写该用户的工作区**：
       - `--wiki-root` 指向工作区 ⇒ raw/wiki 的写入都落在那里
       - 收尾 **skip db 步** ⇒ 不往共享 SQLite 镜像写个人数据
       - 规范仍从代码仓库读 ⇒ 用户改不了规则
    """
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"])
    here = Path(__file__).resolve().parent.parent

    def gen():
        yield _sse({"type": "start", "workspace": ws.name,
                    "message": "开始处理你上传的资料"})
        cmd = [sys.executable, str(here / "scripts" / "ingest_agent.py"),
               "--inbox", "--wiki-root", str(ws), "--quiet"]
        try:
            # 用子进程而非同进程 import：摄入本来是"独立实例"设计，
            # 且它崩溃不该拖垮看板服务。
            # ⚠️ 必须显式沿用当前的数据根，否则子进程会用默认根，
            #    个人工作区就找不到了。
            env = dict(os.environ)
            env["LLM_WIKI_WS"] = str(ws)
            env["PYTHONIOENCODING"] = "utf-8"
            # 子进程必须沿用**当前生效的数据根**，否则它会退回默认根，
            # 于是 `--wiki-root` 指向的个人工作区就找不到了。
            # 直接继承 os.environ 已足够（服务本身也是从环境变量取根的）。
            # （数据根已由 os.environ 继承，无需另行设置）
            proc = subprocess.Popen(
                cmd, cwd=str(here), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            n = 0
            for line in proc.stdout:            # 逐行转发 —— agent 本来就在 print 进度
                s = (line or "").rstrip()
                if not s:
                    continue
                n += 1
                yield _sse({"type": "log", "line": s[:300]})
            proc.wait()
            ok = (proc.returncode == 0)
            yield _sse({"type": "done", "ok": ok, "returncode": proc.returncode,
                        "lines": n,
                        "message": ("摄入完成，已更新你的个人知识库" if ok
                                    else f"摄入中断（退出码 {proc.returncode}）。"
                                         f"已落盘的改动保留在工作区，可重跑。")})
        except Exception as e:  # noqa: BLE001
            yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no",
                                      "Connection": "keep-alive"})


@app.delete("/api/control/workspace", summary="删除个人工作区文件")
def control_delete(path: str = Query(..., description="相对工作区的路径"),
                   authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """删除个人工作区里的一个文件（资料或词条）。**不碰共享基线。**"""
    info = _whoami(authorization, lw_sid)
    ws = _ws(info["external_id"]).resolve()
    target = (ws / path).resolve()

    # ⚠️ 路径必须落在工作区内 —— 否则 `../` 能删到别人或共享库
    def _norm(x: Path) -> str:
        return str(x).replace("\\", "/").lower()
    if not _norm(target).startswith(_norm(ws) + "/"):
        raise HTTPException(400, "路径越出你的工作区")
    if not target.exists():
        raise HTTPException(404, f"不存在：{path}")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"deleted": path}


# ---------------------------------------------------------------------------
# 文档导出（Word / PDF）
# ---------------------------------------------------------------------------

class ExportIn(BaseModel):
    md: str = ""              # 源 markdown 的**名称**（限以下白名单目录内）
    kind: str = "report"      # 仅 report（技术方案/评测报告是赛题交付物，系离线产出，不经站点导出）
    fmt: str = "both"         # docx | pdf | both
    title: str = ""
    charts: bool = False      # 是否重绘并嵌入图表（赛题 5.1.3「图表嵌入」）


class GenerateIn(BaseModel):
    """
    按**分析主题**生成报告（赛题 5.1.3 的核心交互）。

        theme  monthly   月度成本分析 —— `period` 忽略，用 `month`
               quarterly 季度成本分析 —— `period` 形如 `2026-Q2`
               topic     专题分析     —— `period` 是要素名（直接材料/…）

    ⚠️ 生成会调用大模型写叙述段，故**必须登录**（与导出不同）：
       导出只读已有文件，生成要花 token。
    """
    product: str
    month: str = "2026-06"
    theme: str = "monthly"
    period: str = ""


@app.get("/api/themes", summary="可选的分析主题")
def themes() -> Dict[str, Any]:
    """供前端渲染主题选择器——**选项由服务端给**，不在前端硬编。"""
    import period_agg as PA
    import report_build as RB
    return {
        "themes": [{"id": t, "name": RB.THEME_ZH[t],
                    "period_label": {"monthly": "分析月份",
                                     "quarterly": "分析季度",
                                     "topic": "专题对象"}[t],
                    "period_options": (
                        PA.AVAILABLE_QUARTERS if t == "quarterly"
                        else (RB.TOPIC_ELEMENTS if t == "topic" else []))}
                   for t in ("monthly", "quarterly", "topic")],
    }


@app.post("/api/control/generate", summary="按分析主题生成报告")
def control_generate(body: GenerateIn,
                     authorization: str = Header(default=""),
                     lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    生成一份报告（不调 LLM 之外的副作用；产物落在 `reports/`）。

    ⚠️ **必须登录**：生成要用大模型，是花钱的操作。导出（只读已有文件）
       才允许免登录到那个程度——两者成本不同，权限也不该一样。
    """
    info = _whoami(authorization, lw_sid)
    if body.theme not in ("monthly", "quarterly", "topic"):
        raise HTTPException(400, f"未知分析主题：{body.theme}")
    import period_agg as PA

    if body.theme == "quarterly":
        q = body.period or _quarter_of_month(body.month)
        if q not in PA.AVAILABLE_QUARTERS:
            raise HTTPException(400, f"该季度无数据：{q}（可选 {PA.AVAILABLE_QUARTERS}）")
        out_name = f"{body.product}_{q}.md"
    elif body.theme == "topic":
        if not body.period:
            raise HTTPException(400, "专题分析需要指定对象（如「直接材料」）")
        out_name = f"{body.product}_{body.month}_专题{body.period}.md"
    else:
        out_name = f"{body.product}_{body.month}.md"

    import report_build as RB
    try:
        r = RB.build(body.product, body.month, use_llm=True, verbose=False,
                     theme=body.theme, period=body.period)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"生成失败：{type(e).__name__}: {e}")

    out = REPORTS / out_name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(r["report"], encoding="utf-8", newline="\n")
    return {"ok": True, "file": out_name, "theme": body.theme,
            "stats": r["stats"]}


def _quarter_of_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


@app.get("/api/control/exports", summary="可导出的文档清单")
def control_exports(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """列出可导出的报告。"""
    _whoami(authorization, lw_sid)
    reps = sorted(REPORTS.glob("*.md")) if REPORTS.exists() else []
    return {"reports": [{"name": p.name, "stem": p.stem,
                         "size": p.stat().st_size} for p in reps]}


@app.post("/api/control/export", summary="导出 Word/PDF")
def control_export(body: ExportIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> FileResponse:
    """
    导出报告为 Word / PDF。**同步返回文件**（导出通常 3-15 秒，可接受）。

    ⚠️ 源文件必须在**白名单目录**内（reports/），
       且文件名只取 basename —— 否则 `../../.env` 之类能把任意文件读走。
    """
    import export_doc
    _whoami(authorization, lw_sid)

    if body.kind != "report":
        raise HTTPException(400, f"未知的导出类型：{body.kind}")
    # ⚠️ 先挡空名字：`REPORTS / Path("").name` == `REPORTS / ""` == **目录本身**，
    #    而目录也 `exists()`，于是守卫放行、下一步读目录崩成 500。
    #    改判 `is_file()` 并显式拒绝空名，让缺参数的请求得到 400 而不是 500。
    name = Path(body.md).name
    if not name:
        raise HTTPException(400, "缺少报告文件名")
    src = REPORTS / name

    if not src.is_file():
        raise HTTPException(404, f"源文件不存在：{name}")
    if body.fmt not in ("docx", "pdf", "both"):
        raise HTTPException(400, "fmt 只能是 docx / pdf / both")

    out_dir = WIKI_ROOT / "exports"
    try:
        outs = export_doc.export(src, body.fmt, out_dir,
                                 title=body.title or src.stem,
                                 with_charts=body.charts)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"导出失败：{type(e).__name__}: {e}")

    # 单文件时直接回文件；both 时回 zip 更省事——这里先回第一个（PDF 优先）
    pick = next((p for p in outs if p.suffix == ".pdf"), outs[0])
    media = {"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "pdf": "application/pdf"}[pick.suffix.lstrip(".")]
    return FileResponse(str(pick), media_type=media, filename=pick.name)


# ---------------------------------------------------------------------------
# 整改闭环（赛题模块四）：RPA 调度 + 消息推送 + 任务追踪
#
# ⚠️ 对接的是赛题的 **mock RPA 服务**（:8090），微信推送是**模拟**的。
#    mock 没起时不假装成功——如实返回 degraded，前端显示"未送达"。
# ---------------------------------------------------------------------------

class DispatchIn(BaseModel):
    report: str = ""          # 报告文件名（限 reports/ 内）
    month: str = "2026-05"
    product: str = "银黄口服液"


@app.get("/api/rpa/health", summary="模拟 RPA 服务探活")
def rpa_health() -> Dict[str, Any]:
    """模拟 RPA 服务探活。前端据此显示"下游系统已连接/未连接"。"""
    import rpa_client
    return rpa_client.RPAClient().health()


@app.get("/api/rpa/stats", summary="下游工单系统整体统计")
def rpa_stats(authorization: str = Header(default=""),
              lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """下游服务的总数 / 按状态 / 按优先级 / 通知数。"""
    _whoami(authorization, lw_sid)
    import rpa_client
    return rpa_client.RPAClient().stats()


@app.get("/api/rpa/task/{task_id}", summary="单个整改任务详情（含状态流转）")
def rpa_task(task_id: str, authorization: str = Header(default=""),
             lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    查单个任务。

    ⭐ 比列表查询多的是 **`status_history`**——任务的状态流转时间线。
       列表只给"当前是什么状态"，这里给"什么时候变的"。
    """
    _whoami(authorization, lw_sid)
    import rpa_client
    return rpa_client.RPAClient().get_task(task_id)


class NotifyIn(BaseModel):
    recipient: str
    department: str = ""
    message: str


@app.post("/api/rpa/notify", summary="手动补发微信通知")
def rpa_notify(body: NotifyIn, authorization: str = Header(default=""),
               lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    **补发**一条微信通知。

    ⚠️ 正常流程用不到它——派发任务时会自动发通知。
       这个端点只在"自动通知没送达"时人工补发。
    """
    _whoami(authorization, lw_sid)
    import rpa_client
    return rpa_client.RPAClient().notify(body.recipient, body.department,
                                         body.message)


@app.get("/api/rpa/tasks", summary="整改任务追踪看板数据")
def rpa_tasks(authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    任务追踪看板数据（赛题 5.4.3）：
    已生成数 / 已送达数 / 已确认数 + 任务明细。
    """
    _whoami(authorization, lw_sid)
    import rpa_client
    c = rpa_client.RPAClient()
    r = c.list_tasks()
    if not r["ok"]:
        return {"ok": False, "error": r["error"], "tasks": [],
                "counts": {"generated": 0, "delivered": 0, "confirmed": 0}}
    tasks = c.extract_tasks(r)
    counts = c.summarize_status(tasks)
    return {"ok": True, "tasks": tasks, "counts": counts,
            "status_zh": rpa_client.STATUS_ZH}


@app.post("/api/rpa/dispatch", summary="下发整改任务到责任人")
def rpa_dispatch(body: DispatchIn, authorization: str = Header(default=""), lw_sid: str = Cookie(default="")) -> Dict[str, Any]:
    """
    把某份报告第六章的「整改任务清单」派发到模拟 RPA 服务。

    ⚠️ **不从零生成任务** —— 直接搬报告 6.4 表格里已有的任务。
    否则会出现"报告里写的"与"实际发出去的"不一致，那是数据源分裂。
    """
    _whoami(authorization, lw_sid)
    import rpa_client

    # 同 control_export：空 `report` 会让 `REPORTS / ""` 退化成目录本身，
    # 只查 exists() 会放行、随后读目录崩成 500。先挡空名、再判 is_file()。
    name = Path(body.report).name
    if not name:
        raise HTTPException(400, "缺少报告文件名（report 字段）")
    src = REPORTS / name
    if not src.is_file():
        raise HTTPException(404, f"报告不存在：{name}")

    payloads = rpa_client.RPAClient.parse_report_tasks(
        src.read_text(encoding="utf-8"), body.month, body.product)
    if not payloads:
        raise HTTPException(400, "该报告的 6.4 整改任务清单为空，无可派发任务")

    rep = rpa_client.RPAClient().dispatch(payloads)
    return rep


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------

if STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(str(STATIC / "index.html"))


def main() -> int:
    import argparse
    import uvicorn
    ap = argparse.ArgumentParser(description="制药成本分析看板后端（只读）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true",
                    help="改代码自动重启（**仅开发用**；生产别带，见下）")
    a = ap.parse_args()

    print(f"看板: http://{a.host}:{a.port}/")
    print(f"文档: http://{a.host}:{a.port}/docs")
    if a.reload:
        # ⚠️ 为什么要有这个开关（血泪）：
        #    `python server/app.py` 不带 --reload 时，进程跑的是**启动那一刻**的代码。
        #    实测踩过：改了 app.py 后没重启，接口行为一直是旧的，
        #    于是对着真实的旧服务反复调试"为什么 401"——查了令牌哈希、库路径、
        #    盘符、解释器，全对，白花好几轮。**陈旧进程会制造假象证据。**
        #
        # ⚠️ uvicorn 的 reload 要求传**导入字符串**而不是 app 对象，
        #    否则它无法在子进程里重新导入。所以要把它能看到 app 的目录
        #    （server/）加进 sys.path，再传 "app:app"。
        #    reload_dirs 限定在 server/，免得编辑器保存 reports/ 之类也触发重启。
        print("（开发模式：改 server/ 下的代码会自动重启）")
        import os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        uvicorn.run("app:app", host=a.host, port=a.port, log_level="info",
                    reload=True, reload_dirs=[os.path.dirname(os.path.abspath(__file__))])
    else:
        uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

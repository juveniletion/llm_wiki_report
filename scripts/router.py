# -*- coding: utf-8 -*-
"""
router.py — **请求路由 + 模型级联**（赛题 6.2「多模型协作」）

三层成本阶梯 —— 顺序不能颠倒
-----------------------------
    ① state 直答    0 token    命中唯一记录 → 脚本直接答，**根本不进模型**
    ② 小模型        便宜       只读 / 单源 / 问答类   （Qwen2.5-7B）
    ③ 大模型        贵         写入 / 跨源 / 报告 / RPA（deepseek-chat）

⚠️ **多数人只想到 ③（换个便宜模型），其实 ① 和 ② 省得多得多。**
   你的成本大头是**上下文长度**，不是模型档位。为了换便宜模型而放大上下文，
   是捡芝麻丢西瓜。所以：能不用模型就别用；要用就把上下文压到最小。

设计铁律：**路由是脚本，不是模型。**
------------------------------
"要不要用大模型"这个判断，绝不能交给 LLM 去"理解意图"——那是一个
**不可复算的判断**：判错了不会报错，只会给一个**悄悄变差的答案**。
（本库实测过同款事故：把告警判定交给 LLM 自由写，2.84% 被写成"超阈值"。）

⇒ 路由只看**确定性信号**，每个决策都带 `reasons` 可打日志、可审计。

级联升级（cascade escalation）
-----------------------------
小模型答完**必须过校验**（它引用的每个数字都要能在 state/wiki 里查到）。
不过关就自动升大模型。**没有这道校验，小模型就是纯风险**——
省下的 token 换来的是没人发现的错数，那是这个项目最不能要的东西。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

SMALL_MODEL_ENV = "SILICONFLOW_MODEL"
SMALL_MODEL_DEFAULT = "Qwen/Qwen2.5-7B-Instruct"
BIG_MODEL_ENV = "AGENT_MODEL"
BIG_MODEL_DEFAULT = "deepseek-chat"

# 决策档位
L_STATE, L_SMALL, L_LARGE = "state", "small", "large"


# =============================================================================
# 一、确定性信号
#
# 每个信号只回答一个是非/计数问题，全部可复算。**不用 LLM 判意图。**
# =============================================================================

# 写入类动词 —— 会改 wiki/raw 或产出交付物
WRITE_WORDS = (
    "生成报告", "出报告", "导出", "写进", "入库", "更新知识库", "新增词条",
    "编译", "摄入", "派发", "下发", "整改任务", "生成任务", "重新生成",
    "更新看板", "存档",
)
# 需要推理的词 —— 问"为什么/趋势/建议"的，小模型答不好
REASON_WORDS = (
    "为什么", "原因", "归因", "分析", "对比", "比较", "差异", "趋势",
    "建议", "评价", "影响", "预测", "判断", "怎么", "如何",
)

TIME_RE = re.compile(r"20\d{2}[-/年]?\d{1,2}(?:[-/月])?")
NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")


@dataclass
class Signal:
    """一个路由信号的判定结果。"""
    name: str
    value: Any
    why: str

    def __str__(self) -> str:
        return f"{self.name}={self.value}（{self.why}）"


def _db_path() -> Path:
    import paths
    return paths.knowledge_db()


def _connect():
    """
    以**只读**方式打开知识库镜像。

    ⚠️ `mode=ro` 不是可选的严谨，而是职责约束：取数层**只读**。
       写库的只有 `db_build.py`（它自己重建镜像）。
    """
    import sqlite3
    p = _db_path()
    if not p.exists():
        raise FileNotFoundError(
            f"知识库镜像不存在：{p}\n  先跑：python scripts/db_build.py")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def db_ready() -> Tuple[bool, str]:
    """
    库在不在、**新不新鲜**。

    ⚠️ 过期校验不能省：`mirror_metrics` 是从 `state/metrics.json` 镜像来的，
       而 state 又是从 wiki 蒸馏的。wiki 改了、库没重建，库里就是**旧值**——
       它看起来完好无损，却和当前知识库对不上。
       **"正确但过时"是最难发现的错**，所以这里机械比对签名，过期就拒绝用。
    """
    p = _db_path()
    if not p.exists():
        return False, f"知识库镜像不存在（跑 python scripts/db_build.py）"
    try:
        import state as S
        with _connect() as con:
            row = con.execute(
                "SELECT value FROM meta WHERE key='wiki_signature'").fetchone()
        db_sig = (row["value"] if row else "") or ""
        cur_sig = (S.load() or {}).get("wiki_signature", "")
        if db_sig and cur_sig and db_sig != cur_sig:
            return False, (f"知识库镜像已过期（库 {db_sig[:8]} ≠ 当前 {cur_sig[:8]}）"
                           f"——重建：python scripts/db_build.py")
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"知识库镜像不可用：{type(e).__name__}: {e}"


def _db_rows(entity: str = "", metric: str = "", period: str = "",
             limit: int = 200) -> List[Dict[str, Any]]:
    """
    从 `mirror_metrics` 取数（子串匹配，与 `state.lookup` 语义一致）。

    ⚠️ 刻意**对齐 state.lookup 的语义**（子串、空条件不限），
       这样换成读库不会悄悄改变行为——只是换了数据源，判定逻辑不变。
    """
    sql = ("SELECT entity,metric,period,value,unit FROM mirror_metrics WHERE 1=1")
    args: List[Any] = []
    if entity:
        sql += " AND entity LIKE ?"
        args.append(f"%{entity}%")
    if metric:
        sql += " AND metric LIKE ?"
        args.append(f"%{metric}%")
    if period:
        sql += " AND period LIKE ?"
        args.append(f"%{period}%")
    sql += " LIMIT ?"
    args.append(limit)
    with _connect() as con:
        return [dict(r) for r in con.execute(sql, args).fetchall()]


@dataclass
class Vocab:
    ent: set
    met: set
    per: set


def _state_vocab() -> Vocab:
    """
    从**数据库**里取三套词表：实体 / 指标 / 期间。

    ⚠️ 只做"词表"，不做语义理解——子串匹配本就是取数层的语义。
       从 DB 取而不是扫 JSON：库上有 `idx_met_em` / `idx_met_per` 索引，
       且 DISTINCT 在 SQLite 里比在 Python 里集合去重省内存。
    """
    with _connect() as con:
        ent = {r[0] for r in con.execute(
            "SELECT DISTINCT entity FROM mirror_metrics WHERE entity <> ''")}
        met = {r[0] for r in con.execute(
            "SELECT DISTINCT metric FROM mirror_metrics WHERE metric <> ''")}
        per = {r[0] for r in con.execute(
            "SELECT DISTINCT period FROM mirror_metrics WHERE period <> ''")}
    return Vocab(ent, met, per)


def _find_in(text: str, vocab: set) -> List[str]:
    """在提问里找出词表中出现的项（长的优先，避免"单位成本"被"成本"截断）。"""
    hits = [v for v in vocab if v and v in text]
    hits.sort(key=len, reverse=True)
    # 去掉被更长命中包含的短项
    out: List[str] = []
    for h in hits:
        if not any(h != o and h in o for o in out):
            out.append(h)
    return out


def sig_write(question: str, **_k: Any) -> Signal:
    """信号①：**写入意图** —— 会不会改知识库 / 出交付物。"""
    hit = [w for w in WRITE_WORDS if w in question]
    if hit:
        return Signal("写入意图", True, f"命中写入类词：{'、'.join(hit)}")
    return Signal("写入意图", False, "无写入类词")


def _lookup(question: str, vocab: Optional[Vocab] = None,
            limit: int = 200) -> Tuple[List[Dict[str, Any]], List[str]]:
    """抽提问里的 实体/指标/期间，从库里查。返回 (行, 命中的词)。"""
    v = vocab or _state_vocab()
    e_hits = _find_in(question, v.ent)
    m_hits = _find_in(question, v.met)
    p_hits = _find_in(question, v.per)
    rows = _db_rows(entity=e_hits[0] if e_hits else "",
                    metric=m_hits[0] if m_hits else "",
                    period=p_hits[0] if p_hits else "",
                    limit=limit)
    return rows, [*e_hits, *m_hits, *p_hits]


def sig_state_direct(question: str, *, vocab=None, **_k: Any) -> Signal:
    """
    信号②：**库能否直答** —— 命中**唯一**记录时，连模型都不用。

    ⚠️ 判据刻意保守：**只有恰好 1 条**才算能直答。
       命中多条说明有歧义（"人工"能匹配到几十条），那就该交给模型去读，
       不能脚本随便挑一条——**挑错了就是错数**。
    """
    v = vocab or _state_vocab()
    if not _find_in(question, v.met):
        return Signal("库直答", False, "没识别出指标")

    rows, _ = _lookup(question, v, limit=3)
    if len(rows) == 1:
        r = rows[0]
        return Signal("库直答", True,
                      f"唯一命中：{r['entity']}/{r['metric']}"
                      f"/{r['period']}={r['value']}")
    return Signal("库直答", False,
                  f"命中 {len(rows)} 条，有歧义" if rows else "无命中")


def sig_sources(question: str, **_k: Any) -> Signal:
    """信号③：**数据源数量** —— 单源给小模型，跨源交叉给大模型。

    确定性代理：提问里出现了**几个不同产品**。多产品要横向对比，
    小模型容易把两边的数串起来（本库最怕的"张冠李戴"）。
    """
    products = ("银黄口服液", "板蓝根颗粒", "六味地黄胶囊")
    hit = [p for p in products if p in question]
    n = len(hit)
    return Signal("数据源数", n,
                  f"提及 {'、'.join(hit)}" if hit
                  else "未点名产品（视作共享基线单源）")


def sig_output_type(question: str, **_k: Any) -> Signal:
    """信号④：**输出类型** —— 问答 / 报告 / RPA 任务。"""
    if any(w in question for w in ("报告", "导出", "PDF", "Word", "pdf", "word")):
        return Signal("输出类型", "report", "命中报告/导出类词")
    if any(w in question for w in ("整改", "派发", "下发", "任务", "工单")):
        return Signal("输出类型", "rpa", "命中整改/任务类词")
    return Signal("输出类型", "qa", "默认问答")


SIGNALS = (sig_write, sig_state_direct, sig_sources, sig_output_type)


# =============================================================================
# 二、路由
# =============================================================================

@dataclass
class Decision:
    level: str                                   # state | small | large
    reasons: List[Signal] = field(default_factory=list)
    question: str = ""
    payload: Any = None                          # state 直答时的命中行 / 小模型用的上下文

    @property
    def is_direct(self) -> bool:
        return self.level == L_STATE

    def explain(self) -> str:
        head = {L_STATE: "① 知识库直答（0 token）",
                L_SMALL: "② 小模型（Qwen2.5-7B）",
                L_LARGE: "③ 大模型（deepseek-chat）"}[self.level]
        lines = [f"裁决：{head}"]
        for s in self.reasons:
            lines.append(f"  · {s}")
        return "\n".join(lines)


def route(question: str, *, vocab=None) -> Decision:
    """
    确定性路由。规则按**风险从高到低**判——先排掉"必须用大模型"的，
    再看能不能省钱。

    ⚠️ 顺序很重要：写入类永远走大模型，**写权限不给弱模型**
       （小模型错误率更高，而写 wiki 是不可逆的，会污染唯一事实源）。
    """
    sigs: List[Signal] = [
        sig_write(question),
        sig_state_direct(question, vocab=vocab),
        sig_sources(question),
        sig_output_type(question),
    ]
    get = {s.name: s for s in sigs}

    # ① 写权限：大模型。没有例外。
    if get["写入意图"].value:
        return Decision(L_LARGE, sigs, question)

    # ② 交付物（报告 / RPA）：大模型。
    if get["输出类型"].value in ("report", "rpa"):
        return Decision(L_LARGE, sigs, question)

    # ③ 跨源交叉：大模型。
    if get["数据源数"].value >= 2:
        return Decision(L_LARGE, sigs, question)

    # ④ 需要推理：大模型（小模型答不好"为什么"）。
    if any(w in question for w in REASON_WORDS):
        return Decision(L_LARGE, sigs, question)

    # ⑤ 唯一命中：脚本直接答。
    if get["库直答"].value:
        rows, _ = _lookup(question, vocab, limit=3)
        return Decision(L_STATE, sigs, question, payload=rows[0] if rows else None)

    # ⑥ 其余：只读、单源、简单问答 → 小模型
    return Decision(L_SMALL, sigs, question)


# =============================================================================
# 三、模型调用
# =============================================================================

def _load_env() -> None:
    from dotenv import load_dotenv
    for p in [WIKI_ROOT, *WIKI_ROOT.parents]:
        env = p / ".env"
        if env.exists():
            load_dotenv(dotenv_path=env)
            return


def _client(kind: str):
    """建 LLM 客户端。kind ∈ {small, large}。"""
    import httpx
    from langchain_openai import ChatOpenAI

    _load_env()
    if kind == "small":
        key = os.getenv("SILICONFLOW_API_KEY", "")
        if not key:
            raise RuntimeError("未找到 SILICONFLOW_API_KEY（小模型不可用）")
        return ChatOpenAI(
            model=os.getenv(SMALL_MODEL_ENV, SMALL_MODEL_DEFAULT),
            api_key=key,
            base_url=os.getenv("SILICONFLOW_BASE_URL",
                               "https://api.siliconflow.cn/v1").rstrip("/"),
            temperature=0.1,
            http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
            http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
        )

    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise RuntimeError("未找到 DEEPSEEK_API_KEY（大模型不可用）")
    return ChatOpenAI(
        model=os.getenv(BIG_MODEL_ENV, BIG_MODEL_DEFAULT),
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        temperature=0.2,
        http_client=httpx.Client(headers={"Accept-Encoding": "identity"}),
        http_async_client=httpx.AsyncClient(headers={"Accept-Encoding": "identity"}),
    )


SMALL_SYSTEM = """你是中药一厂成本数据的查询助手。

**铁律（违反即不合格）：**
1. **你只能引用「参考数据」里出现过的数字。** 不许计算、不许推算、不许编造。
2. **只要参考数据里有相关记录，就必须把它们如实列出来**（按期间顺序），
   不要回答"没有该数值"。
3. **仅当参考数据里确实找不到相关记录时**，才回答「参考数据中没有该数值」。
4. 回答简短，直接列数值即可。

你只做**查询与转述**，不做分析、不做归因、不提建议。
"""


def build_context(question: str, *, vocab=None, limit: int = 6) -> str:
    """
    给**小模型**准备的极小上下文 —— **从数据库的关键数值表取**。

    ⚠️ 这是省钱的关键：只喂命中的那几条，**不喂整个 wiki / 不喂整个库**。
       小模型便宜在单价，喂全库一样会贵，而且更容易跑偏。

    取数顺序：
      ① 实体+指标+期间 一起匹配（最精确）
      ② 没结果就放宽成「只按指标」（实测多数提问只说指标，不说实体）
    """
    v = vocab or _state_vocab()
    rows, _ = _lookup(question, v, limit=limit)
    if not rows:                       # 放宽：只按指标找
        m = _find_in(question, v.met)
        if m:
            rows = _db_rows(metric=m[0], limit=limit)
    lines = [f"- {r['entity']} / {r['metric']} / {r['period']} = {r['value']}"
             f"{r.get('unit') or ''}" for r in rows[:limit]]
    return "\n".join(lines) if lines else "（无匹配记录）"


# =============================================================================
# 四、校验（小模型的唯一安全带）
# =============================================================================

def validate_numbers(answer: str, context: str) -> Tuple[bool, List[str]]:
    """
    答案里出现的**每个数字**，都必须在参考答案里能找到。

    ⚠️ 没有这道校验，小模型就是纯风险：省下的 token 换来的是**没人发现的错数**。
       这是本库一以贯之的底线——**错误必须能被机械地抓住**。

    返回 (是否通过, 无法溯源的数字列表)。
    """
    allowed = {n.replace(",", "") for n in NUM_RE.findall(context)}
    # 年份等无害数字不算（"2026年"这类）
    bad: List[str] = []
    for n in NUM_RE.findall(answer):
        n2 = n.replace(",", "")
        if n2 in allowed:
            continue
        if n2.endswith(".0"):
            n2 = n2[:-2]
        if n2 in allowed:
            continue
        # 允许纯年份
        if re.fullmatch(r"20\d{2}", n2):
            continue
        bad.append(n)
    return (not bad), bad


# 小模型"拒答"的典型措辞
REFUSAL_WORDS = ("没有该数值", "没有相关", "未找到", "无匹配", "无法确定",
                 "无法回答", "没有找到", "不存在该")


def is_refusal(answer: str) -> bool:
    """小模型是否在说"我没有这个数据"。"""
    return any(w in answer for w in REFUSAL_WORDS)


def validate_answer(answer: str, context: str) -> Tuple[bool, List[str]]:
    """
    小模型的完整校验：**既不能编，也不能该答不答**。

    两类失败，都能机械抓住：

    | 失败类型 | 表现 | 判据 |
    |:---|:---|:---|
    | **幻觉**（编数） | 说出了参考数据里没有的数 | `validate_numbers` |
    | **假阴性**（拒答） | 参考数据**明明有**，却说"没有该数值" | 拒答措辞 + 上下文非空 |

    ⚠️ 第二类是实测发现的：Qwen2.5-7B 面对"二厂单位成本"这样
       **上下文里有 6 行数据**的提问，仍然答"参考数据中没有该数值"。
       它没编造，但答案同样无用——而这种失败**光看数字校验是发现不了的**
       （没有数字，自然没有无法溯源的数字）。
       不修的话，用户得到的就是一句莫名其妙的"没有数据"。
    """
    ok, bad = validate_numbers(answer, context)
    if not ok:
        return False, [f"引用了无法溯源的数字：{'、'.join(bad)}"]

    has_rows = bool(context.strip()) and context.strip() != "（无匹配记录）"
    if has_rows and is_refusal(answer):
        return False, ["参考数据非空，却答『没有该数值』（该答没答）"]

    return True, []


# =============================================================================
# 五、执行（含级联升级）
# =============================================================================

def answer(question: str, *, vocab=None, verbose: bool = True) -> Dict[str, Any]:
    """
    跑完整个阶梯，返回带**完整决策轨迹**的结果。

    ⚠️ 返回的 `trace` 是核心产物之一：它回答「这次为什么用了/没用大模型」。
       没有它，路由就是个黑盒——而黑盒路由正是"不可审计的自主"。
    """
    # 库不可用或**已过期**时，绝不硬着头皮查——如实报出并附上重建命令。
    ready, why = db_ready()
    if not ready:
        return {"level": "unavailable", "answer": f"（无法取数：{why}）",
                "trace": [f"✗ 知识库镜像不可用：{why}"], "escalated": False}

    d = route(question, vocab=vocab)
    trace: List[str] = [d.explain()]

    # ---- ① 库直答（0 token）----
    if d.is_direct:
        r = d.payload or {}
        ans = (f"{r.get('entity','')} 的 {r.get('metric','')}"
               f"（{r.get('period','')}）为 {r.get('value','')}{r.get('unit') or ''}。")
        trace.append("→ 库中唯一命中，未调用任何模型")
        return {"level": L_STATE, "answer": ans, "trace": trace,
                "row": r, "escalated": False}

    ctx = build_context(question, vocab=vocab)

    # ---- ② 小模型 + 校验 + 升级 ----
    if d.level == L_SMALL:
        trace.append(f"→ 上下文 {len(ctx.splitlines())} 行"
                     f"（只喂命中记录，不喂全库）")
        try:
            llm = _client("small")
            reply = llm.invoke([("system", SMALL_SYSTEM),
                                ("user", f"## 参考数据\n{ctx}\n\n## 问题\n{question}")])
            txt = (reply.content or "").strip()
        except Exception as e:  # noqa: BLE001
            trace.append(f"→ 小模型失败（{type(e).__name__}），升级大模型")
            txt = ""

        if txt:
            ok, problems = validate_answer(txt, ctx)
            if ok:
                trace.append("→ 校验通过（数字可溯源、回答非拒答）")
                return {"level": L_SMALL, "answer": txt, "trace": trace,
                        "context": ctx, "escalated": False}
            trace.append(f"→ ⚠️ 校验不过（{'；'.join(problems)}），升级大模型")

    # ---- ③ 大模型 ----
    try:
        llm = _client("large")
        reply = llm.invoke([
            ("system", "你是中药一厂成本分析助手。只引用给定数据，不要编造数字。"),
            ("user", f"## 参考数据\n{ctx}\n\n## 问题\n{question}")])
        txt = (reply.content or "").strip()
    except Exception as e:  # noqa: BLE001
        return {"level": L_LARGE, "answer": f"（大模型调用失败：{type(e).__name__}: {e}）",
                "trace": trace, "escalated": d.level == L_SMALL}

    return {"level": L_LARGE, "answer": txt, "trace": trace, "context": ctx,
            "escalated": d.level == L_SMALL}


# =============================================================================
# 六、自测（不联网，只验路由是确定的）
# =============================================================================

def _selftest() -> int:
    cases = [
        ("银黄口服液 2026-05 的单位成本是多少", "只读单源事实查询"),
        ("银黄口服液和六味地黄胶囊哪个成本高", "跨源对比"),
        ("材料成本为什么上涨", "需要推理"),
        ("帮我生成这个月的报告", "写入/交付物"),
        ("把整改任务派发下去", "RPA"),
        ("导出一份 PDF", "交付物"),
    ]
    print("=" * 74)
    print("路由自测（只看裁决是否落在预期档位）")
    print("=" * 74)
    for q, expect in cases:
        d = route(q)
        print(f"\n提问：{q}")
        print(f"  预期：{expect}")
        print("  " + d.explain().replace("\n", "\n  "))
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="请求路由 + 模型级联")
    ap.add_argument("--ask", help="提问（走完整阶梯）")
    ap.add_argument("--route-only", action="store_true", help="只看路由裁决，不调模型")
    ap.add_argument("--selftest", action="store_true", help="跑路由自测")
    a = ap.parse_args()

    if a.selftest or not a.ask:
        return _selftest()

    if a.route_only:
        print(route(a.ask).explain())
        return 0

    r = answer(a.ask)
    print("=" * 74)
    for line in r["trace"]:
        print(line)
    print("=" * 74)
    print(r["answer"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

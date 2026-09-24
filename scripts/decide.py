# -*- coding: utf-8 -*-
"""
decide.py — **Agent 自主决策**：该生成报告，还是只更新看板？

赛题 6.2 原文：
    Agent自主决策（系统自动判断"需要生成报告还是仅更新看板"）

为什么这个判断必须由脚本做
--------------------------
"要不要花 LLM 成本重写一份报告"是一个**可判定**的问题——
知识库有没有实质变化，是能算出来的。把它交给 LLM 去"理解意图"，
就变成了不可复算的判断：判错的代价不是报错，而是**该出的报告没出**
（用户以为看板刷新了就没事）。这正是本项目一贯避免的那类失败。

所以：**信号全部确定性，决策可打日志、可审计。**

四个信号
--------
| 信号 | 来源 | 含义 |
|:---|:---|:---|
| ① 知识库变了吗 | `state.verify()` | wiki 签名 vs 快照签名 |
| ② 数值变了吗 | `state.verify()` 的 changed | 具体哪些关键数值动了 |
| ③ 报告落后了吗 | 报告 mtime vs wiki mtime | 报告比知识库旧 |
| ④ 报告齐吗 | `reports/*.md` | 该有的（产品×月）报告有没有 |

决策规则（自上而下，命中即止）
------------------------------
    有数值变化   → 生成报告（结论可能变，必须重写）
    知识库变了   → 生成报告（内容变了，报告要跟上）
    报告缺失     → 生成报告
    报告落后     → 生成报告
    都没变       → 仅更新看板（省下 LLM 成本）

**顺序有意义**：先看"变没变"再看"缺不缺"——
有变化时即使报告齐全也要重写，那才是这条规则的重点。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

# 决策结果
GENERATE = "generate_report"       # 需要重新生成报告
DASHBOARD = "update_dashboard"     # 仅更新看板


@dataclass
class Signal:
    """一个决策信号的判定结果。"""
    name: str
    fired: bool                        # 是否触发了某个结论
    detail: str

    def __str__(self) -> str:
        return f"{'●' if self.fired else '○'} {self.name}：{self.detail}"


@dataclass
class Decision:
    action: str
    reasons: List[Signal] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_generate(self) -> bool:
        return self.action == GENERATE

    @property
    def label(self) -> str:
        return "生成报告" if self.is_generate else "仅更新看板"

    def explain(self) -> str:
        head = (f"裁决：**{self.label}**"
                + (f"（{len(self.reasons)} 项信号）" if self.reasons else ""))
        lines = [head]
        for s in self.reasons:
            lines.append(f"  {s}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "label": self.label,
            "reasons": [{"name": s.name, "fired": s.fired, "detail": s.detail}
                        for s in self.reasons],
            "meta": self.meta,
        }


# ---------------------------------------------------------------------------
# 信号
# ---------------------------------------------------------------------------

def sig_kb_changed(root: Path) -> Signal:
    """
    信号①：**知识库变了吗**——`state.verify()` 比对 wiki 签名与快照签名。

    stale=True 表示 wiki 自上次重建 state 之后被改过，
    也就是"知识库有变化"的确定证据。
    """
    try:
        import state as S
        v = S.verify(root / "wiki")
        if v.get("stale"):
            return Signal("知识库已变化", True, "wiki 自上次快照后已改动")
        return Signal("知识库已变化", False, "与快照一致，无改动")
    except Exception as e:  # noqa: BLE001
        # 查不出来时**不能当成"没变"**——那会导致该出的报告没出。
        # 保守起见按"变了"处理，宁可多花一次 LLM 成本。
        return Signal("知识库已变化", True, f"无法判定（{type(e).__name__}），保守起见按已变化处理")


def sig_values_changed(root: Path) -> Signal:
    """
    信号②：**关键数值变了吗**——这是最强的信号。

    与①的区别：wiki 可能只是改了措辞（措辞变化不影响结论），
    而**数值变化一定影响结论**，必须重写报告。
    """
    try:
        import state as S
        v = S.verify(root / "wiki")
        changed = v.get("changed") or []
        added = v.get("added") or []
        removed = v.get("removed") or []
        n = len(changed) + len(added) + len(removed)
        if n:
            bits = []
            if changed:
                bits.append(f"{len(changed)} 项数值变动")
            if added:
                bits.append(f"{len(added)} 项新增")
            if removed:
                bits.append(f"{len(removed)} 项移除")
            return Signal("关键数值已变化", True, "、".join(bits)
                          + "：结论可能已不同，报告必须重写")
        return Signal("关键数值已变化", False, "无数值变动")
    except Exception as e:  # noqa: BLE001
        return Signal("关键数值已变化", False, f"无法判定（{type(e).__name__}）")


def sig_reports_missing(root: Path, month: str,
                        products: List[str]) -> Signal:
    """
    信号③：**该有的报告齐吗**——逐 (产品 × 月份) 检查。

    报告文件名契约：`reports/<产品>_<月份>.md`（见 report_build 的 --out 默认值）。
    """
    reps = root / "reports"
    missing = [p for p in products
               if not (reps / f"{p}_{month}.md").exists()]
    if missing:
        return Signal("报告缺失", True,
                      f"{len(missing)} 份未生成：" + "、".join(missing[:3])
                      + ("…" if len(missing) > 3 else ""))
    return Signal("报告缺失", False, f"{len(products)} 份报告齐全")


def sig_reports_stale(root: Path) -> Signal:
    """
    信号④：**报告落后于知识库吗**。

    ⚠️ **不用 mtime 比**（这是本模块最初写错的地方，实测撞到）：
       · 同一秒内的两次写入，mtime 分不出先后 —— 实测中改成
         "报告 13:37:46 vs 知识库 13:37:54" 这种 8 秒差里的判等，
         恢复文件后仍误报"报告落后"。
       · 更根本的是：`touch`、复制文件、git checkout 都会改 mtime
         而**内容没变**，那不该触发重写报告（白花 LLM 成本）。

    ⇒ 改问语义问题：**状态快照与当前 wiki 是否一致**。
       一致 ⇒ 知识库自上次流水线跑完后没被改过 ⇒ 报告（那次跑出来的）也还新鲜。
       不一致 ⇒ 知识库动过 ⇒ 报告该重写。
       这与信号①②同源，但**结论不同**：①②问"要不要重算"，
       这里问"已有产物还作不作数"。
    """
    try:
        import state as S
        v = S.verify(root / "wiki")
        if v.get("stale"):
            return Signal("报告落后", True,
                          "知识库自上次快照后有改动，已有报告可能已过时")
        return Signal("报告落后", False, "状态快照与知识库一致，已有报告仍新鲜")
    except Exception as e:  # noqa: BLE001
        return Signal("报告落后", False, f"无法判定（{type(e).__name__}）")


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------

def decide(root: Optional[Path] = None, month: str = "2026-05",
           products: Optional[List[str]] = None) -> Decision:
    """
    跑四个信号，得出结论。

    :param root: 知识库根（默认本库）
    :param month: 分析月份
    :param products: 要检查的产品；不给则用 `report_facts.list_products()`
    """
    r = Path(root) if root else WIKI_ROOT
    if products is None:
        try:
            import report_facts as RF
            products = RF.list_products()
        except Exception:  # noqa: BLE001
            products = []

    sigs = [
        sig_values_changed(r),      # 最强信号放最前
        sig_kb_changed(r),
        sig_reports_missing(r, month, products),
        sig_reports_stale(r),
    ]

    # 命中即止：自上而下，第一个 fired 的信号决定结论
    hit = next((s for s in sigs if s.fired), None)
    action = GENERATE if hit else DASHBOARD

    return Decision(action=action, reasons=sigs, meta={
        "root": str(r), "month": month, "products": products,
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "triggered_by": hit.name if hit else None,
    })


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="自主决策：生成报告 or 只更新看板")
    ap.add_argument("--root", help="知识库根（默认本库）")
    ap.add_argument("--month", default="2026-05")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    a = ap.parse_args()

    d = decide(Path(a.root) if a.root else None, a.month)
    if a.json:
        import json
        print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
    else:
        print("=" * 72)
        print(f"自主决策  ·  {a.month}")
        print("=" * 72)
        print(d.explain())
        print()
        print(f"→ 建议动作：{'重新生成 6 章报告' if d.is_generate else '刷新看板即可（无需调用 LLM）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

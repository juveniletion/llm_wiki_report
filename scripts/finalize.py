# -*- coding: utf-8 -*-
"""
finalize.py — **确定性收尾流水线**（脚本做，LLM 不参与）

设计原则
--------
> **能用脚本代码做的事情，就不用 LLM 做。**

本库反复踩过这条原则的反面：把机械步骤做成"LLM 可以调的工具"，
结果 LLM **忘了调**（或调错时机），产物就静默地不完整。
  例：`rebuild_state` 曾是工具 → agent 崩在最后一步 → state 从未重建。

所以本模块把**全部机械步骤**抽出来，由调用方在语义工作结束后**无条件执行**：

    ① 重建 state（关键数值快照）      → scripts/state.py
    ② 同步 DB 镜像（SQLite）          → scripts/db_build.py
    ③ 跑机械校验（结构层）            → scripts/check_evidence.py

三步**幂等、可重入、无副作用**（除各自的派生输出），且**不碰权威区**。

三种用法
--------
    # 1. 独立跑（cron / CI / 手动）
    python scripts/finalize.py

    # 2. 从摄入 agent 调用（agent 结束后自动执行）
    from finalize import finalize
    report = finalize()

    # 3. 只跑其中一步
    python scripts/finalize.py --only state
    python scripts/finalize.py --only db
    python scripts/finalize.py --only check

返回值
------
`finalize()` 返回一个可 JSON 序列化的 dict，含每步的状态与**给 LLM 读的问题摘要**。
调用方可据此决定是否需要让 LLM 做语义修正（如修结构问题）。
"""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
from paths import default_db  # noqa: E402

DEFAULT_DB = default_db()

STEPS = ("state", "db", "graph", "check", "math")


# ---------------------------------------------------------------------------
# 步骤 1：重建 state
# ---------------------------------------------------------------------------

def step_state(quiet: bool = False) -> Dict[str, Any]:
    """
    重建关键数值快照，并返回**本次的值变化清单**（关键数值变更日志）。
    这是唯一需要读旧值的一步，所以用 import 而非 subprocess。
    """
    try:
        import importlib
        import state as S
        importlib.reload(S)

        old = S.load() or {"metrics": []}
        oid = {m["id"]: m for m in old.get("metrics", [])}

        doc = S.build(verbose=False)
        new = {m["id"]: m for m in doc["metrics"]}

        changed = []
        for k in sorted(set(oid) & set(new)):
            a, b = oid[k], new[k]
            if abs(float(a["value"]) - b["value"]) > 1e-9:
                changed.append({"key": k, "was": a["value"], "now": b["value"]})
        added = sorted(set(new) - set(oid))
        removed = sorted(set(oid) - set(new))

        return {
            "ok": True, "count": doc["count"], "signature": doc["wiki_signature"],
            "changed": changed, "added": added, "removed": removed,
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 步骤 2：同步 DB 镜像
# ---------------------------------------------------------------------------

def step_db(db_path: Path = DEFAULT_DB, quiet: bool = False) -> Dict[str, Any]:
    """
    把 wiki/raw/state 镜像进 SQLite。

    ⚠️ 只碰镜像区（mirror_*）。权威区（users/conversations/messages/user_state）
       由 db_build 内部的行数自检保证不被改变。
    """
    try:
        import importlib
        import db_build as B
        importlib.reload(B)

        st = B.build(Path(db_path), verbose=False)
        return {
            "ok": True, "db": str(db_path),
            "documents": st["documents"], "articles": st["articles"],
            "chunks": st["chunks"], "metrics": st["metrics"],
            "conflicts": st["conflicts"],
            "authoritative": st["authoritative"],   # 权威区行数（应与之前一致）
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 步骤：重建因果图谱（派生视图）
# ---------------------------------------------------------------------------

def step_graph(quiet: bool = False) -> Dict[str, Any]:
    """
    从 wiki/raw 重建因果知识图谱。

    ⚠️ 图谱与 state/、.index/ 同级——都是**派生视图**：
       可再生、绝不手改、与 wiki 冲突时以 wiki 为准。

    本步同时承担**结构校验**：引用不存在的实体/编号即报错
    （实测能抓出 `TQ-01` 那类"看起来像结构化知识"的虚构引用）。
    """
    try:
        import importlib
        import graph_build as GB
        importlib.reload(GB)

        doc = GB.build(verbose=False)
        return {
            "ok": True,
            "nodes": doc["stats"]["nodes"], "edges": doc["stats"]["edges"],
            "by_type": doc["stats"]["by_type"],
            "errors": doc["errors"], "warnings": doc["warnings"][:5],
        }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# 步骤 3：机械校验
# ---------------------------------------------------------------------------

_CHECK_SUMMARY_RE = re.compile(r"^\[(\d)\]\s*(.*)$")


def step_check(quiet: bool = False) -> Dict[str, Any]:
    """
    跑 check_evidence.py，解析出结构层问题。

    **只跑不修**——修是语义工作（判断该改哪里），留给 LLM 或人。
    本步的产出是**给 LLM 读的问题摘要**。
    """
    checker = HERE / "check_evidence.py"
    if not checker.exists():
        return {"ok": False, "error": f"校验器不存在: {checker}"}
    try:
        r = subprocess.run(
            [sys.executable, str(checker), str(WIKI_ROOT)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # 解析各段
    sections: Dict[str, str] = {}
    cur = None
    for line in out.split("\n"):
        m = _CHECK_SUMMARY_RE.match(line.strip())
        if m:
            cur = m.group(1)
            sections[cur] = line.strip()
        elif cur and line.strip().startswith(("死链", "索引未收录", "值变化", "❌")):
            sections[cur] += "\n" + line.rstrip()

    # 结构层计数
    #
    # ⚠️ 逐段用**显式模式**取数，不能笼统地取 `nums[-1]`：
    #    [1] `索引缺失 0，死链 0` 取最后一个只会拿到死链，漏掉索引缺失；
    #    [7] `乘性污染 0，普通 CRLF 10` 取最后一个会把**容忍项的 10** 当成问题数。
    #    两者都会让收尾误报。（本库已踩：普通 CRLF 被当成 10 个问题。）
    #
    # ⚠️ [7] 必须计入：它曾静默地把 state 从 555 条打到 339 条。
    #    [1][2][3] 报的是 wiki 内容问题，[7] 报的是**工具本身在破坏文件**。
    #    注意只计 `乘性污染`（真 bug）；`普通 CRLF` 是平台差异，容忍，不计。
    struct_issues = 0
    for k, pat in (("1", r"索引缺失\s*(\d+)"), ("1", r"死链\s*(\d+)"),
                   ("2", r"[：:]\s*(\d+)"), ("3", r"[：:]\s*(\d+)"),
                   ("7", r"乘性污染\s*(\d+)")):
        if k in sections:
            m = re.search(pat, sections[k])
            struct_issues += int(m.group(1)) if m else 0

    # state 同步状态（第 5 段）
    state_stale = "已过期" in sections.get("5", "")

    return {
        "ok": True,
        "structure_issues": struct_issues,   # 必须为 0
        "state_stale": state_stale,
        "sections": sections,
        "raw": out,
    }


# ---------------------------------------------------------------------------
# 步骤 4：算术校验
# ---------------------------------------------------------------------------

def step_math(quiet: bool = False) -> Dict[str, Any]:
    """
    跑 check_math.py——**复算 wiki 里的派生值**。

    ⚠️ 为什么必须独立于 check_evidence：
       `check_evidence` 只查"这个字面量在不在 raw 里"，**查不出算错**。
       本库两次算术事故（产量比 84% 应为 82.89%、六味同比 +4.08% 应为 +4.09%）
       都属于"字面量在 raw 里、但四舍五入错了"——它完全看不见。
    """
    checker = HERE / "check_math.py"
    if not checker.exists():
        return {"ok": False, "error": f"校验器不存在: {checker}"}
    try:
        r = subprocess.run(
            [sys.executable, str(checker)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120,
        )
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    m = re.search(r"通过\s*(\d+)\s*·\s*可疑\s*(\d+)", out)
    passed = int(m.group(1)) if m else 0
    suspect = int(m.group(2)) if m else 0
    return {"ok": True, "passed": passed, "suspect": suspect, "raw": out}


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------

def finalize(db_path: Path = DEFAULT_DB, only: Optional[str] = None,
             quiet: bool = False, skip: Optional[List[str]] = None
             ) -> Dict[str, Any]:
    """
    依次执行确定性收尾。**任一步失败不中断后续步骤**（各自独立，都有用）。

    `only` 可限定只跑某一步（'state' / 'db' / 'check' / 'graph' / 'math'）。
    `skip` 可**排除**某些步骤。

    ⚠️ `skip` 是为**个人工作区**准备的。用户上传文件后跑收尾时，
       绝不能执行 `db` 步——那会往**共享** SQLite 镜像里写个人数据，
       等于把私人资料混进公司知识库。个人区的收尾只跑 state + check。
    """
    todo = [only] if only else list(STEPS)
    if skip:
        todo = [s for s in todo if s not in set(skip)]
    rep: Dict[str, Any] = {}

    if "state" in todo:
        rep["state"] = step_state(quiet)
    if "db" in todo:
        rep["db"] = step_db(db_path, quiet)
    if "check" in todo:
        rep["check"] = step_check(quiet)
    if "graph" in todo:
        rep["graph"] = step_graph(quiet)
    if "math" in todo:
        rep["math"] = step_math(quiet)

    # ---- 汇总：是否有需要 LLM 处理的语义问题 ----
    problems: List[str] = []
    st = rep.get("state", {})
    if st.get("ok"):
        if st.get("changed"):
            problems.append(f"关键数值变化 {len(st['changed'])} 条（若非预期，检查编辑是否有误）")
    elif st:
        problems.append(f"state 重建失败: {st.get('error')}")

    db = rep.get("db", {})
    if db and not db.get("ok"):
        problems.append(f"DB 同步失败: {db.get('error')}")

    ck = rep.get("check", {})
    if ck.get("ok"):
        if ck.get("structure_issues"):
            problems.append(f"结构层有 {ck['structure_issues']} 个问题，需修复")
        if ck.get("state_stale"):
            problems.append("state 与 wiki 不同步（若刚重建，说明重建失败）")

    gr = rep.get("graph", {})
    if gr.get("ok"):
        if gr.get("errors"):
            problems.append(f"图谱结构校验有 {len(gr['errors'])} 处问题，需修复")
    elif gr:
        problems.append(f"图谱重建失败: {gr.get('error')}")

    mt = rep.get("math", {})
    if mt.get("ok"):
        if mt.get("suspect"):
            problems.append(f"算术复算有 {mt['suspect']} 处与声称值不符，需核对")
    elif mt:
        problems.append(f"算术校验失败: {mt.get('error')}")

    rep["problems"] = problems
    rep["clean"] = not problems

    if not quiet:
        _print_report(rep, todo)
    return rep


def _print_report(rep: Dict[str, Any], todo: List[str]) -> None:
    print("=" * 70)
    print("确定性收尾（脚本执行，LLM 未参与）")
    print("=" * 70)

    st = rep.get("state")
    if st is not None:
        if st.get("ok"):
            print(f"① state      ✅ {st['count']} 条关键数值")
            if st.get("changed"):
                print(f"   ⭐ 值变化 {len(st['changed'])} 条：")
                for c in st["changed"][:10]:
                    print(f"      {c['key']}  {c['was']} → {c['now']}")
            if st.get("added"):
                print(f"   新增 {len(st['added'])} 条")
            if st.get("removed"):
                print(f"   消失 {len(st['removed'])} 条")
        else:
            print(f"① state      ❌ {st.get('error')}")

    db = rep.get("db")
    if db is not None:
        if db.get("ok"):
            print(f"② DB 镜像    ✅ documents {db['documents']} · articles {db['articles']}"
                  f" · chunks {db['chunks']} · metrics {db['metrics']}"
                  f" · conflicts {db['conflicts']}")
            print(f"   权威区（未触碰）: {db['authoritative']}")
        else:
            print(f"② DB 镜像    ❌ {db.get('error')}")

    ck = rep.get("check")
    if ck is not None:
        if ck.get("ok"):
            mark = "✅" if ck["structure_issues"] == 0 else "❌"
            print(f"③ 机械校验   {mark} 结构层 {ck['structure_issues']} 个问题")
            for k, s in sorted(ck.get("sections", {}).items()):
                if k in ("1", "2", "3", "5", "7"):
                    print(f"      {s}")
        else:
            print(f"③ 机械校验   ❌ {ck.get('error')}")

    gr = rep.get("graph")
    if gr is not None:
        if gr.get("ok"):
            mark = "✅" if not gr["errors"] else "❌"
            print(f"④ 因果图谱   {mark} {gr['nodes']} 节点 · {gr['edges']} 边")
            for e in gr["errors"][:5]:
                print(f"      ❌ {e}")
        else:
            print(f"④ 因果图谱   ❌ {gr.get('error')}")

    mt = rep.get("math")
    if mt is not None:
        if mt.get("ok"):
            mark = "✅" if mt["suspect"] == 0 else "❌"
            print(f"⑤ 算术复算   {mark} 通过 {mt['passed']} · 可疑 {mt['suspect']}")
            if mt["suspect"]:
                for line in mt["raw"].split("\n"):
                    if "复算" in line or "↳" in line:
                        print(f"      {line.strip()}")
        else:
            print(f"⑤ 算术复算   ❌ {mt.get('error')}")

    print()
    if rep.get("clean"):
        print("✅ 收尾完成，无需 LLM 介入。")
    else:
        print("⚠️  需 LLM 处理（以下是语义问题，脚本无法自行解决）：")
        for p in rep["problems"]:
            print(f"   · {p}")


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="确定性收尾：state 重建 → DB 同步 → 机械校验（LLM 不参与）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"DB 路径（默认 {DEFAULT_DB}）")
    ap.add_argument("--only", choices=STEPS, help="只跑某一步")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    rep = finalize(Path(a.db), only=a.only, quiet=a.quiet or a.json)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0 if rep.get("clean") or a.json else 2


if __name__ == "__main__":
    sys.exit(main())

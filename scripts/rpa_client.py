# -*- coding: utf-8 -*-
"""
rpa_client.py — **整改闭环调度**（赛题模块四）

赛题要求（`raw/meta/赛题要求_创灵境2026.txt` 模块四）
-----------------------------------------------------
    5.4.1 整改任务生成   大模型根据分析结论生成结构化任务 JSON
                         （任务标题 / 责任人 / 来源 / 优先级 / 截止时间）
    5.4.2 RPA调度与消息推送  通过 HTTP API 发送到模拟 RPA 服务
                         模拟"发送微信消息"的闭环，前端展示"已发送至 XX 责任人"
    5.4.3 任务追踪看板（加分项）  展示已生成数 / 已送达数 / 确认数

对接的是赛题的 **mock RPA 服务**（`raw/rpa/mock_rpa_server.py`，:8090）。
关键事实（读源码得到，不是猜的）：

| 项 | 事实 |
|:---|:---|
| 建任务 | `POST /api/rpa/tasks` |
| 微信推送 | **不是独立接口** —— 建任务时 `notify_method=wechat`， |
|          | 返回体里直接带 `data.notify_status.wechat` |
| 状态机 | `sent → received → confirmed → in_progress → completed` |
| 加速测试 | `task_id` 含 `-FAST`（到 confirmed）或 `-DONE`（走完全程） |
| 优先级 | 只能小写 `high`/`medium`/`low`，否则 400 |
| task_id | 必须唯一，重复即 400 |

⚠️ 关于"微信推送"
----------------
赛题原文写的是「**模拟**"发送微信消息"的闭环」——是模拟，不是真推。
所以本模块**只对接 mock**，不引入真实企业微信 webhook。

设计：**mock 优先，失败降级为本地落盘**
--------------------------------------
mock 服务没起（很常见）时，任务不能丢——降级写进本地 `user_state`，
并在返回里标明 `degraded: true`，前端如实显示"未送达（服务未启动）"。
**绝不允许"假装发送成功"**——那是本库一以贯之的底线。

用法
----
    from rpa_client import RPAClient
    c = RPAClient()
    r = c.dispatch(tasks, month="2026-05", product="银黄口服液")
    c.list_tasks()          # 从 mock 拉状态
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

# 默认地址：本机直跑脚本时用（mock 就在本机 8090）。
# ⚠️ 容器里必须覆盖：容器内的 `127.0.0.1` 指的是**容器自己**，
#    而 mock 是**另一个服务**，要走 compose 服务名 `rpa-mock`。
#    所以地址可配（env `RPA_BASE_URL`），由 compose 注入。
#    写死的后果不是报错，而是"连接被拒"→ 四个计数永远为 0 ——
#    看起来像"没有任务"，实际是发不出去。
DEFAULT_BASE = os.getenv("RPA_BASE_URL", "http://127.0.0.1:8090").rstrip("/")

# 状态机（读 mock 源码得到）。用于前端展示进度。
STATUS_FLOW = ["sent", "received", "confirmed", "in_progress", "completed"]
STATUS_ZH = {
    "sent": "已发出",
    "received": "已送达",
    "confirmed": "已确认",
    "in_progress": "处理中",
    "completed": "已完成",
    "failed": "未送达",
}

# 优先级：报告里印中文（给人看），RPA 只收小写英文（接口约束）。
# ⚠️ 这个映射**必须只有一份**。曾经 `build_task_payload` 认中文、
#    `parse_report_tasks` 只认英文，两处规则不一致：
#    报告若印「高」，前者能转 high，后者落到 else 分支**静默降级成 medium**。
#    优先级降级不会报错、不会 400，只是紧急任务被排到中档——最难发现的一类错。
PRIORITY_ZH = {"高": "high", "中": "medium", "低": "low"}
PRIORITY_EN = ("high", "medium", "low")


def to_rpa_priority(v: Any) -> str:
    """中文/英文/大小写混杂 → RPA 只认的小写三值。认不出时落 medium。"""
    s = str(v).strip()
    if s in PRIORITY_ZH:
        return PRIORITY_ZH[s]
    s = s.lower()
    return s if s in PRIORITY_EN else "medium"


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """
    显式禁用代理。

    ⚠️ 实测踩过：本机 `HTTP_PROXY=http://127.0.0.1:5479` 会把发往
       localhost 的请求**拦成 502**，看起来像"服务没起"。
       文档里也写了这一条——**502 未必是服务没起来，先查代理**。
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


class RPAClient:
    """赛题 mock RPA 服务的客户端。**只对接模拟服务，不真推微信。**"""

    def __init__(self, base_url: str = DEFAULT_BASE, timeout: int = 15):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = _no_proxy_opener()

    # ---- 基础请求 -----------------------------------------------------
    def _req(self, method: str, path: str,
             body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        with self._opener.open(req, timeout=self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def health(self) -> Dict[str, Any]:
        try:
            return {"ok": True, **self._req("GET", "/health")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- 任务生成（从分析结论 → RPA 契约）-----------------------------
    @staticmethod
    def build_task_payload(advice: Dict[str, Any], month: str, product: str,
                           seq: int, source_type: str = "月度成本分析报告",
                           fast: bool = False) -> Dict[str, Any]:
        """
        把一条"改进建议"转成 RPA 契约要求的任务 JSON。

        字段映射（`报告模板契约.md` §6.1）：
            任务编号→task_id  任务标题→task_title  责任人→assignee.name
            优先级→priority   来源→source.finding  截止时间→deadline
        """
        ym = month.replace("-", "")
        tid = f"TASK-{ym}-{seq:03d}"
        if fast:
            tid += "-DONE"          # mock 的加速后缀：走完整个状态机

        pr = to_rpa_priority(advice.get("优先级", "medium"))   # mock 只收小写三值

        dept = str(advice.get("责任部门", "财务部")).strip() or "财务部"
        return {
            "task_id": tid,
            "task_title": str(advice.get("建议事项", "成本异常核查")).strip()[:120],
            "assignee": {"name": dept, "department": f"中药一厂-{dept}", "role": "整改责任人"},
            "source": {
                "analysis_type": source_type,
                "analysis_month": month,
                "product": product,
                "finding": str(advice.get("预期效果", "")).strip()[:200] or "见分析报告",
            },
            "priority": pr,
            "deadline": str(advice.get("建议完成时间", "")).strip() or "2026-06-30",
            "suggestion": str(advice.get("预期效果", "")).strip()[:200],
            "notify_method": "wechat",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    @staticmethod
    def parse_report_tasks(report_md: str, month: str,
                           product: str) -> List[Dict[str, Any]]:
        """
        从**已生成的 6 章报告**里解析「6.4 整改任务清单」表格 → RPA 载荷。

        报告已按契约生成，所以这里只需忠实搬运，**不重新生成任务**——
        否则会出现"报告里写的"与"发出去的"不一致。

        表头：| 任务编号 | 任务标题 | 责任人 | 优先级 | 来源 | 截止时间 |
        """
        out: List[Dict[str, Any]] = []
        in_sec = False
        for line in report_md.split("\n"):
            if line.startswith("### 6.4"):
                in_sec = True
                continue
            if in_sec and line.startswith("### "):
                break
            if not in_sec or not line.strip().startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 6 or not re.match(r"^TASK-", cells[0]):
                continue
            tid, title, who, pr, src, due = cells[:6]
            out.append({
                "task_id": tid,
                "task_title": title,
                "assignee": {"name": who, "department": f"中药一厂-{who}", "role": "整改责任人"},
                "source": {"analysis_type": src, "analysis_month": month,
                           "product": product, "finding": title[:120]},
                "priority": to_rpa_priority(pr),
                "deadline": due,
                "suggestion": title[:200],
                "notify_method": "wechat",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        return out

    # ---- 派发 ---------------------------------------------------------
    def dispatch_one(self, payload: Dict[str, Any],
                     existing: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发一个任务。返回统一结构（含是否降级）。

        `existing`：下游已存在的 `{task_id: task_title}`，用于区分
        「同一任务重复下发」与「编号被别的任务占用」——两者都报 400，
        但性质完全不同（见下）。
        """
        try:
            r = self._req("POST", "/api/rpa/tasks", payload)
            d = (r or {}).get("data", {}) or {}
            return {
                "task_id": payload["task_id"],
                "ok": True,
                "degraded": False,
                "status": d.get("status", "sent"),
                "notify": (d.get("notify_status") or {}).get("wechat", ""),
                "tracking_url": d.get("tracking_url", ""),
                "message": (r or {}).get("message", ""),
            }
        except urllib.error.HTTPError as e:
            # ⚠️ 400 通常是"task_id 重复"或"priority 非法"——**如实报出**，
            #    不要吞掉当成成功。HTTPError 的 body 里有服务端给的原因。
            try:
                detail = json.loads(e.read().decode("utf-8")).get("detail", "")
            except Exception:  # noqa: BLE001
                detail = ""

            if "已存在" in detail or "exist" in detail.lower():
                # 报告 6.4 的编号是**确定性**的（TASK-<年月>-<序号>），
                # 所以同一份报告下发第二次必然撞号。那不该当失败——系统状态是对的。
                # ⚠️ 但**绝不能见到"已存在"就当成功**：编号里**不含产品**，
                #    因此同月不同产品会撞同一个编号！
                #    （实测：银黄口服液与六味地黄胶囊的 6.4 都产出 TASK-202605-001，
                #      任务内容却完全不同。）
                #    只看 id 就认成功 = **假成功**：六味的任务其实根本没进下游。
                # ⇒ 必须比对**任务标题**：一致才是"之前发过了"；
                #   不一致说明编号被占用，要**明确报错**，让人知道这次没发出去。
                prev = (existing or {}).get(payload["task_id"])
                title = str(payload.get("task_title", "")).strip()
                if prev is not None and str(prev).strip() == title:
                    return {"task_id": payload["task_id"], "ok": True, "existed": True,
                            "degraded": False, "status": "sent",
                            "notify": "此前已下发（未重复发送）"}
                return {
                    "task_id": payload["task_id"], "ok": False, "degraded": False,
                    "status": "failed", "collision": True,
                    "error": (f"编号已被占用：下游同编号的任务是「{str(prev)[:30] if prev else '?'}」，"
                              f"与本次要发的「{title[:30]}」不是同一件事。"
                              f"本次**未下发**——需先消除编号冲突。"),
                }

            return {"task_id": payload["task_id"], "ok": False, "degraded": False,
                    "status": "failed",
                    "error": f"HTTP {e.code}: {detail or e.reason}"}
        except Exception as e:  # noqa: BLE001
            # 连不上 mock 服务 → 降级（由调用方决定是否落盘）
            return {"task_id": payload["task_id"], "ok": False, "degraded": True,
                    "status": "failed",
                    "error": f"{type(e).__name__}: {e}"}

    def dispatch(self, payloads: List[Dict[str, Any]]) -> Dict[str, Any]:
        """批量派发，返回汇总。

        先拉一次下游已存在的任务（`{task_id: title}`）——**一次列表换 N 次判定**，
        不必每个任务单独查。拉不到（服务没起）就传 None，交给降级分支处理。
        """
        existing: Optional[Dict[str, str]] = None
        try:
            r = self.list_tasks()
            if r.get("ok"):
                existing = {t.get("task_id"): t.get("task_title") or ""
                            for t in self.extract_tasks(r)}
        except Exception:  # noqa: BLE001
            existing = None

        results = [self.dispatch_one(p, existing) for p in payloads]
        ok = [r for r in results if r["ok"]]
        existed = [r for r in ok if r.get("existed")]
        fresh = [r for r in ok if not r.get("existed")]
        degraded = [r for r in results if r.get("degraded")]
        failed = [r for r in results if not r["ok"] and not r.get("degraded")]
        collided = [r for r in results if r.get("collision")]
        return {
            "total": len(results),
            "sent": len(ok),          # 「已在下游系统中」的总数（含此前已发的）
            "fresh": len(fresh),      # 本次真正新发出去的
            "existed": len(existed),
            "degraded": len(degraded),
            "failed": len(failed),
            "collided": len(collided),
            "results": results,
            "mock_available": not degraded,
            "summary": self._summarize(len(results), len(fresh), len(existed),
                                       len(degraded), len(failed), len(collided)),
        }

    @staticmethod
    def _summarize(total: int, fresh: int, existed: int,
                   degraded: int, failed: int, collided: int = 0) -> str:
        parts: List[str] = []
        if fresh:
            parts.append(f"{fresh} 条已下发并向责任人发送通知")
        if existed:
            parts.append(f"{existed} 条此前已下发（未重复发送）")
        if degraded:
            # ⚠️ 这条串会**直接显示在前端**（纯文本，不渲染 markdown），
            #    所以不要写 `**加粗**`——会原样露出星号。
            parts.append(f"{degraded} 条未送达（模拟 RPA 服务未启动）——已保留待重发，请不要当作已下发")
        if collided:
            parts.append(f"{collided} 条未下发：编号与下游已有任务冲突（同月不同产品撞号）")
        other_failed = failed - collided
        if other_failed > 0:
            parts.append(f"{other_failed} 条被拒绝")
        if not parts:
            return "没有可下发的任务"
        if failed or degraded:
            return f"共 {total} 条：" + "，".join(parts)
        return "、".join(parts)

    # ---- 查询 ---------------------------------------------------------
    def list_tasks(self, **filters: Any) -> Dict[str, Any]:
        """
        从 mock 拉任务列表（可按 status/priority/product 过滤）。

        ⚠️ 返回结构实测为 `{code, message, data:{total, page, page_size, tasks:[...]}}`
           —— 任务在 **`data.tasks`**，不是 `data.items`。
           （第一版按 `items` 写，结果列表永远显示 0 条，而派发明明是成功的。）
        """
        q = "&".join(f"{k}={v}" for k, v in filters.items() if v)
        try:
            r = self._req("GET", "/api/rpa/tasks" + (f"?{q}" if q else ""))
            return {"ok": True, "data": r}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "data": {}}

    @staticmethod
    def extract_tasks(listing: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从列表响应里取任务数组（兼容 tasks / items 两种键）。"""
        d = (listing or {}).get("data", {}).get("data", {}) or {}
        return d.get("tasks") or d.get("items") or []

    # ---- 单任务 / 统计 / 通知（接口文档里有，之前没接）-----------------

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """
        查**单个任务**的完整信息。

        ⭐ 比列表查询多出来的关键内容是 **`status_history`**——
        任务的**状态流转过程**（sent → received → confirmed → …）。
        列表只给"当前是什么状态"，拿不到"什么时候变的"，
        而任务追踪的价值恰恰在那条时间线上。

        返回 `{ok, task}` 或 `{ok: False, error}`。
        """
        # ⚠️ task_id 必须 URL 编码。用户可能会查一个不存在的中文编号，
        #    直接拼进 URL 会让 http.client 用 ascii 编码报
        #    `UnicodeEncodeError` —— 那是**用户输入触发的崩溃**，不是 404。
        from urllib.parse import quote
        try:
            r = self._req("GET", f"/api/rpa/tasks/{quote(str(task_id), safe='')}")
            return {"ok": True, "task": (r or {}).get("data", {}) or {}}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"ok": False, "error": f"任务 {task_id} 不存在（可能尚未下发）"}
            return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "degraded": True}

    def stats(self) -> Dict[str, Any]:
        """下游服务的整体统计（总数 / 按状态 / 按优先级 / 通知数）。"""
        try:
            r = self._req("GET", "/api/stats")
            return {"ok": True, "data": (r or {}).get("data", {}) or {}}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "degraded": True}

    def notify(self, recipient: str, department: str,
               message: str) -> Dict[str, Any]:
        """
        **手动补发**一条微信通知。

        ⚠️ 与派发时的自动通知不同：那是建任务的副作用，
           这个是**补发**——只在自动通知没送达时用。
           正常流程不需要调它。
        """
        try:
            r = self._req("POST", "/api/notify/wechat",
                          {"recipient": recipient, "department": department,
                           "message": message})
            d = (r or {}).get("data", {}) or {}
            return {"ok": True, "message_id": d.get("message_id", ""),
                    "sent_at": d.get("sent_at", ""), "status": d.get("status", "")}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "degraded": True}

    @staticmethod
    def summarize_status(tasks: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        任务追踪看板要的三个计数（赛题 5.4.3）：
        已生成数 / 已送达数 / 确认数。
        """
        n = len(tasks)
        reached = sum(1 for t in tasks
                      if STATUS_FLOW.index(t.get("status", "sent"))
                      >= STATUS_FLOW.index("received"))
        confirmed = sum(1 for t in tasks
                        if STATUS_FLOW.index(t.get("status", "sent"))
                        >= STATUS_FLOW.index("confirmed"))
        by_status: Dict[str, int] = {}
        for t in tasks:
            s = t.get("status", "sent")
            by_status[s] = by_status.get(s, 0) + 1
        return {"generated": n, "delivered": reached, "confirmed": confirmed,
                "by_status": by_status}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="整改闭环调度（对接模拟 RPA 服务）")
    ap.add_argument("--health", action="store_true", help="探活模拟服务")
    ap.add_argument("--report", help="从某份报告解析 6.4 任务并派发")
    ap.add_argument("--month", default="2026-05")
    ap.add_argument("--product", default="银黄口服液")
    ap.add_argument("--list", action="store_true", help="列出 mock 里的任务")
    ap.add_argument("--base", default=DEFAULT_BASE)
    a = ap.parse_args()

    c = RPAClient(a.base)

    if a.health:
        print(json.dumps(c.health(), ensure_ascii=False, indent=2))
        return 0

    if a.list:
        r = c.list_tasks()
        if not r["ok"]:
            print(f"⚠️ 模拟 RPA 服务未就绪：{r['error']}")
            return 1
        tasks = c.extract_tasks(r)
        s = c.summarize_status(tasks)
        print(f"已生成 {s['generated']} · 已送达 {s['delivered']} · 已确认 {s['confirmed']}")
        for t in tasks:
            print(f"  {t['task_id']:<22} [{STATUS_ZH.get(t['status'], t['status'])}] "
                  f"{t['task_title'][:44]}")
        return 0

    if a.report:
        p = Path(a.report)
        if not p.exists():
            print(f"报告不存在：{p}")
            return 1
        payloads = RPAClient.parse_report_tasks(
            p.read_text(encoding="utf-8"), a.month, a.product)
        if not payloads:
            print("报告里没有可派发的整改任务（6.4 表为空）")
            return 0
        rep = c.dispatch(payloads)
        print(rep["summary"])
        for r in rep["results"]:
            mark = "✅" if r["ok"] else ("⚠️" if r.get("degraded") else "❌")
            print(f"  {mark} {r['task_id']}  {r.get('notify') or r.get('error','')}")
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())

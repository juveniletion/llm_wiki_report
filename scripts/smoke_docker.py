# -*- coding: utf-8 -*-
"""
smoke_docker.py — 对**已部署的容器**做端到端功能冒烟测试

与单元测试不同：这里只通过 HTTP 打接口，不 import 任何业务代码——
测的是"部署出来的这个东西能不能用"，而不是"函数返回值对不对"。
服务没起、端口不通、CSRF 拦错、跨容器 RPA 连不上，都会在这里暴露。

用法
----
    python scripts/smoke_docker.py                     # 默认 127.0.0.1:8765
    python scripts/smoke_docker.py --base http://host:8765
"""
from __future__ import annotations

import argparse
import http.cookiejar as cj
import json
import random
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

PASS, FAIL = [], []


def rec(ok: bool, name: str, detail: str = "") -> bool:
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f"　{detail}" if detail else ""))
    return ok


class Client:
    """带 cookie + CSRF 的极简客户端。**绕过系统代理**——否则本机请求会被代理拦成 502。"""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = cj.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar))

    @property
    def csrf(self):
        return next((c.value for c in self.jar if c.name == "lw_csrf"), None)

    def req(self, method: str, path: str, body=None, csrf: bool = False, timeout: int = 180):
        h = {"Content-Type": "application/json"}
        if csrf and self.csrf:
            h["X-CSRF-Token"] = self.csrf
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        with self.op.open(r, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            # ⚠️ 不能只看 Content-Type 判定 JSON：SSE 流式接口回的是 text/event-stream，
            #    而错误响应可能没有正确的 Content-Type。按内容再试一次解析，
            #    否则调用方拿到 bytes 却按 dict 用，报的是 AttributeError 而不是真实错误。
            parsed = raw
            if "json" in ctype or raw[:1] in (b"{", b"["):
                try:
                    parsed = json.loads(raw.decode())
                except Exception:  # noqa: BLE001
                    parsed = raw
            return resp.status, parsed, ctype

    def get(self, p, **kw):
        return self.req("GET", p, **kw)

    def post(self, p, body=None, **kw):
        return self.req("POST", p, body=body, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description="容器端到端冒烟测试")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    a = ap.parse_args()
    B = a.base
    print("=" * 74)
    print(f"容器冒烟测试 → {B}")
    print("=" * 74)

    # ---- 1. 起服务 ----
    print("\n[1] 服务可达性")
    c = Client(B)
    try:
        st, _, ct = c.get("/")
        rec(st == 200 and "html" in ct, "GET / 返回静态首页")
        st, spec, _ = c.get("/openapi.json")
        rec(st == 200 and "paths" in spec, "GET /openapi.json 可解析",
            f"{len(spec.get('paths', {}))} 个端点")
    except Exception as e:  # noqa: BLE001
        rec(False, "服务可达", f"{type(e).__name__}: {e}")
        print("\n⚠️ 服务不可达，后续测试无意义，中止。")
        return 1

    # ---- 2. 认证 ----
    print("\n[2] 认证")
    user = f"smoke{random.randint(100000, 999999)}"
    try:
        st, d, _ = c.post("/api/auth/register",
                          {"username": user, "password": "Smoke1234!"})
        rec(st == 200, "注册新用户", user)
    except urllib.error.HTTPError as e:
        rec(False, "注册新用户", f"{e.code} {e.read().decode()[:100]}")
    rec(c.csrf is not None, "下发了 CSRF cookie")
    try:
        st, me, _ = c.get("/api/auth/me")
        rec(st == 200, "GET /api/auth/me 已登录")
    except urllib.error.HTTPError as e:
        rec(False, "GET /api/auth/me", str(e.code))

    # 未认证的边界：**写操作**必须被拒（只读看板是刻意公开的，见下方说明）
    anon = Client(B)
    for name, path, body in [
        ("生成报告", "/api/control/generate", {"product": "银黄口服液", "month": "2026-05"}),
        ("派发任务", "/api/rpa/dispatch", {"report": "x.md", "month": "2026-05"}),
        ("列出会话", "/api/conversations", None),
    ]:
        try:
            if body is None:
                anon.get(path)
            else:
                anon.post(path, body, csrf=True)
            rec(False, f"未认证 {name} 被拒", "⚠️ 竟然放行")
        except urllib.error.HTTPError as e:
            rec(e.code in (401, 403), f"未认证 {name} 被拒", f"HTTP {e.code}")

    # 只读看板**是刻意公开的**（无需登录即可看板），这里断言它在未登录下可用，
    # 避免将来有人误加鉴权把首页看板打坏。
    try:
        st, _, _ = anon.get("/api/overview")
        rec(st == 200, "只读看板未登录可用（设计如此）")
    except urllib.error.HTTPError as e:
        rec(False, "只读看板未登录可用", f"HTTP {e.code}（若加了鉴权则首页会坏）")

    # ---- 3. 只读看板接口 ----
    print("\n[3] 看板只读接口")
    P = urllib.parse.urlencode({"product": "银黄口服液", "month": "2026-05"})
    for name, path in [
        ("themes", "/api/themes"),
        ("overview", "/api/overview"),
        ("trend", f"/api/trend?{P}"),
        ("heatmap", "/api/heatmap"),
        ("products", "/api/products"),
        ("benchmark", f"/api/benchmark?{P}"),
        ("forecast", f"/api/forecast?{P}"),
        ("rpa/health", "/api/rpa/health"),
        ("control/exports", "/api/control/exports"),
    ]:
        try:
            st, d, _ = c.get(path)
            rec(st == 200, f"GET {path.split('?')[0]}", f"HTTP {st}")
        except urllib.error.HTTPError as e:
            rec(False, f"GET {path.split('?')[0]}",
                f"HTTP {e.code} {e.read().decode()[:90]}")

    # exports 不应含 techspec（交付物文档不进站点）
    try:
        _, d, _ = c.get("/api/control/exports")
        rec("techspec" not in d, "导出清单不含技术方案（交付物不入站）",
            f"键={sorted(d.keys())}")
    except Exception:  # noqa: BLE001
        rec(False, "导出清单检查")

    # ---- 4. 网络隔离：跨容器 RPA ----
    print("\n[4] 跨容器 RPA（wiki-dashboard → rpa-mock）")
    try:
        st, h, _ = c.get("/api/rpa/health")
        rec(st == 200 and h.get("ok"), "主服务能连通 rpa-mock", f"tasks={h.get('tasks_count')}")
        st, d, _ = c.post("/api/rpa/dispatch",
                          {"report": "银黄口服液_2026-05.md",
                           "month": "2026-05", "product": "银黄口服液"}, csrf=True)
        rec(st == 200, "派发任务", f"total={d.get('total')}")
    except urllib.error.HTTPError as e:
        rec(False, "跨容器 RPA", f"HTTP {e.code} {e.read().decode()[:120]}")
    # 空文件名应 400（不是 500）
    try:
        c.post("/api/rpa/dispatch", {"month": "2026-05", "product": "银黄口服液"}, csrf=True)
        rec(False, "空 report 被拒", "⚠️ 竟然放行")
    except urllib.error.HTTPError as e:
        rec(e.code == 400, "空 report → 400（非 500）", f"HTTP {e.code}")

    # ---- 5. 报告生成 + 导出 ----
    print("\n[5] 报告生成与导出（模块一）")
    try:
        st, d, _ = c.post("/api/control/generate",
                          {"product": "银黄口服液", "month": "2026-05",
                           "theme": "monthly"}, csrf=True)
        rec(st == 200, "生成月度报告（含 LLM）",
            f"{len(json.dumps(d, ensure_ascii=False))} 字节")
    except urllib.error.HTTPError as e:
        rec(False, "生成月度报告", f"HTTP {e.code} {e.read().decode()[:120]}")

    for fmt in ("pdf", "docx"):
        try:
            st, raw, ct = c.post("/api/control/export",
                                 {"md": "银黄口服液_2026-05.md", "kind": "report",
                                  "fmt": fmt}, csrf=True)
            magic = raw[:4]
            good = (st == 200 and len(raw) > 10000
                    and ((fmt == "pdf" and magic == b"%PDF")
                         or (fmt == "docx" and magic == b"PK\x03\x04")))
            rec(good, f"导出 {fmt.upper()}", f"{len(raw):,} 字节 magic={magic!r}")
        except urllib.error.HTTPError as e:
            rec(False, f"导出 {fmt.upper()}", f"HTTP {e.code} {e.read().decode()[:120]}")

    # 空文件名 → 400（不是 500）
    try:
        c.post("/api/control/export", {"md": "", "kind": "report", "fmt": "pdf"}, csrf=True)
        rec(False, "空 md 被拒", "⚠️ 竟然放行")
    except urllib.error.HTTPError as e:
        rec(e.code == 400, "空 md → 400（非 500）", f"HTTP {e.code}")

    # ---- 6. 多会话隔离 ----
    print("\n[6] 多会话与上下文隔离")
    try:
        st, d, _ = c.get("/api/conversations")
        rec(st == 200 and "conversations" in d, "列出会话")
        st, d2, _ = c.post("/api/conversations", {"title": "冒烟测试会话"}, csrf=True)
        cid = (d2 or {}).get("id") or (d2 or {}).get("conversation_id")
        rec(bool(cid), "新建会话", f"id={cid}")
        if cid:
            st, d3, _ = c.get(f"/api/conversations/{cid}")
            rec(st == 200, "取指定会话")
            st, _, _ = c.req("DELETE", f"/api/conversations/{cid}", csrf=True)
            rec(st == 200, "删除会话")
    except urllib.error.HTTPError as e:
        rec(False, "多会话", f"HTTP {e.code} {e.read().decode()[:120]}")

    # 伪造 id → 404（不是 200/403/500）
    for fake in ("/api/conversations/999999999",
                 "/api/conversations/0"):
        try:
            c.get(fake)
            rec(False, f"伪造会话 id 被拒 {fake}", "⚠️ 竟然放行")
        except urllib.error.HTTPError as e:
            rec(e.code == 404, f"伪造 id → 404 {fake}", f"HTTP {e.code}")

    # ---- 7. 对话（agent 全链路，SSE 流式） ----
    print("\n[7] 对话（agent 工具链，SSE 流式）")
    # ⚠️ `/api/chat` 回的是 `text/event-stream`，**不是 JSON**。要逐行解析
    #    `data: {...}`：`token` 是**增量**片段，`done` 才带**完整** `answer`。
    #
    # ⚠️ 判据必须用 `done.answer`，**不能**只看 token 累加：
    #    库直答（router 第①档，0 token）命中时根本不发 token，
    #    正文只出现在 `done.answer` 里——只看 token 会把它误判成"空回答"。
    #
    # ⚠️ 上游 LLM 偶发失败（实测 1/N 次），故对**上游抖动**允许重试一次，
    #    并把"重试过"如实打出来。这不隐藏问题，只是区分
    #    「系统坏了」与「这次调用上游抽风」。
    def _chat_once():
        h = {"Content-Type": "application/json", "X-CSRF-Token": c.csrf or ""}
        req = urllib.request.Request(
            B + "/api/chat",
            data=json.dumps({"question": "本月单位成本是多少？", "new": True},
                            ensure_ascii=False).encode(),
            method="POST", headers=h)
        types, toks, done_answer, err, ctype = set(), [], "", None, ""
        with c.op.open(req, timeout=300) as resp:
            ctype = resp.headers.get("Content-Type", "")
            for line in resp:
                line = line.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:  # noqa: BLE001
                    continue
                t = ev.get("type")
                types.add(t)
                if t == "token":
                    toks.append(ev.get("text") or "")
                elif t == "done":
                    done_answer = ev.get("answer") or ""
                elif t == "error":
                    err = ev.get("message") or str(ev)
        text = "".join(toks) or done_answer
        return types, text, err, ctype

    try:
        types, text, err, ctype = _chat_once()
        retried = False
        if err and not text:
            print(f"      ⚠️ 首次失败（{err[:60]}），重试一次…")
            retried = True
            types, text, err, ctype = _chat_once()
        rec("event-stream" in ctype, "POST /api/chat 返回 SSE 流", ctype)
        rec("start" in types, "收到 start 事件",
            f"事件类型={sorted(t for t in types if t)}")
        rec(err is None, "流内无 error 事件",
            (err or "") + ("（重试后成功，首次为上游抖动）" if retried and not err else ""))
        rec(len(text) > 0, "取回回答正文",
            f"{len(text)} 字" + ("（来自 done.answer，本轮未流 token）"
                                if text and "token" not in types else "")
            + ("（重试后）" if retried else ""))
        if text:
            print(f"      回答节选：{text[:70]}…")
    except urllib.error.HTTPError as e:
        rec(False, "对话", f"HTTP {e.code} {e.read().decode()[:150]}")
    except Exception as e:  # noqa: BLE001
        rec(False, "对话", f"{type(e).__name__}: {e}")

    # ---- 汇总 ----
    print("\n" + "=" * 74)
    print(f"通过 {len(PASS)} · 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print(f"  - {f}")
    print("=" * 74)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())

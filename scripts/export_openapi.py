# -*- coding: utf-8 -*-
"""
export_openapi.py — 把 FastAPI 的 `/openapi.json` 转成 markdown 接口文档

为什么要这样做，而不是手写
--------------------------
手写的接口文档**必然与代码漂移**——本项目的原则是"派生视图一律脚本生成"，
接口文档正是典型的派生视图：它的唯一事实源是 `server/app.py` 的路由定义。

所以：跑本脚本 → 从 `/openapi.json`（FastAPI 自己生成的规范）转 markdown，
再嵌进技术方案文档。代码改了，重跑一次就同步。

用法
----
    python scripts/export_openapi.py                 # 输出到 stdout
    python scripts/export_openapi.py --out api.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent


def _schema_brief(ref: str, spec: Dict[str, Any]) -> str:
    """把 `#/components/schemas/X` 转成一句话。"""
    name = ref.split("/")[-1]
    sch = (spec.get("components", {}).get("schemas", {}) or {}).get(name, {})
    props = sch.get("properties", {}) or {}
    if not props:
        return name
    parts = []
    for k, v in list(props.items())[:8]:
        t = v.get("type") or v.get("anyOf") or v.get("$ref", "")
        if isinstance(t, list):
            t = "|".join(x.get("type", "?") for x in t if isinstance(x, dict)) or "—"
        if isinstance(t, str) and t.startswith("#/"):
            t = t.split("/")[-1]
        parts.append(f"{k}:{t}")
    return f"{name} ({', '.join(parts)})"


def render(spec: Dict[str, Any]) -> str:
    """把 OpenAPI spec 渲染成 markdown。"""
    paths: Dict[str, Any] = spec.get("paths", {})
    groups: Dict[str, List[str]] = {}

    for path, ops in sorted(paths.items()):
        for method, op in ops.items():
            if method not in ("get", "post", "put", "delete", "patch"):
                continue
            # 按第一段路径分组：/api/control/... → control
            seg = [s for s in path.split("/") if s]
            grp = seg[1] if len(seg) > 1 and seg[0] == "api" else "其他"
            groups.setdefault(grp, [])

            line = f"| `{method.upper()}` | `{path}` | {op.get('summary', '')} |"
            groups[grp].append(line)

    out = [f"共 {len(paths)} 个端点。\n"]
    for grp in sorted(groups):
        out.append(f"#### {grp}\n")
        out.append("| 方法 | 路径 | 说明 |")
        out.append("|:---|:---|:---|")
        out.extend(["/".join(["", groups[grp][0].split("`")[1]]) if False else l
                    for l in groups[grp]])
        out.append("")

    # 请求/响应模型
    schemas = (spec.get("components", {}).get("schemas", {}) or {})
    cus = {k: v for k, v in schemas.items() if not k.startswith(("HTTPValidation", "ValidationError"))}
    if cus:
        out.append("#### 请求 / 响应模型\n")
        for name, sch in sorted(cus.items()):
            props = sch.get("properties", {}) or {}
            if not props:
                continue
            out.append(f"**{name}**")
            out.append("")
            out.append("| 字段 | 类型 | 必填 | 说明 |")
            out.append("|:---|:---|:---|:---|")
            req = set(sch.get("required", []) or [])
            for k, v in props.items():
                t = v.get("type", "")
                if "anyOf" in v:
                    t = "|".join(x.get("type", "?") for x in v["anyOf"] if isinstance(x, dict))
                out.append(f"| `{k}` | {t or '—'} | {'是' if k in req else '否'} | "
                           f"{v.get('description', '') or v.get('title', '')} |")
            out.append("")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="从 /openapi.json 生成 markdown 接口文档")
    ap.add_argument("--out", help="输出文件（默认 stdout）")
    a = ap.parse_args()

    # 直接 import app 拿 spec —— 不依赖服务在跑
    sys.path.insert(0, str(WIKI_ROOT / "server"))
    try:
        import app as A
        spec = A.app.openapi()
    except Exception as e:  # noqa: BLE001
        print(f"无法加载 FastAPI 应用：{type(e).__name__}: {e}")
        return 1

    md = render(spec)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8", newline="\n")
        print(f"已写出 {a.out}（{len(md):,} 字符）")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())

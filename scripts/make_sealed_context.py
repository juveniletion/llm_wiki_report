# -*- coding: utf-8 -*-
"""
make_sealed_context.py — 组装**加密镜像**的构建上下文（代码 + 真实数据）

为什么需要它
------------
公开仓库的 `.dockerignore` 会把 `raw/ wiki/ state/ graph/ reports/` 排除出
**构建上下文**——那是刻意的安全机制（使 `COPY raw/` 物理上不可能成功）。

而加密镜像**需要**把这些数据 COPY 进去。两个需求冲突，所以：

    基础镜像  ← 用仓库目录构建（.dockerignore 生效，零数据）
    加密镜像  ← 用**本脚本组装的另一个上下文**构建（只含代码 + 数据）

⚠️ 为什么不用 `--ignorefile` 之类的开关，而是另建目录：
   ● 目录形态的上下文**看得见**。你要交付的是含机密数据的镜像，
     "我到底把什么打进去了"必须能一眼列出来（`--list` 就是干这个的）。
   ● 临时改 ignore 文件是**指针状态**：构建完忘了改回来，
     下一次构建就会**静默**把数据打进基础镜像——那正是我们防的事。
   ● 组装目录能**复用仓库里的 Dockerfile.sealed / Dockerfile**，
     不必手抄一份，避免两份漂移。

用法
----
    python scripts/make_sealed_context.py                      # 组装到 .sealed_context/
    python scripts/make_sealed_context.py --list               # 只看会打包什么，不写
    python scripts/make_sealed_context.py --out /tmp/sealed    # 指定目录

组装后（手动执行，因为要 Docker 引擎）：

    cd .sealed_context
    docker build -t pharma-cost-wiki:latest .                   # 基础（零数据）
    docker build -f Dockerfile.sealed -t pharma-cost-wiki:sealed .   # 派生（含数据）
    docker save pharma-cost-wiki:sealed -o ../pharma-cost-wiki-sealed.tar

⚠️ 交付前**必须**核对 `--list` 的输出，并确认镜像层里确实有数据（见文末校验）。
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

# ---- 可公开的代码与文档（与公开仓库同步的那一套）----
CODE_ITEMS = [
    "scripts", "server", "references",
    "Dockerfile", "Dockerfile.sealed", "docker-compose.yml",
    "requirements.txt", "server/requirements.txt", "server/requirements-mock.txt",
    "SKILL.md", "INGEST_AGENT.md", "AGENT_RUNTIME.md", "README.md", "DEPLOY.md",
    "系统现状与架构.md",
]

# ---- 真实数据（外包目录整体复制）----
DATA_ITEMS = ["raw", "wiki", "state", "graph", "reports"]

# ---- 绝不进上下文的（列出来是为了让"为什么不带"可查）----
EXCLUDED = {
    "data":         "运行时库：auth.db 是凭据、users/*.db 是隐私、knowledge.db 是派生物（启动自举会重建）",
    "exports":      "导出产物，运行时生成",
    ".index":       "向量缓存，可再生",
    ".env":         "密钥——进了镜像层就永久留在层里，后续层删不掉",
    "__pycache__":  "编译产物",
    ".git":         "版本历史（体积大，且无运行价值）",
    "sample_workspace": "样例数据，给公开仓库用的，与加密镜像无关",
}


def _scan(root: Path) -> list[tuple[Path, int]]:
    """列出会进上下文的文件与大小；跳过 __pycache__ 与 .git。"""
    out: list[tuple[Path, int]] = []
    for item in CODE_ITEMS + DATA_ITEMS:
        src = root / item
        if not src.exists():
            continue
        if src.is_file():
            out.append((Path(item), src.stat().st_size))
            continue
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            parts = set(p.relative_to(root).parts)
            if "__pycache__" in parts or ".git" in parts:
                continue
            out.append((p.relative_to(root), p.stat().st_size))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="组装加密镜像的构建上下文")
    ap.add_argument("--out", default=str(WIKI_ROOT / ".sealed_context"),
                    help="输出目录（默认 .sealed_context/）")
    ap.add_argument("--list", action="store_true", help="只列出会打包什么，不实际复制")
    a = ap.parse_args()

    files = _scan(WIKI_ROOT)
    data_bytes = sum(sz for rel, sz in files
                     if rel.parts and rel.parts[0] in DATA_ITEMS)
    code_bytes = sum(sz for rel, sz in files
                     if rel.parts and rel.parts[0] not in DATA_ITEMS)

    print("=" * 74)
    print("加密镜像构建上下文")
    print("=" * 74)
    print(f"  {'（仅列出）' if a.list else '将复制到' + str(a.out)}")
    print()
    print(f"── 代码与文档（{len([f for f in files if f[0].parts[0] not in DATA_ITEMS])} 个文件）")
    for item in CODE_ITEMS:
        n = len([f for f in files if f[0].parts and f[0].parts[0] == item.rstrip("/")])
        if n:
            print(f"     {item:<36} {n:>4} 个文件")
    print()
    print(f"── 真实数据（{len([f for f in files if f[0].parts[0] in DATA_ITEMS])} 个文件）⚠️ 机密")
    for item in DATA_ITEMS:
        sel = [f for f in files if f[0].parts and f[0].parts[0] == item]
        if sel:
            mb = sum(sz for _, sz in sel) / 1024 / 1024
            print(f"     {item:<36} {len(sel):>4} 个文件  {mb:.2f} MB")
    print()
    print(f"── 刻意不带（{len(EXCLUDED)} 项）")
    for k, why in EXCLUDED.items():
        print(f"     {k:<36} {why}")
    print()
    print(f"  代码 {code_bytes/1024/1024:.2f} MB  +  数据 {data_bytes/1024/1024:.2f} MB "
          f"=  {len(files)} 个文件")

    if a.list:
        print("\n（--list：未写入任何文件）")
        return 0

    out = Path(a.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for rel, _ in files:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(WIKI_ROOT / rel, dst)

    # 上下文里**不放 .dockerignore**：那样 COPY 数据才会成功。
    # 这是有意的——基础镜像仍从仓库目录构建（那边 .dockerignore 生效）。
    print(f"\n✅ 上下文已组装：{out}")
    print("\n下一步（需要 Docker 引擎）：")
    print(f"    cd {out}")
    print("    docker build -t pharma-cost-wiki:latest .")
    print("    docker build -f Dockerfile.sealed -t pharma-cost-wiki:sealed .")
    print("    docker save pharma-cost-wiki:sealed -o ../pharma-cost-wiki-sealed.tar")
    print("\n⚠️ 交付前务必核实镜像里**真的有数据**（别交一个空壳）：")
    print("    docker run --rm --entrypoint sh pharma-cost-wiki:sealed \\")
    print("      -c 'ls -la /app/raw/csv/cost_data/ && head -2 /app/raw/csv/cost_data/*成本汇总*'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

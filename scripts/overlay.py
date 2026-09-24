# -*- coding: utf-8 -*-
"""
overlay.py — **共享基线 + 个人增量**的合并视图

它解决的问题
------------
    共享基线（代码仓库的 `wiki/`）  全体用户共用，只读
    个人工作区（`data/users/<id>/ws/`）  用户自己上传编译出来的，仅自己可见

两者**不是二选一，而是叠加**：

    读的时候 → 合并（个人同名覆盖共享）
    写的时候 → 只写个人层，**共享基线永不被用户改动**

为什么读要合并，而不是只读个人层
--------------------------------
实测踩到：个人工作区初始是空的。agent 分诊时列出词条，
看到"wiki/ 下暂无词条"，就判定"没有已有实体可承载"，
把用户上传的成本简报判成 `No material` —— **什么都没编译出来**。

它连"银黄口服液"这个实体存在都不知道，因为那篇在共享基线里。

反过来，如果只读共享层，用户自己传的东西就**永远检索不到**——
上传功能等于白做。（这是本次修复前的实际状态。）

所以必须是叠加的。

合并规则只有一条，但必须严格
----------------------------
**个人层覆盖共享层（个人优先）。**
路径统一用「相对 `wiki/` 的 posix 形式」作键，两层一致才能对上。

⚠️ 这套规则**读写两侧共用同一份实现**。
   曾经摄入侧自己写了一份、读取侧没有——规则只有一份才不会漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 来源标签。**给用户看的措辞在服务端转换**，这里只给稳定的标识符。
SRC_SHARED = "shared"
SRC_PERSONAL = "personal"

# 这两篇是导航/日志，不是可检索的知识词条
_SKIP_NAMES = ("index.md", "log.md")


def _scan(wiki_dir: Path, tag: str,
          into: Dict[str, Path], src: Dict[str, str]) -> None:
    """把某个 wiki 目录下的词条收进合并表（**后写覆盖先写**）。"""
    if not wiki_dir.exists():
        return
    for p in sorted(wiki_dir.rglob("*.md")):
        if p.name in _SKIP_NAMES:
            continue
        rel = p.relative_to(wiki_dir).as_posix()
        into[rel] = p
        src[rel] = tag


def overlay_articles(shared_root: Optional[Path] = None,
                     personal_root: Optional[Path] = None
                     ) -> List[Tuple[Path, str]]:
    """
    合并「共享基线 + 个人增量」→ `[(实际路径, 来源标签)]`，按相对路径排序。

    :param shared_root:   代码仓库根（其下有 `wiki/`）。None 表示没有共享层。
    :param personal_root: 某个用户的个人工作区（其下有 `wiki/`）。
                          ⚠️ **必须是当前调用者自己的**——传错了就是跨用户泄漏。

    顺序即优先级：先收共享，再收个人 → **个人同名覆盖共享**。
    """
    merged: Dict[str, Path] = {}
    src: Dict[str, str] = {}

    if shared_root is not None:
        _scan(Path(shared_root) / "wiki", SRC_SHARED, merged, src)
    if personal_root is not None:
        _scan(Path(personal_root) / "wiki", SRC_PERSONAL, merged, src)

    return [(merged[k], src[k]) for k in sorted(merged)]


def overlay_lookup(rel_path: str,
                   shared_root: Optional[Path] = None,
                   personal_root: Optional[Path] = None
                   ) -> Optional[Tuple[Path, str]]:
    """
    按相对路径找**一篇**词条，返回 `(实际路径, 来源标签)`；找不到返回 None。

    顺序：**个人层优先**，再回退共享层 —— 这就是 overlay 的读语义。
    名称回退（用户只给了文件名、没给目录）也是同样的优先级。
    """
    if not rel_path:
        return None
    rel = Path(str(rel_path)).as_posix().lstrip("./")
    name = Path(rel).name

    # ① 精确路径：个人层在前
    for base, tag in ((personal_root, SRC_PERSONAL), (shared_root, SRC_SHARED)):
        if base is None:
            continue
        p = Path(base) / "wiki" / rel
        if p.exists() and p.is_file():
            return p, tag

    # ② 名称回退（同样个人优先）
    for base, tag in ((personal_root, SRC_PERSONAL), (shared_root, SRC_SHARED)):
        if base is None:
            continue
        w = Path(base) / "wiki"
        if w.exists():
            hit = list(w.rglob(name))
            if hit:
                return hit[0], tag
    return None


def overlay_roots(root: Optional[Path],
                  personal: Optional[Path]) -> Tuple[Optional[Path], Optional[Path]]:
    """
    归一化两个根。

    常见误用是**把两个根传成同一个值**——那会让个人层覆盖共享层时
    读写同一批文件，等于用户能改共享基线（越权）。
    这里显式判等并拒绝。
    """
    r = Path(root) if root else None
    p = Path(personal) if personal else None
    if r is not None and p is not None:
        try:
            if r.resolve() == p.resolve():
                raise ValueError(
                    "overlay 的两个根不能相同：共享层与个人层必须是不同目录，"
                    "否则用户上传会写进共享基线（越权）。")
        except OSError:
            pass                    # resolve 失败（路径不存在）就跳过这项检查
    return r, p


# ---------------------------------------------------------------------------
# 自测：不读真实数据，只验合并与优先级规则
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    shared, personal = tmp / "shared", tmp / "personal"
    for base in (shared, personal):
        (base / "wiki" / "costs").mkdir(parents=True, exist_ok=True)
    (shared / "wiki" / "costs" / "基线.md").write_text("# 基线", encoding="utf-8")
    (shared / "wiki" / "costs" / "同名.md").write_text("# 共享版", encoding="utf-8")
    (personal / "wiki" / "costs" / "同名.md").write_text("# 个人版", encoding="utf-8")
    (personal / "wiki" / "costs" / "独有.md").write_text("# 独有", encoding="utf-8")
    (personal / "wiki" / "index.md").write_text("# 索引", encoding="utf-8")

    print("=" * 70)
    print("overlay 自测")
    print("=" * 70)
    arts = overlay_articles(shared, personal)
    print(f"合并后 {len(arts)} 篇（index.md 应被排除）：")
    for p, s in arts:
        print(f"  [{s:<8}] costs/{p.name}  ← {p.read_text(encoding='utf-8')}")

    ok = True
    names = {p.name: s for p, s in arts}
    ok &= names.get("基线.md") == SRC_SHARED
    ok &= names.get("独有.md") == SRC_PERSONAL
    # 同名：个人优先
    hit = overlay_lookup("costs/同名.md", shared, personal)
    ok &= hit is not None and hit[1] == SRC_PERSONAL
    print(f"\n同名覆盖 → {overlay_lookup('costs/同名.md', shared, personal)}")
    # 只给文件名也能找到
    print(f"名称回退 → {overlay_lookup('基线.md', shared, personal)}")
    # 找不到
    print(f"不存在的 → {overlay_lookup('没有这篇.md', shared, personal)}")
    # 两个根相同必须报错
    try:
        overlay_roots(shared, shared)
        print("\n❌ 两个根相同竟然没报错")
        ok = False
    except ValueError as e:
        print(f"\n✅ 两个根相同被拒：{str(e)[:40]}…")

    print("\n" + ("✅ 全部通过" if ok else "❌ 有失败项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())

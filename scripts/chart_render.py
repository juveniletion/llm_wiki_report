# -*- coding: utf-8 -*-
"""
chart_render.py — **服务端绘图**（把看板上的图重绘成 PNG，供 Word/PDF 嵌入）

为什么需要它
------------
赛题 5.1.3 要求「格式需保持专业排版（页眉页脚、表格样式、**图表嵌入**）」。
但看板的图是 **ECharts 画在 canvas 上**的——`python-docx` 拿不到，
导出的 Word 里 `inline_shapes` 是 0。

两条路：
    ① 前端 `getDataURL()` 出 PNG 再传给服务端 —— 图与看板一致，但要前端配合，
       且导出流程变成两段（浏览器 → 服务端），离线跑 `report_build` 时没有图。
    ② **服务端用同一份数据重绘** —— 导出是完全独立的，命令行/API/定时任务
       都能出带图的文档。

本项目选了 ②：导出是**确定性产物**，不该依赖"当时有个浏览器开着"。

⚠️ 中文字体是这个模块最容易翻车的地方
------------------------------------
matplotlib 默认字体（DejaVu Sans）**没有汉字**，不设字体的话
所有中文会渲染成一排方框（□□□），而且**不报任何错**——
图生成了、尺寸正常、看起来成功，打开才发现全是豆腐块。

所以 `_setup_font()` 是必须的第一件事，且**找不到字体要显式失败**，
不能静默降级。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ---- 与看板一致的配色（同一套视觉语言，图放进文档里才对得上）----
C_INK = "#1C1917"
C_MUTED = "#78716C"
C_FAINT = "#A8A29E"
C_LINE = "#E7E5E4"
C_ACCENT = "#0F766E"
SERIES_COLORS = ["#0F766E", "#0891B2", "#F59E0B", "#6366F1", "#C2410C"]

# 中文字体候选。Windows 上 msyh(微软雅黑) 最稳；黑体作备选。
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",      # 微软雅黑
    "C:/Windows/Fonts/simhei.ttf",    # 黑体
    "C:/Windows/Fonts/simsun.ttc",    # 宋体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux
    "/System/Library/Fonts/PingFang.ttc",                       # macOS
]

_font_ready = False


def _setup_font() -> str:
    """
    配置中文字体。**找不到就抛异常**，不静默降级。

    ⚠️ 静默降级的后果很隐蔽：图照常生成、尺寸正常、返回成功，
       但里面所有汉字都是方框——**只有人眼看才发现**。
       宁可在这里失败，也好过产出一份看起来正常、实则不可读的文档。
    """
    global _font_ready
    import matplotlib
    matplotlib.use("Agg")              # 无 GUI 后端；服务器上没有显示器
    from matplotlib import font_manager

    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            try:
                font_manager.fontManager.addfont(p)
                name = font_manager.FontProperties(fname=p).get_name()
                matplotlib.rcParams["font.family"] = name
                matplotlib.rcParams["axes.unicode_minus"] = False  # 负号不显示成方框
                _font_ready = True
                return name
            except Exception:  # noqa: BLE001
                continue
    raise RuntimeError(
        "找不到可用的中文字体，图表里的汉字会渲染成方框。\n"
        "  已尝试：" + "、".join(_FONT_CANDIDATES))


def _style_axes(ax: Any) -> None:
    """统一坐标轴观感：去掉上/右边框、弱化网格——与看板风格一致。"""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(C_LINE)
    ax.tick_params(colors=C_FAINT, labelsize=9, length=0)
    ax.grid(axis="y", color="#F0EFED", linestyle="--", linewidth=0.8)
    ax.set_axisbelow(True)


def _save(fig: Any, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 三张核心图
# ---------------------------------------------------------------------------

def render_trend(series: List[Dict[str, Any]], product: str,
                 out: Path) -> Path:
    """
    逐月单位成本趋势（折线）。

    `series` 是 `/api/trend` 那种结构：每项含 月份 / 单位成本 / 单位材料 / …
    """
    _setup_font()
    import matplotlib.pyplot as plt

    keys = [("单位成本", "单位成本"), ("单位材料", "直接材料"),
            ("单位人工", "直接人工"), ("单位制造费用", "制造费用")]
    xs = [str(r.get("月份", ""))[5:7] + "月" for r in series]

    fig, ax = plt.subplots(figsize=(7.4, 3.0))
    for i, (k, label) in enumerate(keys):
        ys = [r.get(k) for r in series]
        if all(v is None for v in ys):
            continue
        ax.plot(xs, ys, marker="o", markersize=4.5, linewidth=2.0,
                color=SERIES_COLORS[i], label=label,
                markerfacecolor="white", markeredgewidth=1.8)
    ax.set_title(f"{product} 逐月单位成本走势", color=C_INK,
                 fontsize=12, fontweight="bold", pad=12)
    ax.set_ylabel("元 / 盒", color=C_MUTED, fontsize=9.5)
    _style_axes(ax)
    ax.legend(frameon=False, fontsize=9, labelcolor=C_MUTED,
              ncol=len(keys), loc="upper center", bbox_to_anchor=(0.5, -0.16))
    return _save(fig, out)


def render_structure(items: List[Tuple[str, float]], total: float,
                     product: str, unit: str, out: Path) -> Path:
    """
    本月成本构成（环形）。

    ⚠️ 环形中心放总额——和看板一致。实心饼图中间空着，
       那个位置正好用来放"总共有多少"，不然读者要自己加。
    """
    _setup_font()
    import matplotlib.pyplot as plt

    labels = [n for n, _ in items]
    vals = [v for _, v in items]
    colors = SERIES_COLORS[:len(items)]

    fig, ax = plt.subplots(figsize=(4.6, 3.4))
    wedges, _texts, autotexts = ax.pie(
        vals, labels=None, autopct="%1.1f%%",
        colors=colors, startangle=90, counterclock=False,
        wedgeprops=dict(width=0.34, edgecolor="white", linewidth=2),
        pctdistance=0.79,
        textprops=dict(color=C_INK, fontsize=9.5, fontweight="bold"))
    for t in autotexts:
        t.set_color(C_INK)
    # 中心总额
    ax.text(0, 0.08, f"{total:.2f}", ha="center", va="center",
            fontsize=19, fontweight="bold", color=C_INK)
    ax.text(0, -0.18, unit, ha="center", va="center",
            fontsize=9, color=C_FAINT)
    ax.set_title(f"{product} 本月成本构成", color=C_INK,
                 fontsize=12, fontweight="bold", pad=10)
    ax.legend(wedges, labels, frameon=False, fontsize=9,
              labelcolor=C_MUTED, loc="center left",
              bbox_to_anchor=(0.98, 0.5))
    return _save(fig, out)


def render_peer(rows: List[Dict[str, Any]], product: str, out: Path) -> Path:
    """
    一厂 vs 二厂 成本要素对比（分组柱状）。

    ⚠️ 只画**成本要素**四项，**不画单位成本**——单位成本是三项之和，
       放进去会让它的柱子比别的高两倍多，其余四项被压扁成一条线，
       图就看不出要素间的差别了。合计在表格里已有。
    """
    _setup_font()
    import matplotlib.pyplot as plt

    elems = [r for r in rows if r.get("成本要素") != "单位成本"]
    if not elems:
        elems = rows
    labels = [str(r.get("成本要素", "")) for r in elems]
    a = [r.get("一厂") or 0 for r in elems]
    b = [r.get("二厂") or 0 for r in elems]

    n = len(labels)
    x = list(range(n))
    w = 0.36
    fig, ax = plt.subplots(figsize=(6.6, 3.0))
    ax.bar([i - w / 2 for i in x], a, w, label="中药一厂",
           color=C_ACCENT, zorder=3)
    ax.bar([i + w / 2 for i in x], b, w, label="中药二厂",
           color="#6366F1", zorder=3)
    # 柱顶标数值——比让读者对刻度线估读准得多
    for i, (va, vb) in enumerate(zip(a, b)):
        ax.text(i - w / 2, va, f"{va:.2f}", ha="center", va="bottom",
                fontsize=8.5, color=C_MUTED)
        ax.text(i + w / 2, vb, f"{vb:.2f}", ha="center", va="bottom",
                fontsize=8.5, color=C_MUTED)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=C_MUTED, fontsize=9.5)
    ax.set_ylabel("元 / 盒", color=C_MUTED, fontsize=9.5)
    ax.set_title(f"{product}　一厂 vs 二厂 成本要素对比", color=C_INK,
                 fontsize=12, fontweight="bold", pad=12)
    _style_axes(ax)
    ax.legend(frameon=False, fontsize=9, labelcolor=C_MUTED,
              ncol=2, loc="upper right")
    return _save(fig, out)


# ---------------------------------------------------------------------------
# 自测：真的生成三张图，并检查**不是空白**（防止静默产出豆腐块或空图）
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
    out_dir = Path(tempfile.mkdtemp())
    print("=" * 70)
    print(f"chart_render 自测 → {out_dir}")
    print("=" * 70)
    try:
        print("  字体:", _setup_font())
    except RuntimeError as e:
        print("  ❌", e)
        return 1

    ok = True
    try:
        p = render_trend(
            [{"月份": f"2026-0{i}", "单位成本": 10 + i * 0.1,
              "单位材料": 6.5 + i * 0.05, "单位人工": 1.5,
              "单位制造费用": 2.3} for i in range(1, 7)],
            "银黄口服液", out_dir / "trend.png")
        print(f"  ✅ 折线 {p.name}  {p.stat().st_size:,} B")
    except Exception as e:  # noqa: BLE001
        print("  ❌ 折线:", e); ok = False

    try:
        p = render_structure(
            [("直接材料", 7.30), ("直接人工", 1.53), ("制造费用", 2.38)],
            11.21, "银黄口服液", "元/盒", out_dir / "donut.png")
        print(f"  ✅ 环形 {p.name}  {p.stat().st_size:,} B")
    except Exception as e:  # noqa: BLE001
        print("  ❌ 环形:", e); ok = False

    try:
        p = render_peer(
            [{"成本要素": "单位成本", "一厂": 11.21, "二厂": 11.60},
             {"成本要素": "直接材料", "一厂": 7.30, "二厂": 7.29},
             {"成本要素": "直接人工", "一厂": 1.53, "二厂": 1.73},
             {"成本要素": "制造费用", "一厂": 2.38, "二厂": 2.58}],
            "银黄口服液", out_dir / "peer.png")
        print(f"  ✅ 柱状 {p.name}  {p.stat().st_size:,} B")
    except Exception as e:  # noqa: BLE001
        print("  ❌ 柱状:", e); ok = False

    # 尺寸检查：纯白图会非常小（压缩率高），能作为"没画出东西"的粗筛
    for f in out_dir.glob("*.png"):
        if f.stat().st_size < 3000:
            print(f"  ⚠️ {f.name} 只有 {f.stat().st_size} B，可能是空白图")

    print("\n" + ("✅ 自测通过" if ok else "❌ 有失败项"))
    print(f"请**打开这几张图肉眼确认**：{out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest())

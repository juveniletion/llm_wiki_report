# -*- coding: utf-8 -*-
"""
export_doc.py — **文档导出**（Markdown → Word / PDF）

赛题 5.1.3 原文：
    支持 PDF 和 Word 双格式导出，格式需保持专业排版
    （页眉页脚、表格样式、图表嵌入）

两条路径，各有分工
------------------
    Word   `python-docx`  —— **精确可控**：页眉页脚、表格样式、字体都能指定
    PDF    `pandoc + xelatex` —— 排版质量最高，但中文有专门的坑（见下）

⚠️ 中文 PDF 的两个坑（实测踩过，都验过）
---------------------------------------
① **必须用 `-H header.tex` 加载 xeCJK**，只写 `-V CJKmainfont=` 是**不够**的：
   实测仅用 `-V` 参数时，PDF 里中文完全不渲染。
   （pandoc 2.12 的默认 LaTeX 模板不含中文支持。）

② **不能用 `pdftotext` 验证中文是否显示** —— 本库实测：
   PDF 视觉上中文完全正常，但 `pdftotext` 提取出来是空白，
   因为 xdvipdfmx 生成的字体**缺 ToUnicode 反向映射**。
   验证方法必须是 **`pdftoppm` 转图 + 肉眼看**，或者看 pdf 里的字体对象。
   ⇒ **验证方法本身也会骗人**，这是本次最有价值的一条经验。

用法
----
    python scripts/export_doc.py --md reports/银黄口服液_2026-05.md --to docx
    python scripts/export_doc.py --md reports/银黄口服液_2026-05.md --to pdf
    python scripts/export_doc.py --md x.md --to both --out-dir exports/
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent
EXPORT_DIR = WIKI_ROOT / "exports"

# 中文字体：用**字体名**而非路径 —— msyh.ttc 是 TTC 集合，
# fontspec 走系统字体库比直接指路径稳。
#
# ⚠️ 这里踩过一个真事故，教训比代码本身值钱：
#
#   第一版按**平台**猜字体名（win32→Microsoft YaHei / 其它→Noto Sans CJK SC）。
#   看起来合理，实际上**猜错了**：容器导出 docx 时写进的是
#   `Noto Sans CJK SC`，而 Windows 上装的 Noto 中文家族叫 `Noto Sans SC`
#   ——**名字对不上**。Word 找不到就静默回退到默认字体，
#   于是"在容器里导出、拿回 Windows 打开"的文档字体全乱。
#
#   ⇒ **不要按平台猜字体名**。字体名是否有效，取决于**实际装了哪个字体**，
#     跟平台没有必然关系：自定义镜像、精简版系统、Linux 桌面发行版，
#     三种平台都可能装不同的名字。
#
#   正确做法（本函数）：**按真实字体文件探测**，探测不到再回退到名字表。
#     ① env `CJK_FONT` 显式指定 → 最优先（部署时可强制）
#     ② 按字体文件是否存在，映射到**该文件真实的家族名**
#     ③ 都探不到 → 按平台给一个保守默认（至少名字是常见拼写）
#
#   为什么按"文件"而不是按"字体名"探测：字体名需要查询系统字体库
#   （fontconfig / Windows GDI），而**文件路径**是确定的、无需依赖；
#     msyh.ttc / NotoSansCJK-Regular.ttc 这些文件名在各平台是稳定的。
_CJK_FONT_BY_FILE: List[Tuple[str, str]] = [
    # (字体文件路径, 该文件真实的字体家族名)
    ("C:/Windows/Fonts/msyh.ttc",                  "Microsoft YaHei"),
    ("C:/Windows/Fonts/simhei.ttf",                "SimHei"),
    ("C:/Windows/Fonts/NotoSansSC-VF.ttf",         "Noto Sans SC"),      # ← Windows 这版的名字
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                                                   "Noto Sans CJK SC"),  # ← Debian/容器这版的名字
    ("/System/Library/Fonts/PingFang.ttc",         "PingFang SC"),
]
_CJK_FONT_FALLBACK = {"win32": "Microsoft YaHei", "darwin": "PingFang SC"}


def _detect_cjk_font() -> str:
    """探测本机**实际可用**的中文字体名（见上方事故说明）。"""
    env = os.getenv("CJK_FONT")
    if env:
        return env
    for path, family in _CJK_FONT_BY_FILE:
        if Path(path).exists():
            return family
    return _CJK_FONT_FALLBACK.get(sys.platform, "Noto Sans CJK SC")


CJK_FONT = _detect_cjk_font()

# xeCJK 头文件 —— PDF 中文渲染的关键
LATEX_HEADER = r"""
\usepackage{xeCJK}
\setCJKmainfont{%(font)s}
\setCJKsansfont{%(font)s}
\setCJKmonofont{%(font)s}
%% 中英混排时的断行规则：不设这个，整段中文可能不换行
\XeTeXlinebreaklocale "zh"
\XeTeXlinebreakskip = 0pt plus 1pt
%% 表格里的中文更需要空间
\usepackage{longtable,booktabs,array}
%% 代码块（报告里偶尔有）
\usepackage{fancyvrb}
%% ⚠️ 插图必须显式加载 graphicx。pandoc 走 markdown `![]()` 时会自动加，
%%    但我们**绕过它、直接写 raw LaTeX**（为了不被套上 Figure N 题注），
%%    那就得自己加载——否则报 `Undefined control sequence \includegraphics`。
\usepackage{graphicx}
%% 图注用灰色——纯黑会跟正文抢注意力
\usepackage{xcolor}
""" % {"font": CJK_FONT}


# ---------------------------------------------------------------------------
# Word
# ---------------------------------------------------------------------------

def _insert_image(doc: Any, ch: Dict[str, Any], _cn: Any,
                  width_cm: float = 14.5) -> None:
    """
    往文档里插一张图 + 图注。

    ⚠️ 两个坑：
       ① 图片必须**限宽**。`add_picture` 默认按原始像素尺寸放，
          160 dpi 的图会超出页边距。
       ② `WD_ALIGN_PARAGRAPH` / `Cm` / `RGBColor` 都是**函数内 import**
          （见 `to_docx` 开头）。本函数是模块级的，**必须自己 import**，
          否则运行时 NameError —— 而且只在真的插图时才炸，
          "不带图导出正常"会让人误以为没问题。
    """
    img = ch.get("image")
    if not img or not Path(img).exists():
        return
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm, RGBColor

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run().add_picture(str(img), width=Cm(width_cm))
    cap = ch.get("caption")
    if cap:
        cp = doc.add_paragraph()
        cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = cp.add_run(cap)
        _cn(r, 9)
        r.font.color.rgb = RGBColor(0x78, 0x71, 0x6C)


def _outline(par: Any, level: int) -> None:
    """
    给段落设**大纲级别**（`w:outlineLvl`），而不是用 `Heading N` 样式。

    ⚠️ 为什么不用 Heading 样式：赛题官方模板就是这么做的（实测它的段落
       `style=None` 但 `outlineLvl=1/2/3`）。**Word 的目录域（TOC）靠大纲级别
       抓取条目**——只设样式名字、不设大纲级别，有的模板下目录会是空的。
       同时设两样也行，这里是跟官方保持一致。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = par._element.get_or_add_pPr()
    lvl = OxmlElement("w:outlineLvl")
    lvl.set(qn("w:val"), str(max(0, min(level, 8))))
    pPr.append(lvl)


def _add_toc_field(doc: Any, _cn: Any,
                   entries: Optional[List[Tuple[int, str]]] = None) -> None:
    """
    插入 Word **目录域**，并**预填条目作为域的缓存结果**。

    一个域有两部分：指令（`instrText`）和**缓存结果**（begin 与 end 之间的内容）。
    Word 更新域时用指令重算并覆盖缓存；**别的阅读器（WPS、预览、转换工具）
    不更新域，显示的正是缓存**。

    ⚠️ 这里踩了两个坑，都是"Word 里正常、别处空白"：

    ① **`w:dirty="true"` 不能省**。域默认不是 dirty 的，Word 打开文档时
       **不会自动重算**它，要用户手动 F9——实测打开就是一片空白，
       而文档里还写着"请按 F9 更新"，等于把缺陷写在了交付物上。
       标了 dirty，Word/WPS 打开即自动填充，无需用户操作。
       （pandoc 生成 docx 目录就是这么做的，本次实测是照它的做法对齐。）

    ② **缓存结果不能空**。只写指令不写结果，凡是不重算域的阅读器都显示空白。
       所以按 `entries` 预填一份条目。**页码故意不填**——docx 的分页由 Word 的
       排版引擎决定，我们算不出来；乱填一个数是"看起来对了"的错，
       比留空更坏。缓存里给的是**缩进的章节列表**，Word 一更新就换成带页码的真目录。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Cm

    p = doc.add_paragraph()
    r1 = p.add_run()
    b = OxmlElement("w:fldChar")
    b.set(qn("w:fldCharType"), "begin")
    b.set(qn("w:dirty"), "true")          # ← 坑①：不加 Word 不会自动重算
    r1._r.append(b)

    r2 = p.add_run()
    it = OxmlElement("w:instrText"); it.set(qn("xml:space"), "preserve")
    # \o "1-3" 收 1-3 级；\h 超链接；\z 网页视图隐藏页码；\u 按大纲级别
    it.text = r' TOC \o "1-3" \h \z \u '
    r2._r.append(it)

    r3 = p.add_run()
    sep = OxmlElement("w:fldChar"); sep.set(qn("w:fldCharType"), "separate")
    r3._r.append(sep)
    for r in (r1, r2, r3):
        _cn(r, 10.5)

    # ---- 域的缓存结果：缩进的章节列表（坑②）----
    # 这一段的段落**必须在域内**（begin..end 之间），所以先写缓存、再写 end。
    for lv, txt in (entries or []):
        ep = doc.add_paragraph()
        ep.paragraph_format.left_indent = Cm(0.55 * (lv - 1))
        ep.paragraph_format.space_after = None
        er = ep.add_run(txt)
        _cn(er, 10.5 - 0.5 * (lv - 1), bold=(lv == 1))

    tail = doc.add_paragraph()
    rt = tail.add_run()
    e = OxmlElement("w:fldChar"); e.set(qn("w:fldCharType"), "end")
    rt._r.append(e)
    _cn(rt, 10.5)


def md_toc_entries(md_path: Path) -> List[Tuple[int, str]]:
    """
    从 markdown 里取目录条目 `(大纲级别, 标题)`，**给 docx 目录域做缓存结果**。

    级别换算必须与正文一致（见 `to_docx` 里设大纲级别那段）：
        `##章` → 大纲 1、`###节` → 2、`####小节` → 3；
        `#` 一级标题是文档名，**不进口录**。
    """
    out: List[Tuple[int, str]] = []
    try:
        lines = md_path.read_text(encoding="utf-8").split("\n")
    except OSError:
        return out
    for line in lines:
        m = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
        if m:
            out.append((len(m.group(1)) - 1, m.group(2).replace("**", "").strip()))
    return out


def _front_matter(doc: Any, _cn: Any, title: str, date_str: str,
                  entries: Optional[List[Tuple[int, str]]] = None) -> None:
    """
    报告前置部分：封面页 → 文档控制 → 阅读指南 → 目录。

    结构参考赛题官方模板（`04_报告模板/月度成本分析报告模板.docx`），
    但**内容改写为药厂语境**：

    ⚠️ 官方模板写的是「《…》整体解决方案」「重庆创灵境数字技术有限公司」
       「文件版本 V3.0」——那是**出题方（软件供应商）**的文档。
       我们的报告是**药厂的成本分析**，照搬会张冠李戴。
       所以：编制单位用「中药一厂 财务部」，去掉"解决方案"字样，版本从 V1.0 起。
    """
    from docx.shared import Pt, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    def line(text, size=11, bold=False, align="center", color=None, space=6):
        p = doc.add_paragraph()
        p.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER,
                       "left": WD_ALIGN_PARAGRAPH.LEFT}[align]
        r = p.add_run(text)
        _cn(r, size, bold=bold)
        if color:
            r.font.color.rgb = color
        p.paragraph_format.space_after = Pt(space)
        return p

    C_MUTED = RGBColor(0x78, 0x71, 0x6C)

    # ---- 封面 ----
    main, sub = _cover_title_lines(title)
    for _ in range(6):
        doc.add_paragraph()
    line(f"《{main}》", 19, bold=True, space=8)
    # 副标题取 H1 里「—」后的产品名。**不能写死"月度产品成本多维深度分析报告"**：
    # 报告的 H1 本身就含这句，照写会在封面出现两行同样的话。
    if sub:
        line(f"—— {sub} ——", 13, space=22, color=C_MUTED)
    line("文件版本： V1.0", 11, space=4)
    line(f"编制日期： {date_str}", 11, space=4)
    line("编制单位： 中药一厂 财务部", 11, space=4)
    doc.add_page_break()

    # ---- 文档控制 ----
    # 前置块的三个标题**不设大纲级别**：它们与「目录」同级，
    # 若进目录会变成「目录 → 文档控制 → 阅读指南 → 目录」这种自己指自己的怪结构。
    h = doc.add_paragraph(); r = h.add_run("文档控制"); _cn(r, 15, bold=True)
    for label, val in (("版本记录", "V1.0　首次发布"),
                       ("查阅", "财务部、生产部、采购部、质量部"),
                       ("分发", "厂内管理评审；不外发")):
        p = doc.add_paragraph()
        rr = p.add_run(f"{label}："); _cn(rr, 10.5, bold=True)
        rv = p.add_run(val); _cn(rv, 10.5)
        p.paragraph_format.space_after = Pt(3)
    doc.add_paragraph()

    # ---- 阅读指南 ----
    h = doc.add_paragraph(); r = h.add_run("阅读指南"); _cn(r, 15, bold=True)
    p = doc.add_paragraph()
    _cn(p.add_run("为便于不同角色快速抓住重点，建议按以下路径阅读："), 10.5)
    for role, path in (("管理层", "先看「二、总成本概览」与「六、总结与建议」"),
                       ("财务", "重点看「三、成本要素明细分析」与「五、对标分析」"),
                       ("生产", "重点看「三、成本要素明细分析」与「四、重点产品专项分析」"),
                       ("采购", "重点看「三、直接材料分析」与「四、关键原材料市场价格跟踪」")):
        p = doc.add_paragraph(style="List Bullet")
        rr = p.add_run(f"{role}："); _cn(rr, 10.5, bold=True)
        rv = p.add_run(path); _cn(rv, 10.5)
    doc.add_page_break()

    # ---- 目录 ----
    h = doc.add_paragraph(); r = h.add_run("目　录"); _cn(r, 15, bold=True)
    _add_toc_field(doc, _cn, entries)
    doc.add_page_break()


def to_docx(md_path: Path, out_path: Path,
            title: str = "", subtitle: str = "",
            charts: Optional[List[Dict[str, Any]]] = None,
            front_matter: bool = False) -> Path:
    """
    Markdown → Word。

    ⚠️ python-docx 的**中文字体坑**：
       `run.font.name = "微软雅黑"` 只作用于**西文**部分，
       中文会退回默认的宋体。必须额外设 `w:eastAsia`：
           run._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
       这是 python-docx 的已知行为（OOXML 里中西文是两套字体属性）。
    """
    import docx
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn

    doc = docx.Document()

    # ---- 页面与页边距 ----
    # 左右 3.2cm：对齐赛题官方模板（月度成本分析报告模板.docx）。
    # 之前用 2.2cm 四边一致，是把"网页阅读"的舒适感带进了正式文档——
    # 正式报告两侧留白要更宽（装订线 + 阅读节奏）。
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(2.5)
        s.left_margin = s.right_margin = Cm(3.2)

    # ---- 默认字体：中西文都要设 ----
    normal = doc.styles["Normal"]
    normal.font.name = CJK_FONT
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)

    def _cn(run, size: Optional[float] = None, bold: Optional[bool] = None):
        """给一个 run 同时设中西文字体（否则中文会变宋体）。"""
        run.font.name = CJK_FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
        if size:
            run.font.size = Pt(size)
        if bold is not None:
            run.font.bold = bold

    # ---- 页眉页脚 ----
    if title:
        hdr = doc.sections[0].header.paragraphs[0]
        hdr.text = title
        hdr.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for r in hdr.runs:
            _cn(r, 9)

    # 页脚：第 X 页 / 共 Y 页
    #
    # ⚠️ 页码不是普通文本，是**域（field）**——`PAGE` / `NUMPAGES`。
    #    直接写 "第 1 页" 是死的，打印出来每页都显示 1。
    #    域要用 `fldChar`(开始) + `instrText`(指令) + `fldChar`(结束) 三段拼。
    #    赛题 5.1.3 明确写了「页眉页脚」，空着说不过去。
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn as _qn

    def _add_field(par, code: str) -> None:
        """在段落里插入一个 Word 域（如 PAGE / NUMPAGES）。"""
        r1 = par.add_run()
        fld_begin = OxmlElement("w:fldChar")
        fld_begin.set(_qn("w:fldCharType"), "begin")
        r1._r.append(fld_begin)

        r2 = par.add_run()
        instr = OxmlElement("w:instrText")
        instr.set(_qn("xml:space"), "preserve")
        instr.text = f" {code} "
        r2._r.append(instr)

        r3 = par.add_run()
        fld_end = OxmlElement("w:fldChar")
        fld_end.set(_qn("w:fldCharType"), "end")
        r3._r.append(fld_end)
        # 域里的 run 也要设中文字体，否则两侧"第/页"会变宋体
        for r in (r1, r2, r3):
            _cn(r, 9)

    ftr = doc.sections[0].footer.paragraphs[0]
    ftr.alignment = WD_ALIGN_PARAGRAPH.CENTER
    ftr.text = ""
    _cn(ftr.add_run("第 "), 9)
    _add_field(ftr, "PAGE")
    _cn(ftr.add_run(" 页 / 共 "), 9)
    _add_field(ftr, "NUMPAGES")
    _cn(ftr.add_run(" 页"), 9)

    # ---- 报告前置：封面 / 文档控制 / 阅读指南 / 目录 ----
    if front_matter:
        _front_matter(doc, _cn, title or title_from_md(md_path),
                      datetime.now().strftime("%Y-%m-%d"),
                      md_toc_entries(md_path))

    lines = md_path.read_text(encoding="utf-8").split("\n")
    i = 0
    table_buf: List[List[str]] = []

    def flush_table():
        """把缓冲的 markdown 表格写成 Word 表格。"""
        nonlocal table_buf
        if not table_buf:
            return
        rows = [r for r in table_buf if not re.match(r"^\|[\s:|-]+\|$", "|" + "|".join(r) + "|")]
        if not rows:
            table_buf = []
            return
        ncol = max(len(r) for r in rows)
        t = doc.add_table(rows=0, cols=ncol)
        t.style = "Light Grid Accent 1"

        # ---- 列宽按内容分配 ----
        # Word 默认**均分列宽**，于是「任务标题」这种长文本列被挤成一条竖线：
        # 实测 6.4 表每行折成 8 行、三行就占掉大半页。
        # 按内容宽度加权分，长文本列自然拿到更多宽度。
        widths = _col_widths(rows, ncol)
        t.autofit = False
        for ri, row in enumerate(rows):
            cells = t.add_row().cells
            for ci in range(ncol):
                txt = row[ci].strip() if ci < len(row) else ""
                txt = txt.replace("**", "").replace("`", "")
                cells[ci].text = ""
                cells[ci].width = Cm(widths[ci])
                p = cells[ci].paragraphs[0]
                r = p.add_run(txt)
                _cn(r, 9, bold=(ri == 0))
        table_buf = []
        doc.add_paragraph()

    for raw in lines:
        line = raw.rstrip()

        # 表格行：累积到缓冲区，遇到非表格行再 flush
        if line.strip().startswith("|"):
            table_buf.append([c for c in line.strip().strip("|").split("|")])
            i += 1
            continue
        flush_table()

        if not line.strip():
            continue
        # 分隔线（`---` / `***` / `___`）—— 是排版标记，不该当正文段落
        if re.match(r"^\s*([-*_])\1{2,}\s*$", line):
            continue
        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            lvl = min(len(m.group(1)), 4)
            heading_text = m.group(2).strip()
            # 大纲级别 —— **目录域靠它抓取条目**。
            # markdown 的 `##章` → 大纲 1；`###节` → 2；`####小节` → 3。
            #
            # ⚠️ `#` 一级标题（文档主标题）**不进目录**：
            #    它跟前置块里的"文档控制""阅读指南""目录"是同一层级的标题，
            #    进了目录纯属噪声。
            #
            # ⚠️⚠️ 但**光"不设 outlineLvl"不够**：`add_heading(level=1)` 会给段落
            #    挂上内置样式 `Heading 1`，而**该样式自带大纲级别 0**——
            #    于是 Word 的目录域照样收它（实测目录第一条正是文档自己的标题）。
            #    所以一级标题必须**用普通段落 + 手动格式**，不用 Heading 样式，
            #    与前置块的「文档控制」「阅读指南」「目录」保持一致。
            if lvl == 1:
                h = doc.add_paragraph()
                h.paragraph_format.space_before = Pt(12)
                h.paragraph_format.space_after = Pt(6)
            else:
                h = doc.add_heading("", level=lvl)
                _outline(h, lvl - 1)
            r = h.add_run(heading_text)
            _cn(r, {1: 17, 2: 14, 3: 12, 4: 11}[lvl], bold=True)
            # 图表锚点：这个标题后面要插图吗
            for ch in (charts or []):
                if ch.get("after", "").strip() == f"{m.group(1)} {heading_text}":
                    _insert_image(doc, ch, _cn)
            continue
        # 引用块（报告里的 ⚠️ 提示）
        if line.lstrip().startswith(">"):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.6)
            r = p.add_run(line.lstrip()[1:].strip().replace("**", ""))
            _cn(r, 9.5)
            r.font.color.rgb = RGBColor(0x78, 0x71, 0x6C)
            continue
        # 列表
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            p = doc.add_paragraph(style="List Bullet")
            r = p.add_run(m.group(1).replace("**", "").strip())
            _cn(r, 10.5)
            continue
        # 普通段落（去掉残留的行内标记）
        p = doc.add_paragraph()
        r = p.add_run(line.replace("**", "").strip())
        _cn(r, 10.5)

    flush_table()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _which(*names: str) -> Optional[str]:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def to_pdf(md_path: Path, out_path: Path,
           title: str = "", toc: bool = False) -> Path:
    """
    Markdown → PDF（pandoc + xelatex + xeCJK）。

    ⚠️ 必须 `-H header.tex` —— 只传 `-V CJKmainfont=` 中文不渲染（实测）。
    """
    pandoc = _which("pandoc")
    if not pandoc:
        raise RuntimeError("未找到 pandoc。装法：https://pandoc.org/installing.html")

    header = out_path.parent / "_cjk_header.tex"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header.write_text(LATEX_HEADER, encoding="utf-8")

    cmd = [pandoc, str(md_path), "-o", str(out_path),
           "--pdf-engine=xelatex",
           "-H", str(header),
           # 左右 3.2cm 对齐 Word 侧与赛题官方模板
           "-V", "geometry:left=3.2cm,right=3.2cm,top=2.5cm,bottom=2.5cm",
           "-V", "colorlinks=true",
           "-V", "linkcolor=teal",
           # 标题整体降一级：markdown 的 `#` 是"文档名"不是章节，
           # 不进目录。对齐 docx 侧「一级标题不设大纲级别」的做法。
           "--shift-heading-level-by=-1",
           "--standalone"]
    if title:
        # ⚠️ 只在**没有前置块**时才让 pandoc 生成标题页。
        #    有前置块时标题已在 raw LaTeX 的封面里了，再给 title 会多出一个
        #    pandoc 样式的标题页（位置在目录之后，很怪）。
        if not toc:
            cmd += ["--metadata", f"title={title}"]
    # ⚠️ **不用 pandoc 的 `--toc`**。
    #    pandoc 的模板把 `$toc$` 放在 `$body$` **最前面**，
    #    于是目录被插到我们 raw LaTeX 封面页**之前**——实测封面消失、
    #    目录排到第 1 页，而且标题是英文 "Contents"（`toc-title` 变量不起作用）。
    #    改为在前置块末尾自己写 `\tableofcontents`，位置由我们控制。
    #    （`toc` 参数仍传进来，只用于"要不要加前置块"的判断，见 export()）

    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    if r.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"PDF 生成失败（pandoc 退出码 {r.returncode}）：\n"
            + ((r.stderr or r.stdout or "")[-1200:]))
    return out_path


# ---------------------------------------------------------------------------
# mermaid → 文字版架构图
#
# ⚠️ 本机 **没有 mermaid 过滤器**（`mermaid-filter` 未安装），
#    pandoc 2.12 也不原生渲染 mermaid。直接导出的话，
#    架构图会退化成一段 mermaid **源码文本**印在 PDF 上——那不是"专业排版"。
#
# 解法：导出时把 mermaid 块换成**等价的 ASCII 架构图**。
#   取舍：ASCII 图不如渲染出的图形好看，但**信息完整、排版干净、零依赖**。
#   若将来到有 mermaid 过滤器或 Graphviz 的环境，换掉这一个函数即可。
# ---------------------------------------------------------------------------

def mermaid_to_ascii(block: str) -> str:
    """
    把简化的 mermaid 流程图转成 ASCII 框图。

    只支持本库实际用到的语法子集（`graph TB/LR` + 节点 + 箭头 + subgraph），
    不做通用 mermaid 解析——那需要一个完整的解析器，不值当。
    """
    lines = [l.rstrip() for l in block.strip().split("\n")]
    # 收集节点：形如  ID["标签"]  或  ID["标签<br/>续行"]
    node_re = re.compile(r'^\s*([A-Za-z_][\w]*)\s*\[\s*"([^"]*)"\s*\]')
    nodes: Dict[str, str] = {}
    edges: List[Tuple[str, str, str]] = []
    groups: List[Tuple[str, List[str]]] = []
    cur_group: Optional[str] = None

    for ln in lines:
        s = ln.strip()
        if not s or s.startswith(("graph ", "style ", "%%")):
            continue
        if s.startswith("subgraph "):
            m = re.match(r'subgraph\s+(\w+)\s*\[\s*"([^"]*)"\s*\]', s)
            cur_group = m.group(2) if m else s[len("subgraph "):].strip()
            groups.append((cur_group, []))
            continue
        if s == "end":
            cur_group = None
            continue
        m = node_re.match(s)
        if m:
            nid, label = m.group(1), m.group(2)
            nodes[nid] = label.replace("<br/>", " / ")
            if groups:
                groups[-1][1].append(nid)
            continue
        # 箭头： A -->|标签| B   或   A --> B   或  A & B --> C
        m = re.match(r'^\s*([\w&\s]+?)\s*--+>?\s*(?:\|([^|]*)\|)?\s*([\w&\s]+)\s*$', s)
        if m:
            srcs = [x.strip() for x in m.group(1).split("&") if x.strip()]
            label = (m.group(2) or "").strip()
            for dst in [x.strip() for x in m.group(3).split("&") if x.strip()]:
                for sc in srcs:
                    edges.append((sc, dst, label))

    out: List[str] = []
    in_group: Dict[str, str] = {}
    for gname, ids in groups:
        for i in ids:
            if i in nodes:
                in_group[i] = gname

    shown: set = set()
    for gname, ids in groups:
        members = [i for i in ids if i in nodes]
        if not members:
            continue
        out.append("+" + "-" * 3 + f" {gname} " + "-" * max(0, 44 - len(gname)))
        for i in members:
            out.append(f"|   {i:<6} {nodes[i]}")
            shown.add(i)
        out.append("+" + "-" * 52)
    rest = [i for i in nodes if i not in shown]
    if rest:
        out.append("（未分组）")
        for i in rest:
            out.append(f"    {i:<6} {nodes[i]}")
    if edges:
        out.append("")
        out.append("数据流：")
        for a, b, lab in edges:
            la = nodes.get(a, a)
            lb = nodes.get(b, b)
            arrow = f" --[{lab}]--> " if lab else " -------> "
            out.append(f"  {la} {arrow} {lb}")
    return "\n".join(out)


def replace_mermaid(md_text: str) -> str:
    """把 ```mermaid 块替换为 ASCII 架构图（供导出用）。"""
    pat = re.compile(r"^```mermaid\s*\n(.*?)^```\s*$", re.S | re.M)
    return pat.sub(lambda m: "```\n" + mermaid_to_ascii(m.group(1)) + "\n```", md_text)


# ---------------------------------------------------------------------------
# 文本 → Markdown（把纯文本报告包成可导出的 md）
# ---------------------------------------------------------------------------

def wrap_text_as_md(text: str, title: str) -> str:
    if text.lstrip().startswith("#"):
        return text
    return f"# {title}\n\n{text}\n"


def front_matter_md(title: str, date_str: str) -> str:
    """
    **PDF 路径**的前置块（markdown 形式）。

    与 docx 侧的 `_front_matter()` 内容一致，只是实现方式不同：
    PDF 走 pandoc，只能靠 markdown 表达；docx 侧可以直接操作段落对象。
    ⚠️ 两处**内容要同步改**，否则同一份报告的 Word 版和 PDF 版会长得不一样。

    ⚠️ **不能用 markdown 的 `title:` 元数据**：pandoc 会据此自动生成
       一个 `\\maketitle`（样式我们控制不了），跟手写的封面页打架。
       实测那样做封面根本不出现、标题还跑到了目录页顶上。
       ⇒ 全部走 raw LaTeX，pandoc 的模板不插手。

    ⚠️ **下划线必须转义**：万一标题里带 `_`（如回退到文件名 `银黄口服液_2026-05`），
       那个 `_` 在 LaTeX 里是数学下标，会报 `Missing $ inserted`。
    """
    def _tex(s: str) -> str:
        for ch, repl in (("\\", r"\textbackslash{}"), ("_", r"\_"),
                         ("&", r"\&"), ("%", r"\%"), ("#", r"\#")):
            s = s.replace(ch, repl)
        return s

    main, sub = _cover_title_lines(title)

    return r"""```{=latex}
\begin{titlepage}
\centering
\vspace*{4cm}
{\Huge\bfseries 《%(title)s》\par}
\vspace{1.2cm}
{\large %(sub)s\par}
\vspace{2.5cm}
{\normalsize
文件版本： V1.0\\[6pt]
编制日期： %(date)s\\[6pt]
编制单位： 中药一厂 财务部\par}
\vfill
\end{titlepage}
```

```{=latex}
\thispagestyle{empty}
\section*{文档控制}
\addcontentsline{toc}{section}{文档控制}
\textbf{版本记录}：V1.0　首次发布\\
\textbf{查阅}：财务部、生产部、采购部、质量部\\
\textbf{分发}：厂内管理评审；不外发

\section*{阅读指南}
\addcontentsline{toc}{section}{阅读指南}
```

为便于不同角色快速抓住重点，建议按以下路径阅读：

- **管理层**：先看「二、总成本概览」与「六、总结与建议」
- **财务**：重点看「三、成本要素明细分析」与「五、对标分析」
- **生产**：重点看「三、成本要素明细分析」与「四、重点产品专项分析」
- **采购**：重点看「三、直接材料分析」与「四、关键原材料市场价格跟踪」

```{=latex}
\newpage
\renewcommand{\contentsname}{目　录}
\tableofcontents
\newpage
```

""" % {"title": _tex(main), "sub": _tex(sub), "date": date_str}


def inject_chart_refs(md_text: str, charts: List[Dict[str, Any]]) -> str:
    """
    把图表以 `![]()` 形式插到锚点标题**之后**，供 **PDF 路径**用。

    ⚠️ 为什么 PDF 要单独处理：PDF 走 pandoc 读 markdown，
       它只认 markdown 的图片语法，拿不到 python-docx 那种"直接 add_picture"。
       所以走 PDF 时得先把引用写进 markdown **副本**。
       同一份 `charts` 配置，两条路各取所需——
       **源文件始终不动**（它还要供看板、GitHub、检索用）。
    """
    if not charts:
        return md_text
    lines = md_text.split("\n")
    out: List[str] = []
    for ln in lines:
        out.append(ln)
        # 这一行是锚点标题吗
        for ch in charts:
            if ln.strip() == ch.get("after", "").strip():
                # ⚠️ 用 raw LaTeX，**不用 markdown 的 `![]()`**。
                #    原因：pandoc 把"独占一行的图片 + alt 文字"识别成 figure，
                #    自动套 \caption{} → PDF 里出现「Figure 1: 图 1 …」双题注，
                #    而 `-V figure-caption=false` **不是有效变量**（试过，没用）。
                #    直接给 LaTeX 就等于绕过它那套 figure 机制。
                #
                # ⚠️ 用**绝对路径**：相对路径在最小用例里能渲染，但在完整报告上
                #    pandoc 解析不下去，图被**静默丢弃**（只剩图注文字）。
                img = Path(ch["image"]).resolve().as_posix()
                cap = ch.get("caption") or ""
                out.append("")
                out.append("```{=latex}")
                out.append(r"\begin{center}")
                out.append(r"\includegraphics[width=0.94\textwidth]{%s}" % img)
                if cap:
                    out.append(r"\\[4pt]{\small\color{gray} %s}" % cap)
                out.append(r"\end{center}")
                out.append("```")
                out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 报告图表：从 report_facts 重绘，锚到报告的小节标题
#
# ⚠️ **不改源 markdown**：锚点写在导出配置里，源文件保持干净
#    （它同时供看板、GitHub、检索使用，塞进图片引用会污染知识层）。
# ---------------------------------------------------------------------------

def build_report_charts(product: str, month: str,
                        out_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    按产品/月份生成三张图，返回 [{after, image, caption}]。

    三张图与看板一一对应：
        2.2 成本结构      → 环形（本月构成）
        4.1 近6个月趋势   → 折线（逐月单位成本）
        5.1 差异总览      → 柱状（一厂 vs 二厂）
    """
    import chart_render as CR
    import report_facts as RF
    import benchmark_engine as BE

    img_dir = (out_dir or EXPORT_DIR) / "_charts"
    img_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w一-鿿-]", "_", f"{product}_{month}")
    fs = RF.build(product, month)
    charts: List[Dict[str, Any]] = []

    # ① 成本结构（2.2 节后）
    try:
        items = [
            ("直接材料", fs.facts["本月材料成本"].value),
            ("直接人工", fs.facts["本月人工成本"].value),
            ("制造费用", fs.facts["本月制造成本"].value),
        ]
        total = fs.facts["本月单位成本"].value
        p = CR.render_structure(items, total, product, "元/盒",
                                img_dir / f"{safe}_结构.png")
        charts.append({"after": "### 2.2 成本结构", "image": p,
                       "caption": f"图 1　{product} {month} 单位成本构成（元/盒）"})
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ 成本结构图跳过：{type(e).__name__}: {e}")

    # ② 逐月趋势（4.1 节后）
    try:
        p = CR.render_trend(fs.monthly, product, img_dir / f"{safe}_趋势.png")
        charts.append({"after": "### 4.1 近6个月单位成本趋势", "image": p,
                       "caption": f"图 2　{product} 逐月单位成本走势"})
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ 趋势图跳过：{type(e).__name__}: {e}")

    # ③ 厂际对比（5.1 节后）—— 走对标引擎，与看板同源
    try:
        r = BE.run(product, month, use_llm=False, verbose=False)
        rows = r["steps"][0]["rows"]
        p = CR.render_peer(rows, product, img_dir / f"{safe}_对标.png")
        charts.append({"after": "### 5.1 差异总览", "image": p,
                       "caption": f"图 3　{product} 一厂 vs 二厂 成本要素对比（元/盒）"})
    except Exception as e:  # noqa: BLE001
        print(f"    ⚠️ 对标图跳过：{type(e).__name__}: {e}")

    return charts


def _disp_w(s: str) -> int:
    """
    文本的**显示宽度**：CJK 按 2 列算，ASCII 按 1 列。

    ⚠️ 不能用 `len()`——它把「银黄口服液」和「TASK-YH」都算 5，
       可前者实际占 10 列宽。按 `len()` 分配列宽会让中文列**始终偏窄**，
       表格折行反而更厉害。
    """
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)


# 每 1 显示列 ≈ 0.16cm（9pt）；单元格左右内边距合计 ≈ 0.38cm
_CH_CM = 0.16
_PAD_CM = 0.38
_MIN_COL_CM = 1.4


def _col_widths(rows: List[List[str]], ncol: int,
                avail_cm: float = 14.6) -> List[float]:
    """
    给各列分宽度（合计 = `avail_cm` = 14.6cm，即 A4 减左右边距各 3.2cm）。

    先把每列的**所需宽度**算出来（该列最宽单元格的显示宽度 × 0.16 + 内边距），
    然后：

    - 总量放得下 → 每列给够，**余量各列均分**（每列已拿到内容所需宽度，
      余量是净赚的；均分可预期，也不会造出 10cm 与 1cm 并排的怪表）
    - 放不下 → 反复把**最宽的一列压到与次宽齐平**，直到总和放得下

    为什么不按比例缩放：等比例会**把所有列一起压窄**，于是
    「截止时间」这种 10 字符的定长值被折成 `2026-07-` + `31`——
    日期断行在业务文档里是可读性事故（也容易被当成两个值）。
    压最宽的那列则是把代价放在**本来就该折行的长句**上。

    ⚠️ 前一版用「各列平均显示宽度」按比例归一，两个坑：
       ① 均值低估定长列（「截止时间」每次都是 10 字符，均值却不到 10）；
       ② 归一后「优先级」只剩 0.77cm，**表头自己就折行**。
    """
    need: List[float] = []
    for ci in range(ncol):
        vals = [_disp_w(r[ci].strip()) for r in rows if ci < len(r)]
        need.append(max(vals) * _CH_CM + _PAD_CM if vals else _MIN_COL_CM)

    total = sum(need)
    if total <= avail_cm:
        extra = (avail_cm - total) / ncol            # 余量均分
        return [x + extra for x in need]

    w = list(need)
    for _ in range(400):
        if sum(w) <= avail_cm:
            break
        i = w.index(max(w))
        rest = [x for j, x in enumerate(w) if j != i]
        w[i] = max(max(rest) if rest else _MIN_COL_CM,
                   max(w[i] - (sum(w) - avail_cm), _MIN_COL_CM))
    if sum(w) > avail_cm:                            # 兜底：全都压到下限还不够
        s = avail_cm / sum(w)
        w = [max(x * s, _MIN_COL_CM) for x in w]
    return w


def rebalance_pipe_tables(md_text: str, total: int = 100) -> str:
    """
    重排 markdown **管道表格**分隔行的短横数量，让 pandoc 按内容分列宽。

    ⚠️ pandoc 对 pipe table 的列宽**来自分隔行 `|---|` 的短横条数**（相对比例）
       本库模板里各列都写 10 条 → 所有列等宽 → 「任务标题」这种长文本列
       被压成 2cm，一行折成 8 行。Word 侧靠 `flush_table` 显式设列宽，
       PDF 侧只能改分隔行——**两边的宽度分配必须用同一套 `_disp_w` 逻辑**，
       否则同一个表格在 docx 和 pdf 里长得不一样。

    只动分隔行，**不动表头和数据行**（模板里「不得改动表头文字」是硬约束）。
    """
    lines = md_text.split("\n")
    out: List[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # 定位表格：当前行是 `|...|`，下一行是分隔行
        if (line.strip().startswith("|") and i + 1 < len(lines)
                and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1])):
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            ncol = len(head)
            # 收集表体行
            j = i + 2
            body: List[List[str]] = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                body.append([c.strip() for c in
                             lines[j].strip().strip("|").split("|")])
                j += 1
            rows = [head] + body
            widths = _col_widths(rows, ncol)
            # 短横条数按列宽比例分配（至少 3 条，否则 pandoc 不认作分隔行）
            dashes = [max(3, int(round(total * w / sum(widths)))) for w in widths]
            # 对齐标记：原分隔行的 `:` 保留（左/中/右对齐是模板意图）
            orig = lines[i + 1].strip().strip("|").split("|")
            seps = []
            for ci in range(ncol):
                o = (orig[ci] if ci < len(orig) else "").strip()
                body_dashes = "-" * dashes[ci]
                if o.startswith(":") and o.endswith(":"):
                    body_dashes = ":" + body_dashes + ":"      # 居中
                elif o.startswith(":"):
                    body_dashes = ":" + body_dashes            # 左对齐
                elif o.endswith(":"):
                    body_dashes = body_dashes + ":"            # 右对齐
                seps.append(body_dashes)
            out.append(line)
            out.append("|" + "|".join(seps) + "|")
            out.extend(lines[i + 2:j])
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _cover_title_lines(title: str) -> Tuple[str, str]:
    """
    把 H1 拆成**封面主标题 + 副标题**。

    报告的 H1 形如：
        中药一厂 2026-05 月度产品成本多维深度分析报告 — 银黄口服液
    封面拆两行更好看：主标题取「—」前的部分，副标题取产品名。
    没有「—」时副标题为空（此时**不能**拿一句固定文案兜底，见下）。

    ⚠️ 分隔符**必须限定为破折号且两边带空格**。第一版写成 `[—–-]`（含 ASCII
       连字符），结果在 `2026-05` 那个 `-` 上就切了——封面印成
       `《中药一厂 2026》` + `05 月度产品…`。日期里的连字符不是分隔符。
    """
    parts = re.split(r"\s+[—–]\s+", title, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return title.strip(), ""


def title_from_md(md_path: Path) -> str:
    """
    取 markdown 的 **H1 作为文档标题**，而不是文件名。

    ⚠️ 曾经用 `md_path.stem` 兜底，于是封面印成 `《银黄口服液_2026-05》`——
       文件名里的 `_` 在 LaTeX 里是数学下标，`_tex()` 转义后**显示为下划线**，
       结果封面挂着一串 `__`（PDF 实测：`《银黄口服液 __2026-05》`）。
       根因不是转义没做，而是**标题本来就不该来自文件名**：
       报告 md 的第一行 H1 才是这个文档真正的标题。
    """
    try:
        for line in md_path.read_text(encoding="utf-8").split("\n"):
            s = line.strip()
            if s.startswith("# "):
                return s[2:].strip()
            if s:
                break          # 只有文件开头连续的 `# ` 才算标题
    except OSError:
        pass
    return md_path.stem


# ---------------------------------------------------------------------------

def export(md_path: Path, fmt: str, out_dir: Optional[Path] = None,
           title: str = "", with_charts: bool = False,
           front_matter: bool = False) -> List[Path]:
    """
    把一份 markdown 导出成指定格式。fmt ∈ {docx, pdf, both}。

    ⚠️ 导出前会把 mermaid 块**换成 ASCII 架构图**（见 `replace_mermaid` 的说明）。
    转换在**内存里的临时副本**上做——**不改源 markdown**，
    因为源文件里的 mermaid 在 GitHub/看板上能正常渲染。

    `with_charts=True` 时按文件名 `<产品>_<月份>.md` 推断产品与月份，
    重绘三张图并锚到对应小节（赛题 5.1.3 的「图表嵌入」）。
    """
    out_dir = out_dir or EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = md_path.stem
    title = title or title_from_md(md_path)

    charts: List[Dict[str, Any]] = []
    if with_charts:
        m = re.match(r"^(?P<prod>.+?)_(?P<ym>\d{4}-\d{2})$", stem)
        if m:
            charts = build_report_charts(m.group("prod"), m.group("ym"), out_dir)
            print(f"   📊 已生成 {len(charts)} 张图")

    src_text = md_path.read_text(encoding="utf-8")

    # ---- docx 用：原 markdown（mermaid 换 ASCII），图靠 add_picture 直接插 ----
    docx_src: Path = md_path
    need_tmp = "```mermaid" in src_text

    # ---- pdf 用：markdown 里必须有 `![]()`，所以还要把图**写进副本** ----
    pdf_src: Path = md_path
    need_pdf_tmp = need_tmp or bool(charts) or front_matter

    tmp_docx: Optional[Path] = None
    tmp_pdf: Optional[Path] = None
    if need_tmp or need_pdf_tmp:
        text = replace_mermaid(src_text) if need_tmp else src_text
        if need_pdf_tmp and front_matter:
            text = front_matter_md(title, datetime.now().strftime("%Y-%m-%d")) + text
        if need_pdf_tmp:
            # 图片相对路径要能解析——副本就放在 out_dir 下，
            # 而图也在 out_dir/_charts/，相对关系成立。
            text = inject_chart_refs(text, charts)
            # 表格列宽：pandoc 按分隔行的**短横数量**分配列宽，
            # 模板里 `|---|` 各列等长 → 长文本列被压扁折行。
            text = rebalance_pipe_tables(text)
        # ⚠️ 两份用途不同（docx 不要 `![]()`，否则会多出一条图片链接文本），
        #    所以各写各的临时文件，别图省事共用一份。
        if need_tmp:
            tmp_docx = out_dir / f"_{stem}_docx.md"
            tmp_docx.write_text(replace_mermaid(src_text),
                                encoding="utf-8", newline="\n")
            docx_src = tmp_docx
        if need_pdf_tmp:
            tmp_pdf = out_dir / f"_{stem}_pdf.md"
            tmp_pdf.write_text(text, encoding="utf-8", newline="\n")
            pdf_src = tmp_pdf

    outs: List[Path] = []
    try:
        if fmt in ("docx", "both"):
            outs.append(to_docx(docx_src, out_dir / f"{stem}.docx", title=title,
                                charts=charts, front_matter=front_matter))
        if fmt in ("pdf", "both"):
            outs.append(to_pdf(pdf_src, out_dir / f"{stem}.pdf", title=title,
                                 toc=front_matter))
    finally:
        for t in (tmp_docx, tmp_pdf):
            if t is not None:
                t.unlink(missing_ok=True)
    return outs


def main() -> int:
    ap = argparse.ArgumentParser(description="文档导出（Markdown → Word / PDF）")
    ap.add_argument("--md", required=True, help="源 markdown 路径")
    ap.add_argument("--to", default="both", choices=["docx", "pdf", "both"])
    ap.add_argument("--out-dir", help=f"输出目录（默认 {EXPORT_DIR}）")
    ap.add_argument("--title", default="", help="页眉标题")
    ap.add_argument("--charts", action="store_true",
                    help="重绘三张图并嵌进文档（赛题 5.1.3「图表嵌入」）")
    ap.add_argument("--front-matter", action="store_true",
                    help="加文档外壳：封面/文档控制/阅读指南/目录（正式报告用）")
    a = ap.parse_args()

    md = Path(a.md)
    if not md.exists():
        print(f"源文件不存在：{md}")
        return 1
    out_dir = Path(a.out_dir) if a.out_dir else None
    try:
        outs = export(md, a.to, out_dir, title=a.title,   # 空则由 export() 取 md 的 H1
                      with_charts=a.charts, front_matter=a.front_matter)
    except Exception as e:  # noqa: BLE001
        print(f"导出失败：{e}")
        return 2
    for p in outs:
        print(f"✅ {p}  （{p.stat().st_size:,} B）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

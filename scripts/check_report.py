# -*- coding: utf-8 -*-
"""
check_report.py — **报告合规性机械校验**

按 `wiki/interfaces/报告模板契约.md` §8「使用约束」逐条检查一份生成的报告。

为什么是脚本：这五条约束**完全机械**——
  1. 占位符语法 `{{中文名}}`
  2. `{{xxx表格}}` 生成整块 markdown 表格
  3. **不得删减章节**（6 章是验收清单）
  4. **不得改动表头文字**
  5. `.docx`/`.md` 内容一致（不适用程序化校验，跳过）

外加两项本库自定检查：
  6. 不得残留 `{{占位符}}`
  7. RPA 字段约束：`task_id` 唯一、`priority` 取值能转成 high/medium/low
     （报告印中文高/中/低，提交时才转小写英文）

用法
----
    python scripts/check_report.py reports/银黄口服液_2026-05.md
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402

ensure_utf8_stdout()

HERE = Path(__file__).resolve().parent
WIKI_ROOT = HERE.parent

REQUIRED_CHAPTERS = ["## 一、封面与基本信息", "## 二、总成本概览", "## 三、成本要素明细分析",
                     "## 四、重点产品专项分析", "## 五、对标分析", "## 六、总结与建议"]

# 专题是**另一套六章结构**（围绕单一成本要素展开），不是月度的改措辞
REQUIRED_CHAPTERS_TOPIC = ["## 一、专题背景与结论", "## 二、现状数据", "## 三、驱动因素拆解",
                           "## 四、对标参照", "## 五、风险评估", "## 六、结论与整改建议"]

# 契约 §8 第 4 条：不得改动表头文字
#
# ⚠️ **表头随分析主题而变**（月度说"本月"、季度说"本季"、专题说"本期"）。
#    原先这里只有月度一套，于是**季度报告会被误报"表头被改动 2 处"**——
#    报告本身没错，是校验器认错了模板。现在按主题分开，
#    `detect_theme()` 从正文自动识别，调用方不必自己声明。
REQUIRED_HEADERS = [
    "| 指标 | 本月实际 | 上月实际 | 环比变动 | 去年同月 | 同比变动 | 预算值 | 预算偏差 |",
    "| 成本要素 | 金额(元/盒) | 占比 | 环比变动 | 贡献度 |",
    "| 月份 | 产量(盒) | 单位材料(元/盒) | 单位人工(元/盒) | 单位制造费用(元/盒) | 单位成本(元/盒) | 环比变动 |",
    "| 对比维度 | 中药一厂 | 中药二厂 | 差异金额 | 差异率 | 方向 |",
    "| 任务编号 | 任务标题 | 责任人 | 优先级 | 来源 | 截止时间 |",
    "| 序号 | 建议事项 | 责任部门 | 优先级 | 预期效果 | 建议完成时间 |",
]

REQUIRED_HEADERS_QUARTERLY = [
    "| 指标 | 本季实际 | 上季实际 | 环比变动 | 去年同期 | 同比变动 | 预算值 | 预算偏差 |",
    "| 成本要素 | 金额(元/盒) | 占比 | 环比变动 | 贡献度 |",
    "| 月份 | 产量(盒) | 单位材料(元/盒) | 单位人工(元/盒) | 单位制造费用(元/盒) | 单位成本(元/盒) | 环比变动 |",
    "| 序号 | 原材料名称 | 本季单价(元/盒) | 上季单价(元/盒) | 环比变动 | 变动原因初步判断 |",
    "| 指标 | 本季 | 上季 | 环比 | 说明 |",
    "| 任务编号 | 任务标题 | 责任人 | 优先级 | 来源 | 截止时间 |",
    "| 序号 | 建议事项 | 责任部门 | 优先级 | 预期效果 | 建议完成时间 |",
]

REQUIRED_HEADERS_TOPIC = [
    "| 指标 | 本期 | 对比期 | 变动 | 说明 |",
    "| 月份 | {} | 环比变动 | 占总成本比 |",   # {} 为专题名称，占位比对
    "| 对比维度 | 中药一厂 | 中药二厂 | 差异 | 差异率 |",
    "| 任务编号 | 任务标题 | 责任人 | 优先级 | 来源 | 截止时间 |",
    "| 序号 | 建议事项 | 责任部门 | 优先级 | 预期效果 | 建议完成时间 |",
]


def detect_theme(text: str) -> str:
    """
    从正文识别分析主题。**按各主题独有的表头/措辞判定**，不猜文件名。

    判定顺序有意从最特异到最一般：季度有"本季实际"、专题有"| 本期 | 对比期 |"，
    两者都不匹配则视为月度。
    """
    if "本季实际" in text:
        return "quarterly"
    if "| 本期 | 对比期 |" in text:
        return "topic"
    return "monthly"


def headers_for(theme: str) -> list[str]:
    return {"monthly": REQUIRED_HEADERS,
            "quarterly": REQUIRED_HEADERS_QUARTERLY,
            "topic": REQUIRED_HEADERS_TOPIC}.get(theme, REQUIRED_HEADERS)


def chapters_for(theme: str) -> list[str]:
    """专题是另一套六章结构；月/季度同构（季度只是换了期间措辞）。"""
    return REQUIRED_CHAPTERS_TOPIC if theme == "topic" else REQUIRED_CHAPTERS


def _header_present(h: str, text: str) -> bool:
    """专题表头带 `{}` 占位（专题名称），按前两列匹配。"""
    if "{}" in h:
        prefix = h.split("{}")[0]
        return any(line.startswith(prefix) for line in text.split("\n"))
    return h in text


# 契约 §8 第 5 条 / SKILL 硬约束：不得出现的错误编号
FORBIDDEN = [
    (r"\bTQ-01\b", "使用了不存在的设备编号 TQ-01（真实格式 EQ-<类别>-<三位流水>）"),
    (r"%%", "出现双重百分号"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description="报告合规性机械校验")
    ap.add_argument("path")
    a = ap.parse_args()

    p = Path(a.path)
    if not p.exists():
        print(f"文件不存在: {p}")
        return 1
    t = p.read_text(encoding="utf-8")
    bad: list[str] = []
    ok: list[str] = []

    # 0 识别主题（表头随主题而变，识别错会把合规报告误报成表头被改）
    theme = detect_theme(t)
    THEME_ZH = {"monthly": "月度", "quarterly": "季度", "topic": "专题"}
    ok.append(f"[主题] 按「{THEME_ZH[theme]}」模板校验表头")

    # 1 章齐全（月/季度同构；专题另有一套六章）
    req_ch = chapters_for(theme)
    miss = [c for c in req_ch if c not in t]
    (bad if miss else ok).append(
        f"[§8.3 不得删减章节] {'缺失: ' + ', '.join(miss) if miss else f'{len(req_ch)} 章齐全'}")

    # 2 表头未改动（用**该主题**的表头清单）
    badh = [h for h in headers_for(theme) if not _header_present(h, t)]
    (bad if badh else ok).append(
        f"[§8.4 不得改动表头] "
        f"{'被改动 ' + str(len(badh)) + ' 处: ' + ' / '.join(x[:40] for x in badh) if badh else '全部保留'}")

    # 3 无残留占位符
    ph = re.findall(r"\{\{[^}]+\}\}", t)
    (bad if ph else ok).append(
        f"[§8.1 占位符已全填] {'残留 ' + str(len(set(ph))) + ' 个: ' + ', '.join(sorted(set(ph))[:5]) if ph else '无残留'}")

    # 4 禁止项
    for pat, msg in FORBIDDEN:
        if re.search(pat, t):
            bad.append(f"[禁止项] {msg}")
    if not any(re.search(p, t) for p, _ in FORBIDDEN):
        ok.append("[禁止项] 未发现 TQ-01 / 双重百分号")

    # 5 RPA 字段约束（第六章整改任务清单）
    rows = [l for l in t.split("\n") if re.match(r"^\|\s*TASK-", l)]
    if rows:
        ids = [l.split("|")[1].strip() for l in rows]
        prios = [l.split("|")[4].strip() for l in rows]
        if len(set(ids)) != len(ids):
            bad.append("[RPA] task_id 不唯一")
        # ⚠️ 报告里印**中文**高/中/低（给人看），提交时才转小写英文
        #    （`rpa_client.to_rpa_priority`）。所以这里校验的是**可转换性**，
        #    而不是"必须已经是英文"——否则会误报。真正的英文约束在提交侧，
        #    由 `rpa_client` 的 to_rpa_priority 兜底，非法值落 medium。
        legal = ("高", "中", "低", "high", "medium", "low")
        illegal = [x for x in prios if x.lower() not in legal]
        if illegal:
            bad.append(f"[RPA] priority 应为 高/中/低（提交时转 high/medium/low），"
                       f"出现无法转换的值: {illegal}")
        if not illegal and len(set(ids)) == len(ids):
            ok.append(f"[RPA] {len(rows)} 条任务：task_id 唯一、priority 取值合法")
    else:
        bad.append("[RPA] 未找到整改任务行（第六章应有 {{整改任务表格}}）")

    # 6 三要素行的同比两列应为 —
    # ⚠️ 只有月/季度模板有这张「成本结构」表（专题不设，故不计入）。
    if theme == "topic":
        ok.append("[模板口径] 专题模板无三要素行，跳过该项")
    else:
        seg = t.split("| 其中：直接材料")[1].split("\n")[0] if "| 其中：直接材料" in t else ""
        if "| — | — |" in seg:
            ok.append("[模板口径] 三要素行「去年同月/同比」保留固定 —")
        else:
            bad.append("[模板口径] 三要素行应有固定的 `— | —` 两列")

    print("=" * 72)
    print(f"报告合规性检查  {p.name}")
    print("=" * 72)
    for s in ok:
        print(f"  ✅ {s}")
    for s in bad:
        print(f"  ❌ {s}")
    print()
    print("✅ 全部通过" if not bad else f"⚠️  {len(bad)} 项不合规")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

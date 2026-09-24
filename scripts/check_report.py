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

# 契约 §8 第 4 条：不得改动表头文字
REQUIRED_HEADERS = [
    "| 指标 | 本月实际 | 上月实际 | 环比变动 | 去年同月 | 同比变动 | 预算值 | 预算偏差 |",
    "| 成本要素 | 金额(元/盒) | 占比 | 环比变动 | 贡献度 |",
    "| 月份 | 产量(盒) | 单位材料(元/盒) | 单位人工(元/盒) | 单位制造费用(元/盒) | 单位成本(元/盒) | 环比变动 |",
    "| 对比维度 | 中药一厂 | 中药二厂 | 差异金额 | 差异率 | 方向 |",
    "| 任务编号 | 任务标题 | 责任人 | 优先级 | 来源 | 截止时间 |",
    "| 序号 | 建议事项 | 责任部门 | 优先级 | 预期效果 | 建议完成时间 |",
]

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

    # 1 章齐全
    miss = [c for c in REQUIRED_CHAPTERS if c not in t]
    (bad if miss else ok).append(
        f"[§8.3 不得删减章节] {'缺失: ' + ', '.join(miss) if miss else '6 章齐全'}")

    # 2 表头未改动
    badh = [h for h in REQUIRED_HEADERS if h not in t]
    (bad if badh else ok).append(
        f"[§8.4 不得改动表头] {'被改动 ' + str(len(badh)) + ' 处' if badh else '全部保留'}")

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

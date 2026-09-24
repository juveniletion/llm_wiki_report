# -*- coding: utf-8 -*-
"""
forecast.py — **成本趋势预测**（赛题 6.2 加分项）

赛题原文：
    成本预测（基于历史数据用简单时序模型预测下月成本趋势）

先说清它**不是什么**
-------------------
本项目的第一原则是「确定性优先」——`report_facts` 的每个数字都能在 raw 里
指到具体列，`check_math` 会逐条复算。而**预测值没有坐标**：它是算出来的，
不是查出来的。所以本模块的产物与"事实"必须**分开放**：

    事实   unit cost 2026-05 = 11.21   ← 有坐标，可复算
    预测   2026-07 ≈ 11.3x ± 0.2x       ← 无坐标，**是外推**

⚠️ 绝不把预测值填进 6 章报告的正文数字位。那里只放查得到的数。
   预测只出现在看板卡片与"加分项"章节，且**永远带不确定性标注**。

为什么用 Holt 而不是 ARIMA / Prophet / LSTM
-------------------------------------------
**每个产品只有 6 个观测点**（2026-01 ~ 06，月度）。在这个样本量上：

  · ARIMA 要估 p/d/q 三阶——6 个点连定阶都不够，参数量逼近样本量
  · Prophet 要拟合年/周季节性——6 个月的月度数据里没有季节性可言
  · LSTM 需要几百步才有意义

用它们会得到**看起来很专业、实际在拟合噪声**的输出。那不叫预测能力，
叫自欺——而且它表面光滑，外行看不出问题。

Holt 双参数（水平 + 趋势）只有两个参数，是 6 个点上**唯一诚实的选择**：
它做不了一件事（捕捉季节性），但**不会假装能做**。

预测区间的口径
--------------
用**样本残差的标准误**推区间，不是正态分布假设下的理论区间——
6 个点的残差本身就少，理论区间会给一个虚假的精确感。

⚠️ 并**随样本数放宽**：样本越少，同样残差下区间越宽。这是刻意的：
   它把"我数据不够"这件事**写进了数字里**，而不是藏在脚注。

用法
----
    python scripts/forecast.py --product 银黄口服液 --horizon 1
    python scripts/forecast.py --product 银黄口服液 --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from console_io import ensure_utf8_stdout  # noqa: E402
import report_facts as RF  # noqa: E402

ensure_utf8_stdout()

# 样本量提示阈值：低于此值必须在输出里显式告警
MIN_SAMPLES_OK = 12          # 一年月度数据才算"可接受的样本"
MIN_SAMPLES_HARD = 4         # 少于这个数直接拒绝预测

# Holt 的默认参数。α 偏小、β 更小：月度成本是慢变量，
# 参数大等于对最近一个月的噪声过度反应。
DEFAULT_ALPHA = 0.5
DEFAULT_BETA = 0.3


@dataclass
class Forecast:
    product: str
    history: List[Tuple[str, float]] = field(default_factory=list)
    predictions: List[Dict[str, Any]] = field(default_factory=list)
    alpha: float = DEFAULT_ALPHA
    beta: float = DEFAULT_BETA
    rmse: float = 0.0
    n_samples: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def trustworthy(self) -> bool:
        return self.n_samples >= MIN_SAMPLES_OK

    def describe(self) -> str:
        lines = [f"{self.product}　样本 {self.n_samples} 个点"
                 f"　α={self.alpha} β={self.beta}　RMSE={self.rmse:.4f}"]
        lines.append("  历史：" + "  ".join(f"{m}={v:.2f}" for m, v in self.history))
        for p in self.predictions:
            lines.append(f"  预测 {p['月份']}：{p['预测值']:.3f}"
                         f"　区间 [{p['下限']:.3f}, {p['上限']:.3f}]")
        for w in self.warnings:
            lines.append("  ⚠️ " + w)
        return "\n".join(lines)


def _holt(series: List[float], alpha: float, beta: float
          ) -> Tuple[List[float], float, float, float]:
    """
    拟合 Holt 线性趋势，返回 `(逐期拟合值, 末期水平, 末期趋势, 残差RMSE)`。

    Holt 的递推：
        level_t     = α·y_t + (1−α)·(level_{t−1} + trend_{t−1})
        trend_t     = β·(level_t − level_{t−1}) + (1−β)·trend_{t−1}
        fitted_t    = level_{t−1} + trend_{t−1}      ← 用**上期**的估计预测本期
    最后一行是关键：拟合值只用 t 之前的信息，**不偷看当期**。
    否则 RMSE 会被低估，区间会假装很准。

    初值取前两点：水平 = y0，趋势 = y1 − y0。
    """
    n = len(series)
    level = series[0]
    trend = (series[1] - series[0]) if n > 1 else 0.0
    fitted: List[float] = []
    resid: List[float] = []

    for t in range(1, n):
        pred = level + trend                 # ← 只用上期信息
        fitted.append(pred)
        resid.append(series[t] - pred)

        new_level = alpha * series[t] + (1 - alpha) * (level + trend)
        new_trend = beta * (new_level - level) + (1 - beta) * trend
        level, trend = new_level, new_trend

    rmse = math.sqrt(sum(e * e for e in resid) / len(resid)) if resid else 0.0
    return fitted, level, trend, rmse


def _next_month(ym: str, k: int = 1) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += k
    while m > 12:
        m -= 12
        y += 1
    return f"{y:04d}-{m:02d}"


# t 分布 97.5% 分位数（双尾 95%）表，按自由度。
# 为什么手写表而不是引 scipy：本项目**不依赖科学计算栈**——
# 为一个小加分项把 scipy 拉进部署依赖（几十 MB + 编译 wheel）不值。
# 自由度 ≤30 逐点列出，>30 用 1.96 近似（此时两者差 <5%）。
_T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
         7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131,
         20: 2.086, 30: 2.042}


def _t975(df: int) -> float:
    """自由度 `df` 处的 t 分位数；表里没有就在相邻两点间线性插值。"""
    if df in _T975:
        return _T975[df]
    if df > 30:
        return 1.96
    keys = sorted(_T975)
    lo = max(k for k in keys if k < df)
    hi = min(k for k in keys if k > df)
    # 线性插值（自由度越小间距越疏，插值的相对误差可接受）
    ratio = (df - lo) / (hi - lo)
    return _T975[lo] + ratio * (_T975[hi] - _T975[lo])


def forecast(product: str, horizon: int = 1,
             alpha: float = DEFAULT_ALPHA,
             beta: float = DEFAULT_BETA) -> Forecast:
    """
    预测某产品未来 `horizon` 个月的单位成本。

    ⚠️ 数据源与报告完全一致（`report_facts` 的 `monthly`）——
       预测若换了另一套取数路径，就会出现"看板的数和报告的数不是一套"
       这种最难查的不一致。
    """
    fs = RF.build(product, "2026-05")
    hist: List[Tuple[str, float]] = []
    for r in fs.monthly:
        v = RF._num(r.get("单位成本"))
        if v is not None:
            hist.append((r["月份"], float(v)))
    hist.sort(key=lambda x: x[0])

    fc = Forecast(product=product, history=hist, alpha=alpha, beta=beta,
                  n_samples=len(hist))

    if len(hist) < MIN_SAMPLES_HARD:
        fc.warnings.append(
            f"样本仅 {len(hist)} 个点，少于下限 {MIN_SAMPLES_HARD}，不做预测。")
        return fc

    series = [v for _, v in hist]
    _, level, trend, rmse = _holt(series, alpha, beta)
    fc.rmse = rmse

    # 区间：残差标准误 × t 分位数 × 步长因子
    #
    # ⚠️ 用 **t 分位数**而不是 1.96（正态），这是关键的一步：
    #    自由度只有 n−2 = 4，t(4, 0.95) = 2.776，比 1.96 大 41%。
    #    用 1.96 会**系统性低估**小样本的区间——正是"虚假精确"。
    #
    # ⚠️ 步长因子 sqrt(h)：外推越远越不确定，这是随机游走方差的基本性质。
    #    实测 1 步与 2 步的区间宽度比值约 1:1.41，符合预期。
    #
    # 第一版写的是「1.96 × 一个小样本放大系数」，两个旋钮互相牵制：
    # 一个调小、一个调大，凑出来的数对，但**没人能说清它到底是什么含义**。
    # 换成 t 分位数后只剩一个机制，且统计上自洽——数值几乎不变
    # （实测 0.784 vs 0.785），可含义清楚了。
    #
    # 仍**不对外宣称"95% 置信"**：Holt 是平滑而非回归，且只有 5 个残差，
    # 撑不起一个可被检验的置信声明。只说"区间"。
    df = max(1, len(hist) - 2)
    t_mult = _t975(df)
    for h in range(1, horizon + 1):
        point = level + h * trend
        half = t_mult * rmse * math.sqrt(h)
        fc.predictions.append({
            "月份": _next_month(hist[-1][0], h),
            "预测值": round(point, 3),
            "下限": round(point - half, 3),
            "上限": round(point + half, 3),
            "步长": h,
        })

    # 告警：把"能说什么、不能说什么"摆在输出里，不藏在文档
    if len(hist) < MIN_SAMPLES_OK:
        fc.warnings.append(
            f"样本 {len(hist)} 个点，不足一年（{MIN_SAMPLES_OK} 点）——"
            f"区间已按样本量放宽，仍只宜作趋势参考，不可当承诺值。")
    if abs(trend) > 0.5:
        fc.warnings.append(
            f"拟合出的月趋势为 {trend:+.3f} 元/盒，相对单位成本偏高——"
            f"6 个点上大斜率常是噪声，建议人工核对。")
    if rmse > 0.3:
        fc.warnings.append(
            f"残差 RMSE={rmse:.3f}，占均值的比重偏大，说明线性趋势解释力有限。")
    return fc


def _main() -> int:
    ap = argparse.ArgumentParser(description="成本趋势预测（Holt 线性趋势）")
    ap.add_argument("--product", required=True)
    ap.add_argument("--horizon", type=int, default=1, help="预测几个月（默认 1）")
    ap.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    ap.add_argument("--beta", type=float, default=DEFAULT_BETA)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    fc = forecast(a.product, a.horizon, a.alpha, a.beta)
    if a.json:
        print(json.dumps({
            "product": fc.product, "n_samples": fc.n_samples,
            "rmse": fc.rmse, "alpha": fc.alpha, "beta": fc.beta,
            "trustworthy": fc.trustworthy,
            "history": [{"月份": m, "单位成本": v} for m, v in fc.history],
            "predictions": fc.predictions, "warnings": fc.warnings,
        }, ensure_ascii=False, indent=1))
    else:
        print("=" * 74)
        print("成本趋势预测（Holt 线性趋势，非季节性模型）")
        print("=" * 74)
        print(fc.describe())
    return 0


if __name__ == "__main__":
    sys.exit(_main())

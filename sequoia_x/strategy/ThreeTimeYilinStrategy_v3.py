"""三倍量缩量盘整突破策略 V3：候选池打分版。

适用目标：
- 每天稳定筛选 5-8 只股票
- 不再用“全部硬条件同时满足”的方式筛选
- 改为“基础过滤 + 综合评分 + 排序取前 8”

策略逻辑：
1. 最近 3-90 个交易日内出现过三倍量上涨
2. 三倍量之后进入相对缩量整理
3. 当前没有明显破位，趋势结构偏强
4. 股价接近或突破整理平台
5. 成交额达到最低流动性要求
6. 根据量能、趋势、突破、振幅、成交额等因素打分
7. 返回分数最高的 5-8 只股票

注意：
- 本策略是“观察池/候选池”策略，不代表直接买入。
- 如果最终结果少于 5 只，通常说明当天市场不适合该模型，或者本地行情未更新。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TripleVolumeBreakoutStrategy(BaseStrategy):
    """三倍量缩量盘整突破策略 V3：打分排序版。

    和旧版相比：
    - 成交额门槛从 5 亿降到 1 亿
    - 三倍量有效期从 30 天放宽到 90 天
    - 不再要求所有条件都完美满足
    - 不再限制突破日成交量必须小于三倍量日
    - 最终通过综合评分排序，最多返回 8 只股票
    """

    webhook_key: str = "triple_volume"

    # 至少需要 60 日均线，再留出冗余
    _MIN_BARS: int = 120

    # 目标每日输出数量
    _TARGET_MIN: int = 5
    _TARGET_MAX: int = 8

    # 三倍量信号有效期
    _MIN_DAYS_AFTER_SIGNAL: int = 3
    _MAX_DAYS_AFTER_SIGNAL: int = 90

    # 成交额门槛：先用 1 亿，方便形成候选池
    _MIN_TURNOVER: float = 100_000_000

    # 盘整区间最大振幅：30%
    _MAX_CONSOLIDATION_RANGE: float = 1.30

    # 正常入选分数线
    _MIN_SCORE: float = 55.0

    # 候选不足时的兜底分数线
    _FALLBACK_MIN_SCORE: float = 45.0

    def _is_risky_stock(self, symbol: str, last: pd.Series) -> bool:
        """排除停牌、无成交、异常价格股票。"""
        if last["open"] <= 0 or last["close"] <= 0 or last["volume"] <= 0:
            return True

        # 如果你的 engine 未来支持 get_stock_name，则可以自动排除 ST / 退市
        get_stock_name = getattr(self.engine, "get_stock_name", None)
        if callable(get_stock_name):
            try:
                name = str(get_stock_name(symbol)).upper()
                if "ST" in name or "*" in name or "退" in name:
                    return True
            except Exception:
                pass

        return False

    @staticmethod
    def _safe_float(value: object, default: float = 0.0) -> float:
        """安全转为 float，避免 None / NaN 影响评分。"""
        try:
            result = float(value)
            if np.isnan(result) or np.isinf(result):
                return default
            return result
        except Exception:
            return default

    def _score_stock(
        self,
        last: pd.Series,
        prev: pd.Series,
        signal_bar: pd.Series,
        consolidation: pd.DataFrame,
        days_after_signal: int,
    ) -> tuple[float, dict[str, float]]:
        """给候选股打分，返回 score 和明细。"""

        score = 0.0
        detail: dict[str, float] = {}

        platform_high = self._safe_float(consolidation["high"].max())
        platform_low = self._safe_float(consolidation["low"].min())
        platform_avg = self._safe_float(consolidation["close"].mean())

        if platform_low <= 0:
            return 0.0, {}

        consolidation_range = platform_high / platform_low
        detail["platform_high"] = platform_high
        detail["platform_low"] = platform_low
        detail["platform_avg"] = platform_avg
        detail["consolidation_range"] = consolidation_range

        # 1. 三倍量信号质量：最高 15 分
        prev_volume = max(self._safe_float(signal_bar.get("prev_volume", 0.0)), 1.0)
        signal_volume = self._safe_float(signal_bar["volume"])
        volume_multiple = signal_volume / prev_volume

        volume_score = min(volume_multiple / 3.0, 2.0) * 7.5
        score += volume_score
        detail["volume_multiple"] = volume_multiple
        detail["volume_score"] = volume_score

        # 2. 信号时间：最高 10 分
        # 5-30 天最好，31-60 天次之，61-90 天降低权重
        if 5 <= days_after_signal <= 30:
            time_score = 10.0
        elif 31 <= days_after_signal <= 60:
            time_score = 7.0
        else:
            time_score = 4.0

        score += time_score
        detail["time_score"] = time_score
        detail["days_after_signal"] = float(days_after_signal)

        # 3. 缩量整理：最高 20 分
        if len(consolidation) >= 4:
            mid = len(consolidation) // 2
            early_vol = self._safe_float(consolidation["volume"].iloc[:mid].mean())
            late_vol = self._safe_float(consolidation["volume"].iloc[-mid:].mean())

            if early_vol > 0:
                shrink_ratio = late_vol / early_vol
            else:
                shrink_ratio = 1.0

            if shrink_ratio < 0.60:
                shrink_score = 20.0
            elif shrink_ratio < 0.80:
                shrink_score = 15.0
            elif shrink_ratio < 1.00:
                shrink_score = 10.0
            elif shrink_ratio < 1.20:
                shrink_score = 5.0
            else:
                shrink_score = 0.0
        else:
            shrink_ratio = 1.0
            shrink_score = 0.0

        score += shrink_score
        detail["shrink_ratio"] = shrink_ratio
        detail["shrink_score"] = shrink_score

        # 4. 盘整振幅：最高 15 分
        if consolidation_range <= 1.15:
            range_score = 15.0
        elif consolidation_range <= 1.20:
            range_score = 12.0
        elif consolidation_range <= 1.25:
            range_score = 8.0
        elif consolidation_range <= self._MAX_CONSOLIDATION_RANGE:
            range_score = 4.0
        else:
            range_score = 0.0

        score += range_score
        detail["range_score"] = range_score

        # 5. 趋势结构：最高 20 分
        trend_score = 0.0

        if last["close"] > last["ma20"]:
            trend_score += 6.0

        if last["ma20"] >= prev["ma20"]:
            trend_score += 5.0

        if last["ma20"] > last["ma60"]:
            trend_score += 5.0

        if last["ma5"] > last["ma10"]:
            trend_score += 4.0

        score += trend_score
        detail["trend_score"] = trend_score

        # 6. 突破强度：最高 20 分
        breakout_score = 0.0

        # 完全突破平台高点，分数最高
        if last["close"] > platform_high:
            breakout_score += 12.0
        # 还没有突破高点，但站上平台均价，也给观察分
        elif last["close"] > platform_avg:
            breakout_score += 6.0

        if last["close"] > signal_bar["close"]:
            breakout_score += 4.0

        if last["close"] > last["open"]:
            breakout_score += 2.0

        if last["close"] > prev["close"]:
            breakout_score += 2.0

        score += breakout_score
        detail["breakout_score"] = breakout_score

        # 7. 成交额加分：最高 10 分
        turnover = self._safe_float(last["turnover"])

        if turnover >= 1_000_000_000:
            turnover_score = 10.0
        elif turnover >= 500_000_000:
            turnover_score = 8.0
        elif turnover >= 300_000_000:
            turnover_score = 6.0
        elif turnover >= 100_000_000:
            turnover_score = 4.0
        else:
            turnover_score = 0.0

        score += turnover_score
        detail["turnover_score"] = turnover_score
        detail["turnover"] = turnover

        # 8. 风险扣分：近 20 天涨幅过大则扣分，降低高位加速末端风险
        recent_20_low = self._safe_float(last.get("low_20", 0.0))
        if recent_20_low > 0:
            recent_gain = self._safe_float(last["close"]) / recent_20_low - 1.0
        else:
            recent_gain = 0.0

        if recent_gain > 0.50:
            score -= 10.0
        elif recent_gain > 0.35:
            score -= 5.0

        detail["recent_gain"] = recent_gain
        detail["score"] = score

        return score, detail

    def run(self) -> list[str]:
        """遍历全市场，返回每日评分最高的 5-8 只股票。"""

        all_data = self.engine.load_all_ohlcv()
        if not all_data:
            return []

        all_candidates: list[tuple[str, float, dict[str, float]]] = []
        quality_candidates: list[tuple[str, float, dict[str, float]]] = []

        stats = {
            "total": 0,
            "enough_bars": 0,
            "basic_filter": 0,
            "has_signal": 0,
            "valid_signal": 0,
            "candidate": 0,
            "fallback_score_pass": 0,
            "quality_score_pass": 0,
            "final": 0,
        }

        for symbol, df in all_data.items():
            stats["total"] += 1

            try:
                if df is None or len(df) < self._MIN_BARS:
                    continue

                stats["enough_bars"] += 1

                required_cols = {"date", "open", "high", "low", "close", "volume", "turnover"}
                if not required_cols.issubset(df.columns):
                    missing = required_cols - set(df.columns)
                    logger.warning(f"[{symbol}] 缺少必要行情字段：{missing}")
                    continue

                df = df.sort_values("date")

                for col in ["open", "high", "low", "close", "volume", "turnover"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                df = df.dropna(subset=["open", "high", "low", "close", "volume", "turnover"])

                if len(df) < self._MIN_BARS:
                    continue

                # 均线与辅助指标
                df["ma5"] = df["close"].rolling(5).mean()
                df["ma10"] = df["close"].rolling(10).mean()
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()
                df["low_20"] = df["low"].rolling(20).min()

                # 用于计算三倍量倍数
                df["prev_volume"] = df["volume"].shift(1)

                # 三倍量上涨信号
                df["triple_volume_signal"] = (
                    (df["volume"] >= df["volume"].shift(1) * 3)
                    & (df["close"] > df["open"])
                    & (df["close"] > df["close"].shift(1))
                )

                last_pos = len(df) - 1
                last = df.iloc[-1]
                prev = df.iloc[-2]

                if self._is_risky_stock(symbol, last):
                    continue

                indicator_cols = ["ma5", "ma10", "ma20", "ma60", "vol_ma20", "low_20"]
                if last[indicator_cols].isna().any() or prev[["ma20", "ma60"]].isna().any():
                    continue

                # 基础流动性过滤
                if last["turnover"] < self._MIN_TURNOVER:
                    continue

                # 基础趋势过滤：只排除明显跌破中期趋势的股票
                # 不是必须强多头，只要求没有明显破位
                if last["close"] < last["ma60"] * 0.95:
                    continue

                stats["basic_filter"] += 1

                # 找最近一次三倍量信号，不包含今天
                signal_positions = np.flatnonzero(
                    df["triple_volume_signal"].iloc[:last_pos].to_numpy()
                )

                if len(signal_positions) == 0:
                    continue

                stats["has_signal"] += 1

                signal_pos = int(signal_positions[-1])
                signal_bar = df.iloc[signal_pos]

                days_after_signal = last_pos - signal_pos

                if not (
                    self._MIN_DAYS_AFTER_SIGNAL
                    <= days_after_signal
                    <= self._MAX_DAYS_AFTER_SIGNAL
                ):
                    continue

                stats["valid_signal"] += 1

                # 三倍量之后到昨日作为整理区间
                consolidation = df.iloc[signal_pos + 1:last_pos]

                if len(consolidation) < 3:
                    continue

                platform_high = self._safe_float(consolidation["high"].max())
                platform_low = self._safe_float(consolidation["low"].min())

                if platform_low <= 0:
                    continue

                # 只排除特别夸张的宽幅震荡
                if platform_high / platform_low > self._MAX_CONSOLIDATION_RANGE:
                    continue

                stats["candidate"] += 1

                score, detail = self._score_stock(
                    last=last,
                    prev=prev,
                    signal_bar=signal_bar,
                    consolidation=consolidation,
                    days_after_signal=days_after_signal,
                )

                if score < self._FALLBACK_MIN_SCORE:
                    continue

                detail["symbol"] = symbol
                all_candidates.append((symbol, score, detail))
                stats["fallback_score_pass"] += 1

                if score >= self._MIN_SCORE:
                    quality_candidates.append((symbol, score, detail))
                    stats["quality_score_pass"] += 1

            except Exception as exc:
                logger.warning(f"[{symbol}] TripleVolumeBreakoutStrategy 计算失败：{exc}")
                continue

        # 排序：优先按综合分数，其次按成交额
        all_candidates.sort(
            key=lambda x: (x[1], x[2].get("turnover", 0.0)),
            reverse=True,
        )
        quality_candidates.sort(
            key=lambda x: (x[1], x[2].get("turnover", 0.0)),
            reverse=True,
        )

        # 如果高质量候选足够，使用高质量候选；否则使用兜底候选填满前 8
        if len(quality_candidates) >= self._TARGET_MIN:
            final_selected = quality_candidates[: self._TARGET_MAX]
        else:
            final_selected = all_candidates[: self._TARGET_MAX]
            if len(final_selected) < self._TARGET_MIN:
                logger.warning(
                    "TripleVolumeBreakoutStrategy 今日候选不足 5 只，"
                    "可能是市场环境偏弱、三倍量信号不足，或本地行情未更新。"
                )

        stats["final"] = len(final_selected)

        candidates = [symbol for symbol, _, _ in final_selected]

        logger.info(f"TripleVolumeBreakoutStrategy 漏斗统计：{stats}")

        for symbol, score, detail in final_selected:
            logger.info(
                f"[{symbol}] score={score:.1f} "
                f"days={detail.get('days_after_signal', 0):.0f} "
                f"turnover={detail.get('turnover', 0):.0f} "
                f"shrink={detail.get('shrink_ratio', 0):.2f} "
                f"range={detail.get('consolidation_range', 0):.2f} "
                f"breakout_score={detail.get('breakout_score', 0):.1f} "
                f"trend_score={detail.get('trend_score', 0):.1f}"
            )

        logger.info(f"TripleVolumeBreakoutStrategy 最终选出 {len(candidates)} 只股票")

        return candidates


# 兼容你之前可能使用的文件名 / 类名
class ThreeTimeYilinStrategy(TripleVolumeBreakoutStrategy):
    """兼容别名：等同于 TripleVolumeBreakoutStrategy。"""

    pass

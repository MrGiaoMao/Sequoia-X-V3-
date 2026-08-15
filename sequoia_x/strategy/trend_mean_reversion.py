"""上升趋势超跌回归策略：中期趋势健康 + 短期超跌 + 止跌确认。"""

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class TrendMeanReversionStrategy(BaseStrategy):
    """上升趋势超跌回归策略（A股防接飞刀版）。

    策略定位：
    不是无脑抄底，而是在中期趋势仍然健康的股票里，寻找短期被情绪砸过头、
    但没有跌坏主趋势，并且已经出现初步止跌迹象的修复机会。

    选股条件（向量化，严禁 iterrows）：
    1. 中期趋势健康：20日均线 > 60日均线，且今日收盘价 > 60日均线
    2. 60日相对强度不弱：今日收盘价 > 60个交易日前收盘价
    3. 短期超跌：今日收盘价低于20日均线 3% - 10%
    4. 近5日明显回调：近5日跌幅 <= -6%，但不能跌幅过深（>-18%）
    5. 流动性过滤：今日成交额 > 100,000,000
    6. 防接飞刀过滤：最近两日不能连续接近跌停，今日不能接近跌停收盘
    7. 止跌确认：今日收阳，或今日收盘价高于昨日收盘价，或出现较长下影线

    排序逻辑：
    按综合修复分数排序，优先选择“偏离20日均线较明显、仍在60日线之上、
    且短期回调充分”的股票。

    Attributes:
        webhook_key: 路由到 'mean_reversion' 专属飞书机器人。
    """

    webhook_key: str = "mean_reversion"
    _MIN_BARS: int = 61  # 60日均线 + 60日前收盘价对比

    # 参数集中管理，方便后续回测调优
    _MIN_TURNOVER: float = 100_000_000
    _MIN_MA20_DEVIATION: float = 0.03
    _MAX_MA20_DEVIATION: float = 0.10
    _MIN_5D_DROP: float = -0.06
    _MAX_5D_DROP: float = -0.18
    _LIMIT_DOWN_THRESHOLD: float = 0.905
    _LONG_LOWER_SHADOW_RATIO: float = 0.35

    @staticmethod
    def _safe_numeric(df: pd.DataFrame) -> pd.DataFrame:
        """将策略所需字段转为数值，避免数据库中字符串类型导致比较异常。"""
        df = df.copy()
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["open", "high", "low", "close", "volume", "turnover"])

    def run(self) -> list[str]:
        """遍历全市场，返回满足上升趋势超跌回归条件的股票代码列表。"""
        all_data = self.engine.load_all_ohlcv()
        if not all_data:
            return []
        selected: list[tuple[str, float]] = []

        for symbol, df in all_data.items():
            try:
                if len(df) < self._MIN_BARS:
                    continue

                df = self._safe_numeric(df)
                if len(df) < self._MIN_BARS:
                    continue

                # 均线与成交量/价格行为指标
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()
                df["pct_5"] = df["close"] / df["close"].shift(5) - 1
                df["pct_60"] = df["close"] / df["close"].shift(60) - 1
                df["limit_down_like"] = df["close"] <= df["close"].shift(1) * self._LIMIT_DOWN_THRESHOLD

                last = df.iloc[-1]
                prev = df.iloc[-2]

                required_cols = ["ma20", "ma60", "vol_ma20", "pct_5", "pct_60"]
                if any(pd.isna(last[col]) for col in required_cols):
                    continue

                if last["ma20"] <= 0 or last["ma60"] <= 0 or last["vol_ma20"] <= 0:
                    continue

                # 条件 1：中期趋势健康，避免买入长期下跌趋势
                trend_ok = (
                    last["ma20"] > last["ma60"]
                    and last["close"] > last["ma60"]
                    and last["pct_60"] > 0
                )

                # 条件 2：短期价格明显低于20日均线，但不能偏离过深
                ma20_deviation = (last["ma20"] - last["close"]) / last["ma20"]
                oversold_ok = (
                    self._MIN_MA20_DEVIATION <= ma20_deviation <= self._MAX_MA20_DEVIATION
                )

                # 条件 3：近5日有明显回调，但排除崩盘式下跌
                pullback_ok = self._MAX_5D_DROP < last["pct_5"] <= self._MIN_5D_DROP

                # 条件 4：流动性充足，避免反弹无法成交
                liquid_ok = last["turnover"] > self._MIN_TURNOVER

                # 条件 5：防接飞刀，排除连续接近跌停和今日接近跌停收盘
                two_day_limit_down = bool(last["limit_down_like"] and prev["limit_down_like"])
                today_limit_down = last["close"] <= prev["close"] * self._LIMIT_DOWN_THRESHOLD
                risk_ok = not two_day_limit_down and not today_limit_down

                # 条件 6：止跌确认，至少出现一种承接迹象
                intraday_range = last["high"] - last["low"]
                body_low = min(last["open"], last["close"])
                lower_shadow = body_low - last["low"]
                long_lower_shadow = (
                    intraday_range > 0
                    and lower_shadow / intraday_range >= self._LONG_LOWER_SHADOW_RATIO
                    and last["close"] > last["low"] * 1.02
                )

                bullish_candle = last["close"] > last["open"]
                close_up = last["close"] > prev["close"]
                stop_falling_ok = bullish_candle or close_up or long_lower_shadow

                if not (
                    trend_ok
                    and oversold_ok
                    and pullback_ok
                    and liquid_ok
                    and risk_ok
                    and stop_falling_ok
                ):
                    continue

                # 综合排序分数：偏离20日线越明显、短期回调越充分、离60日线越安全，分数越高
                distance_above_ma60 = (last["close"] - last["ma60"]) / last["ma60"]
                score = (
                    ma20_deviation * 100
                    + abs(last["pct_5"]) * 50
                    + distance_above_ma60 * 20
                )

                selected.append((symbol, float(score)))

            except Exception as exc:
                logger.warning(f"[{symbol}] TrendMeanReversionStrategy 计算失败：{exc}")
                continue

        selected.sort(key=lambda item: item[1], reverse=True)
        result = [symbol for symbol, _ in selected]

        logger.info(f"TrendMeanReversionStrategy 选出 {len(result)} 只股票")
        return result

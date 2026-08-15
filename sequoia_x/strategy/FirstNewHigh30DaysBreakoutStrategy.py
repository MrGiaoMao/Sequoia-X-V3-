"""30日首次新高均线发散突破策略:首次新高 + 均线多头发散 + 布林中轨强势 + 放量确认。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class FirstNewHigh30DaysBreakoutStrategy(BaseStrategy):
    """30日首次新高均线发散突破策略。

    选股逻辑(V1 稳健版,适合作为强势股候选池):
    1. 30日首次新高:
       - 今日最高价突破过去30个交易日最高价
       - 收盘价不低于过去30日高点的98%,过滤盘中虚破
       - 过去29个交易日内没有出现过同类有效突破
    2. 均线多头排列:
       - MA5 > MA10 > MA20 > MA60
    3. 均线上行与发散:
       - MA5、MA10、MA20 均较上一交易日上升
       - MA60 不明显走弱,要求 MA60 >= 5日前 MA60
       - MA5-MA10、MA10-MA20 的间距继续扩大
    4. 布林中轨强势:
       - 最近3个交易日收盘价均在20日均线之上
    5. 成交量放大:
       - 今日成交量 > 昨日前5日均量 * 1.2
       - 今日成交量 < 昨日前20日均量 * 3,过滤异常爆量高潮
    6. 收盘质量:
       - 今日阳线
       - 收盘价处于当日振幅上半区,减少长上影假突破
    7. 流动性:
       - 今日成交额大于 5 亿
    8. 风险过滤:
       - 排除 ST、停牌、异常无成交个股

    注意:
    - 这里把"今日"视为突破确认日,信号在收盘后确认。
    - 实盘执行时建议 T 日收盘后生成候选,T+1 结合开盘涨幅、板块环境和指数环境决定是否买入。
    - 本类只负责选股,不包含卖出逻辑;卖出建议另设止损、时间止损和移动止盈模块。
    """

    webhook_key: str = "first_new_high_30d_breakout"

    # 至少需要 30 日新高、60 日均线,并保留足够冗余用于"首次突破"判断
    _MIN_BARS: int = 100

    # 30日首次新高窗口
    _HIGH_PERIOD: int = 30
    _FIRST_LOOKBACK_DAYS: int = 29

    # 有效突破:收盘价至少回到前高的98%,避免盘中冲高回落
    _MIN_CLOSE_TO_PREV_HIGH: float = 0.98

    # 放量确认:今日成交量 > 昨日前5日均量 * 1.2
    _VOLUME_EXPAND_RATIO: float = 1.20

    # 异常爆量过滤:今日成交量 < 昨日前20日均量 * 3
    _MAX_VOLUME_OVERHEAT_RATIO: float = 3.00

    # 收盘强度:收盘价至少处于当日振幅的60%位置
    _MIN_CLOSE_POSITION: float = 0.60

    # 成交额过滤:5 亿
    _MIN_TURNOVER: float = 500_000_000

    # 是否启用突破日涨幅上限。默认启用,避免买在短线高潮。
    _ENABLE_DAILY_GAIN_CAP: bool = True
    _MAX_DAILY_GAIN: float = 0.085

    # 是否启用市场环境过滤。默认关闭,因为不同 engine 的指数数据接口可能不同。
    # 如果你的 engine 支持 get_ohlcv("000300.SH") / get_ohlcv("000905.SH") / get_ohlcv("399006.SZ"),
    # 可以改为 True。
    _ENABLE_MARKET_FILTER: bool = False
    _MARKET_INDEX_SYMBOLS: tuple[str, ...] = ("000300.SH", "000905.SH", "399006.SZ")

    def _get_amount_column(self, df: pd.DataFrame) -> str | None:
        """兼容不同数据源的成交额字段。"""
        for col in ["turnover", "amount", "money"]:
            if col in df.columns:
                return col
        return None

    def _is_risky_stock(self, symbol: str, last: pd.Series) -> bool:
        """尽量排除 ST、停牌、异常无成交股票。"""

        # 停牌或异常数据
        if last["open"] <= 0 or last["close"] <= 0 or last["volume"] <= 0:
            return True

        # 如果 engine 支持获取股票名称,则排除 ST / *ST / 退市股
        get_stock_name = getattr(self.engine, "get_stock_name", None)

        if callable(get_stock_name):
            try:
                name = get_stock_name(symbol)
                if name:
                    name = str(name).upper()
                    if "ST" in name or "*" in name or "退" in name:
                        return True
            except Exception:
                # 获取名称失败不直接判定为风险股,避免误杀
                pass

        return False

    def _market_environment_ok(self) -> bool:
        """可选市场环境过滤:至少一个主要指数站上20日线且20日线向上。"""

        if not self._ENABLE_MARKET_FILTER:
            return True

        for index_symbol in self._MARKET_INDEX_SYMBOLS:
            try:
                df = self.engine.get_ohlcv(index_symbol)

                if df is None or len(df) < 25:
                    continue

                required_cols = {"close"}
                if not required_cols.issubset(df.columns):
                    continue

                df = df.copy()
                if "date" in df.columns:
                    df = df.sort_values("date")

                df["ma20"] = df["close"].rolling(20).mean()
                last = df.iloc[-1]
                prev = df.iloc[-2]

                if (
                    pd.notna(last["ma20"])
                    and pd.notna(prev["ma20"])
                    and last["close"] > last["ma20"]
                    and last["ma20"] > prev["ma20"]
                ):
                    return True

            except Exception as exc:
                logger.warning(f"[{index_symbol}] 市场环境过滤计算失败:{exc}")
                continue

        return False

    def run(self) -> list[str]:
        """遍历全市场，返回满足30日首次新高均线发散突破条件的股票代码列表。"""

        if not self._market_environment_ok():
            logger.info("FirstNewHigh30DaysBreakoutStrategy 市场环境不满足，今日不选股")
            return []

        all_data = self.engine.load_all_ohlcv()
        if not all_data:
            return []

        selected: list[tuple[str, float, float, float]] = []

        for symbol, df in all_data.items():
            try:
                if df is None or len(df) < self._MIN_BARS:
                    continue

                required_cols = {"open", "high", "low", "close", "volume"}
                if not required_cols.issubset(df.columns):
                    continue

                amount_col = self._get_amount_column(df)
                if amount_col is None:
                    continue

                if "date" in df.columns:
                    df = df.sort_values("date")

                # 均线
                df["ma5"] = df["close"].rolling(5).mean()
                df["ma10"] = df["close"].rolling(10).mean()
                df["ma20"] = df["close"].rolling(20).mean()
                df["ma60"] = df["close"].rolling(60).mean()
                df["vol_ma5"] = df["volume"].rolling(5).mean()
                df["vol_ma20"] = df["volume"].rolling(20).mean()
                df["prev_30_high"] = df["high"].shift(1).rolling(self._HIGH_PERIOD).max()
                df["valid_30_high_breakout"] = (
                    (df["high"] > df["prev_30_high"])
                    & (df["close"] >= df["prev_30_high"] * self._MIN_CLOSE_TO_PREV_HIGH)
                )
                df["first_valid_breakout"] = (
                    df["valid_30_high_breakout"]
                    & (df["valid_30_high_breakout"].shift(1).rolling(self._FIRST_LOOKBACK_DAYS).sum() == 0)
                )

                last_pos = len(df) - 1
                last = df.iloc[last_pos]
                prev = df.iloc[last_pos - 1]

                if self._is_risky_stock(symbol, last):
                    continue

                indicator_cols = ["ma5", "ma10", "ma20", "ma60", "vol_ma5", "vol_ma20", "prev_30_high"]
                if last[indicator_cols].isna().any():
                    continue

                # 条件1：首次有效突破
                if not bool(last["first_valid_breakout"]):
                    continue

                # 条件2：均线多头排列
                bull_alignment = (
                    last["ma5"] > last["ma10"]
                    and last["ma10"] > last["ma20"]
                    and last["ma20"] > last["ma60"]
                )

                # 条件3：均线上行
                ma_rising = (
                    last["ma5"] > prev["ma5"]
                    and last["ma10"] > prev["ma10"]
                    and last["ma20"] > prev["ma20"]
                    and last["ma60"] >= df["ma60"].iloc[-5]
                )

                # 条件4：均线发散
                spread_5_10 = last["ma5"] - last["ma10"]
                prev_spread_5_10 = prev["ma5"] - prev["ma10"]
                spread_10_20 = last["ma10"] - last["ma20"]
                prev_spread_10_20 = prev["ma10"] - prev["ma20"]
                ma_diverging = spread_5_10 > prev_spread_5_10 and spread_10_20 > prev_spread_10_20

                ma_condition = bull_alignment and ma_rising and ma_diverging

                # 条件5：布林中轨强势
                above_mid_3days = (
                    last["close"] > last["ma20"]
                    and prev["close"] > prev["ma20"]
                    and df["close"].iloc[-3] > df["ma20"].iloc[-3]
                )

                # 条件6：成交量放大且不过度爆量
                prev_vol_ma5 = prev["vol_ma5"]
                prev_vol_ma20 = prev["vol_ma20"]
                if pd.isna(prev_vol_ma5) or pd.isna(prev_vol_ma20) or prev_vol_ma5 <= 0 or prev_vol_ma20 <= 0:
                    continue
                volume_expanded = last["volume"] > prev_vol_ma5 * self._VOLUME_EXPAND_RATIO
                volume_not_overheated = last["volume"] < prev_vol_ma20 * self._MAX_VOLUME_OVERHEAT_RATIO
                volume_condition = volume_expanded and volume_not_overheated

                # 条件7：收盘质量
                day_range = last["high"] - last["low"]
                if day_range <= 0:
                    continue
                close_position = (last["close"] - last["low"]) / day_range
                strong_close = (
                    last["close"] > last["open"]
                    and last["close"] > prev["close"]
                    and close_position >= self._MIN_CLOSE_POSITION
                )

                # 条件8：涨幅上限
                if self._ENABLE_DAILY_GAIN_CAP:
                    daily_gain_ok = (last["close"] / prev["close"] - 1) <= self._MAX_DAILY_GAIN
                else:
                    daily_gain_ok = True

                # 条件9：成交额
                liquid = last[amount_col] > self._MIN_TURNOVER

                if ma_condition and above_mid_3days and volume_condition and strong_close and daily_gain_ok and liquid:
                    volume_ratio = float(last["volume"] / prev_vol_ma5)
                    breakout_ratio = float(last["close"] / last["prev_30_high"] - 1)
                    selected.append((symbol, float(last[amount_col]), volume_ratio, breakout_ratio))

            except Exception as exc:
                logger.warning(f"[{symbol}] FirstNewHigh30DaysBreakoutStrategy 计算失败：{exc}")
                continue

        selected.sort(key=lambda x: x[1], reverse=True)
        candidates = [symbol for symbol, _, _, _ in selected]

        logger.info(f"FirstNewHigh30DaysBreakoutStrategy 选出 {len(candidates)} 只股票")
        return candidates

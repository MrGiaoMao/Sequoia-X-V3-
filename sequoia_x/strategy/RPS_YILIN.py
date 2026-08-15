import sqlite3

import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class RpsYilinStrategy(BaseStrategy):
    """RPS 亦霖版中等严格突破策略。

    核心逻辑：
    1. RPS 强度：过去 120 日涨幅处于全市场前 5%，即 RPS >= 95
    2. 真突破：今日收盘价突破前 120 个交易日最高价，不再使用“距离高点 10% 内”
    3. 放量确认：今日成交量 > 过去 20 日均量的 1.5 倍，均量不含今日
    4. 流动性：今日成交额 > 1 亿，过滤成交稀薄的小票
    5. K 线质量：今日实体阳线，且收盘价高于昨日收盘价
    6. 趋势过滤：今日 close > MA20 > MA60
    7. 控制输出：按 RPS、成交额、突破强度排序，只返回前 30 只
    """

    webhook_key: str = "rps_yilin"

    # RPS 参数
    rps_period: int = 120
    rps_threshold: int = 95

    # 过滤参数
    volume_ma_period: int = 20
    volume_multiplier: float = 1.5
    min_turnover: float = 100_000_000
    max_results: int = 30

    # 均线参数
    ma_fast_period: int = 20
    ma_slow_period: int = 60

    def run(self) -> list[str]:
        try:
            with sqlite3.connect(self.engine.db_path) as conn:
                df = pd.read_sql(
                    """
                    SELECT symbol, date, open, high, low, close, volume, turnover
                    FROM stock_daily
                    """,
                    conn,
                )
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            logger.info("RpsYilinStrategy 无行情数据")
            return []

        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["symbol", "date"])

        numeric_cols = ["open", "high", "low", "close", "volume", "turnover"]
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["open", "high", "close", "volume", "turnover"])

        grouped = df.groupby("symbol", group_keys=False)

        # 纵向计算过去 rps_period 日涨幅，用于横向 RPS 排名。
        df["close_shift"] = grouped["close"].shift(self.rps_period)
        df["pct_change"] = (df["close"] - df["close_shift"]) / df["close_shift"]

        # 真突破应比较“今日收盘价”与“昨日以前的 rps_period 日最高价”。
        # shift(1) 用于排除今日 high，避免把今日盘中高点误算进前高。
        df["prev_high"] = grouped["high"].transform(
            lambda s: s.shift(1).rolling(
                self.rps_period,
                min_periods=self.rps_period,
            ).max()
        )

        # 成交量均值也排除今日，避免今日放量抬高均值。
        df["vol_ma20"] = grouped["volume"].transform(
            lambda s: s.shift(1).rolling(
                self.volume_ma_period,
                min_periods=self.volume_ma_period,
            ).mean()
        )

        # 趋势均线使用当日收盘价计算，确认目前仍处于上升趋势。
        df["ma20"] = grouped["close"].transform(
            lambda s: s.rolling(
                self.ma_fast_period,
                min_periods=self.ma_fast_period,
            ).mean()
        )
        df["ma60"] = grouped["close"].transform(
            lambda s: s.rolling(
                self.ma_slow_period,
                min_periods=self.ma_slow_period,
            ).mean()
        )
        df["prev_close"] = grouped["close"].shift(1)

        latest_date = df["date"].max()
        latest_df = df[df["date"] == latest_date].copy()
        latest_df = latest_df.dropna(
            subset=[
                "pct_change",
                "prev_high",
                "vol_ma20",
                "ma20",
                "ma60",
                "prev_close",
            ]
        )

        if latest_df.empty:
            logger.info("RpsYilinStrategy 最新交易日无满足基础数据长度的股票")
            return []

        # 横向 RPS 排位：只在最新交易日做全市场排名。
        latest_df["rps"] = latest_df["pct_change"].rank(pct=True) * 100

        # 条件 1：RPS 强度，从原来的前 10% 收紧到前 5%。
        rps_strong = latest_df["rps"] >= self.rps_threshold

        # 条件 2：真突破，收盘价必须站上前 120 日高点。
        true_breakout = latest_df["close"] > latest_df["prev_high"]

        # 条件 3：成交额过滤，避免流动性不足。
        liquid = latest_df["turnover"] > self.min_turnover

        # 条件 4：放量确认，必须大于过去 20 日均量的 1.5 倍。
        volume_confirm = latest_df["volume"] > latest_df["vol_ma20"] * self.volume_multiplier

        # 条件 5：K 线质量，必须是实体阳线且今日真涨。
        bullish_candle = latest_df["close"] > latest_df["open"]
        real_up = latest_df["close"] > latest_df["prev_close"]

        # 条件 6：趋势过滤，价格在 MA20 上方，且 MA20 > MA60。
        trend_ok = (latest_df["close"] > latest_df["ma20"]) & (
            latest_df["ma20"] > latest_df["ma60"]
        )

        selected = latest_df[
            rps_strong
            & true_breakout
            & liquid
            & volume_confirm
            & bullish_candle
            & real_up
            & trend_ok
        ].copy()

        if selected.empty:
            logger.info("RpsYilinStrategy 选出 0 只股票")
            return []

        # 排序逻辑：优先看 RPS，其次看成交额，再看突破幅度。
        selected["breakout_strength"] = selected["close"] / selected["prev_high"] - 1
        selected = selected.sort_values(
            by=["rps", "turnover", "breakout_strength"],
            ascending=[False, False, False],
        )

        selected = selected.head(self.max_results)

        logger.info(
            f"RpsYilinStrategy 选出 {len(selected)} 只股票 "
            f"| latest_date={latest_date.date()} "
            f"| rps_threshold={self.rps_threshold} "
            f"| min_turnover={self.min_turnover:.0f}"
        )
        return selected["symbol"].tolist()


class RpsBreakoutStrategy(RpsYilinStrategy):
    """兼容旧类名：等同于 RpsYilinStrategy。"""

    pass

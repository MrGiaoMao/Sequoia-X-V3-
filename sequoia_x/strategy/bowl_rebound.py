"""碗口反弹策略：放量异动 + 上升趋势回踩 + KDJ低位反弹。

策略来源：
- 参考 GitHub a-share-quant-selector 的 bowl_rebound.py 思路
- 改写为 Sequoia-X 当前 BaseStrategy + run() 模式

核心逻辑：
1. 短期趋势线 > 多空线，说明股票仍处于右侧趋势
2. 最近 M 天内出现过放量阳线，说明曾有资金异动
3. 当前 KDJ-J 值低位，说明短线回调较充分
4. 当前价格回落到“碗中”或靠近关键趋势线
5. 今日成交额足够，避免流动性太差的小票
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class BowlReboundStrategy(BaseStrategy):
    """碗口反弹策略。

    这个策略不是突破追涨，而是强势股回踩低吸模型。

    选股条件：
    1. 趋势结构：短期趋势线 > 多空线
    2. 资金异动：最近 M 天内出现过 N 倍放量阳线
    3. 动能回落：KDJ-J <= J_VAL
    4. 价格位置：回落碗中 / 靠近多空线 / 靠近短期趋势线
    5. 流动性：今日成交额 >= MIN_TURNOVER
    6. 风险过滤：排除停牌、无成交、ST、退市类股票

    返回：
        满足条件的股票代码列表。
    """

    webhook_key: str = "bowl_rebound"

    # 至少需要 114 日均线 + KDJ + 冗余
    _MIN_BARS: int = 130

    # 最近 M 天内出现 N 倍放量阳线
    _LOOKBACK_DAYS: int = 15
    _VOLUME_MULTIPLIER: float = 4.0

    # KDJ J 值低位
    _J_MAX: float = 30.0

    # 成交额过滤：先用 1 亿，后续可以提高到 2 亿 / 5 亿
    _MIN_TURNOVER: float = 100_000_000

    # 价格靠近多空线、短期趋势线的容忍范围
    _DUOKONG_PCT: float = 0.03
    _SHORT_TREND_PCT: float = 0.02

    # 多空线均线参数
    _M1: int = 14
    _M2: int = 28
    _M3: int = 57
    _M4: int = 114

    @staticmethod
    def _ema(series: pd.Series, span: int) -> pd.Series:
        """计算 EMA。"""
        return series.ewm(span=span, adjust=False).mean()

    @classmethod
    def _calculate_kdj(
        cls,
        df: pd.DataFrame,
        n: int = 9,
        m1: int = 3,
        m2: int = 3,
    ) -> pd.DataFrame:
        """计算 KDJ 指标。"""
        low_n = df["low"].rolling(n).min()
        high_n = df["high"].rolling(n).max()

        denominator = high_n - low_n
        rsv = (df["close"] - low_n) / denominator.replace(0, np.nan) * 100
        rsv = rsv.fillna(50)

        # 通达信 SMA 近似：alpha = 1 / m
        k = rsv.ewm(alpha=1 / m1, adjust=False).mean()
        d = k.ewm(alpha=1 / m2, adjust=False).mean()
        j = 3 * k - 2 * d

        return pd.DataFrame(
            {
                "K": k,
                "D": d,
                "J": j,
            },
            index=df.index,
        )

    def _is_risky_stock(self, symbol: str, last: pd.Series) -> bool:
        """排除停牌、无成交、ST、退市类股票。"""
        if last["open"] <= 0 or last["close"] <= 0 or last["volume"] <= 0:
            return True

        get_stock_name = getattr(self.engine, "get_stock_name", None)

        if callable(get_stock_name):
            try:
                name = str(get_stock_name(symbol)).upper()
                if "ST" in name or "*" in name or "退" in name:
                    return True
            except Exception:
                pass

        return False

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算策略所需指标。"""
        result = df.copy()

        # 1. 短期趋势线：EMA(EMA(CLOSE, 10), 10)
        ema10 = self._ema(result["close"], 10)
        result["short_term_trend"] = self._ema(ema10, 10)

        # 2. 多空线：MA14、MA28、MA57、MA114 的平均值
        result["ma_m1"] = result["close"].rolling(self._M1).mean()
        result["ma_m2"] = result["close"].rolling(self._M2).mean()
        result["ma_m3"] = result["close"].rolling(self._M3).mean()
        result["ma_m4"] = result["close"].rolling(self._M4).mean()

        result["bull_bear_line"] = (
            result["ma_m1"]
            + result["ma_m2"]
            + result["ma_m3"]
            + result["ma_m4"]
        ) / 4

        # 3. 趋势线在上
        result["trend_above"] = result["short_term_trend"] > result["bull_bear_line"]

        # 4. 价格位置分类
        result["fall_in_bowl"] = (
            (result["close"] >= result["bull_bear_line"])
            & (result["close"] <= result["short_term_trend"])
        )

        result["near_duokong"] = (
            (result["close"] >= result["bull_bear_line"] * (1 - self._DUOKONG_PCT))
            & (result["close"] <= result["bull_bear_line"] * (1 + self._DUOKONG_PCT))
        )

        result["near_short_trend"] = (
            (result["close"] >= result["short_term_trend"] * (1 - self._SHORT_TREND_PCT))
            & (result["close"] <= result["short_term_trend"] * (1 + self._SHORT_TREND_PCT))
        )

        # 5. KDJ
        kdj = self._calculate_kdj(result)
        result["K"] = kdj["K"]
        result["D"] = kdj["D"]
        result["J"] = kdj["J"]

        # 6. 放量阳线
        result["vol_ratio"] = result["volume"] / result["volume"].shift(1)
        result["vol_surge"] = result["vol_ratio"] >= self._VOLUME_MULTIPLIER
        result["positive_candle"] = result["close"] > result["open"]

        result["key_candle"] = result["vol_surge"] & result["positive_candle"]

        return result

    def run(self) -> list[str]:
        """遍历全市场，返回满足碗口反弹条件的股票代码列表。"""
        all_data = self.engine.load_all_ohlcv()
        if not all_data:
            return []

        selected: list[tuple[str, float, str, float]] = []

        stats = {
            "total": 0,
            "enough_bars": 0,
            "trend_above": 0,
            "j_low": 0,
            "has_key_candle": 0,
            "position_ok": 0,
            "liquid": 0,
            "selected": 0,
        }

        for symbol, df in all_data.items():
            stats["total"] += 1

            try:
                if df is None or len(df) < self._MIN_BARS:
                    continue

                stats["enough_bars"] += 1

                required_cols = {"open", "high", "low", "close", "volume", "turnover"}
                if not required_cols.issubset(df.columns):
                    logger.warning(f"[{symbol}] 缺少字段：{required_cols - set(df.columns)}")
                    continue

                if "date" in df.columns:
                    df = df.sort_values("date")
                else:
                    df = df.sort_index()

                for col in ["open", "high", "low", "close", "volume", "turnover"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

                df = df.dropna(subset=["open", "high", "low", "close", "volume", "turnover"])

                if len(df) < self._MIN_BARS:
                    continue

                df = self._calculate_indicators(df)

                last = df.iloc[-1]

                if self._is_risky_stock(symbol, last):
                    continue

                indicator_cols = [
                    "short_term_trend",
                    "bull_bear_line",
                    "K",
                    "D",
                    "J",
                    "vol_ratio",
                ]

                if last[indicator_cols].isna().any():
                    continue

                # 条件 1：趋势线在上
                trend_above = bool(last["trend_above"])
                if not trend_above:
                    continue
                stats["trend_above"] += 1

                # 条件 2：J 值低位
                j_low = last["J"] <= self._J_MAX
                if not j_low:
                    continue
                stats["j_low"] += 1

                # 条件 3：最近 M 天存在放量阳线
                lookback = df.tail(self._LOOKBACK_DAYS)

                if lookback.empty:
                    continue

                # 如果最近 M 天最大成交量那天是阴线，说明最大量可能是抛压，剔除
                max_volume_idx = lookback["volume"].idxmax()
                max_volume_row = lookback.loc[max_volume_idx]

                if max_volume_row["close"] < max_volume_row["open"]:
                    continue

                key_candles = lookback[lookback["key_candle"]]

                if key_candles.empty:
                    continue
                stats["has_key_candle"] += 1

                # 条件 4：价格位置
                if last["fall_in_bowl"]:
                    category = "回落碗中"
                    position_score = 3
                elif last["near_duokong"]:
                    category = "靠近多空线"
                    position_score = 2
                elif last["near_short_trend"]:
                    category = "靠近短期趋势线"
                    position_score = 1
                else:
                    continue

                stats["position_ok"] += 1

                # 条件 5：成交额过滤
                liquid = last["turnover"] >= self._MIN_TURNOVER
                if not liquid:
                    continue

                stats["liquid"] += 1

                # 排序分数：
                # 位置越好、J值越低、成交额越高，排名越靠前
                j_score = max(0, self._J_MAX - float(last["J"]))
                turnover_score = float(last["turnover"]) / 100_000_000

                score = position_score * 100 + j_score + turnover_score

                selected.append(
                    (
                        symbol,
                        score,
                        category,
                        float(last["J"]),
                    )
                )

                stats["selected"] += 1

            except Exception as exc:
                logger.warning(f"[{symbol}] BowlReboundStrategy 计算失败：{exc}")
                continue

        # 按综合分数排序
        selected.sort(key=lambda x: x[1], reverse=True)

        candidates = [symbol for symbol, _, _, _ in selected]

        logger.info(f"BowlReboundStrategy 漏斗统计：{stats}")
        logger.info(f"BowlReboundStrategy 选出 {len(candidates)} 只股票")

        return candidates

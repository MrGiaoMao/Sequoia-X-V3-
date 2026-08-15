"""A股止跌候选筛选策略。

设计目标：
1. 只在股票此前确实经历下跌时寻找止跌；
2. 使用价格结构、下跌减速、抛压衰竭、日内承接、波动正常化、
   相对强度和短期突破等相对独立的信号；
3. 所有指标仅使用信号日及以前的数据，避免未来函数；
4. 保持 BaseStrategy.run() -> list[str] 的现有系统接口。

说明：
- 技术指标价格建议使用前复权数据；
- 成交量/成交额/换手率应使用未复权原始数据；
- 本策略负责生成止跌候选池，不等同于最终买入指令。
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from sequoia_x.core.logger import get_logger
from sequoia_x.strategy.base import BaseStrategy

logger = get_logger(__name__)


class StopFallStrategy(BaseStrategy):
    """A股止跌候选策略（S2 初步企稳 / S3 止跌确认）。"""

    webhook_key: str = "stop_fall"

    # ---------- 下跌背景 ----------
    min_history_days: int = 80
    drawdown_period: int = 60
    drawdown_threshold: float = -0.12
    trend_period: int = 20
    near_low_atr_limit: float = 2.0

    # ---------- 价格结构 ----------
    atr_period: int = 20
    low_hold_tolerance_atr: float = -0.50
    breakout_period: int = 5

    # ---------- 横截面筛选 ----------
    selection_quantile: float = 0.80
    improving_market_score_floor: float = 60.0
    neutral_market_score_floor: float = 65.0
    weak_market_score_floor: float = 70.0
    min_confirmation_signals: int = 4

    # True 时仅输出已突破短期高点的 S3 股票；False 时也输出高分 S2 股票。
    require_breakout: bool = False

    # ---------- 市场环境 ----------
    weak_breadth_threshold: float = 0.30
    improving_breadth_threshold: float = 0.45
    improving_breadth_change: float = 0.05

    # 可用作抛压代理的字段，按优先级选择。
    liquidity_column_candidates: tuple[str, ...] = (
        "turnover_rate",
        "turnover",
        "amount",
        "volume",
        "vol",
    )

    def run(self) -> list[str]:
        try:
            df, liquidity_col = self._load_daily_data()
        except Exception as exc:
            logger.error(f"读取数据库失败: {exc}")
            return []

        if df.empty:
            logger.info("StopFallStrategy 无可用日线数据")
            return []

        try:
            feature_df = self._build_features(df, liquidity_col)
        except Exception as exc:
            logger.exception(f"计算止跌因子失败: {exc}")
            return []

        if feature_df.empty:
            return []

        latest_date = feature_df["date"].max()
        latest = feature_df[feature_df["date"] == latest_date].copy()

        if latest.empty:
            return []

        # 下跌背景是硬条件，避免把普通上涨中的小回调误判为止跌。
        candidate_mask = (
            (latest["history_count"] >= self.min_history_days)
            & (latest["drawdown_60"] <= self.drawdown_threshold)
            & (latest["prior_return_20"] < 0.0)
            & (latest["near_low_atr"] <= self.near_low_atr_limit)
            & (latest["low_hold"] >= self.low_hold_tolerance_atr)
            & (latest["atr_20"] > 0.0)
            & (~latest["one_price_limit_down"])
        )

        candidates = latest.loc[candidate_mask].copy()
        if candidates.empty:
            logger.info(
                "StopFallStrategy %s 无股票通过下跌背景与前低企稳过滤",
                latest_date.date(),
            )
            return []

        candidates = self._score_candidates(candidates)
        breadth, breadth_change, market_regime = self._get_market_regime(
            feature_df, latest_date
        )

        score_floor = self._score_floor_for_regime(market_regime)
        if len(candidates) >= 10:
            quantile_cutoff = float(
                candidates["stop_score"].quantile(self.selection_quantile)
            )
            score_cutoff = max(score_floor, quantile_cutoff)
        else:
            score_cutoff = score_floor

        # 这些条件保证模型不是仅凭“跌得多”选股。
        final_mask = (
            (candidates["stop_score"] >= score_cutoff)
            & (candidates["deceleration"] > 0.0)
            & (candidates["relative_strength_5"] > 0.0)
            & (candidates["clv_3"] > -0.20)
            & (
                candidates["confirmation_count"]
                >= self.min_confirmation_signals
            )
        )

        # 弱市中只接受已经突破确认的股票，降低“接飞刀”概率。
        if market_regime == "weak" or self.require_breakout:
            final_mask &= candidates["breakout"]

        selected = candidates.loc[final_mask].copy()
        selected["stage"] = np.where(selected["breakout"], "S3", "S2")
        selected = selected.sort_values(
            ["stage", "stop_score", "relative_strength_5"],
            ascending=[False, False, False],
        )

        logger.info(
            "StopFallStrategy %s: 候选=%d, 选出=%d, 市场=%s, "
            "Breadth=%.1f%%, Breadth5D=%+.1f%%, 分数线=%.2f",
            latest_date.date(),
            len(candidates),
            len(selected),
            market_regime,
            breadth * 100.0 if np.isfinite(breadth) else float("nan"),
            breadth_change * 100.0
            if np.isfinite(breadth_change)
            else float("nan"),
            score_cutoff,
        )

        if not selected.empty:
            preview_cols = [
                "symbol",
                "stage",
                "stop_score",
                "drawdown_60",
                "low_hold",
                "relative_strength_5",
            ]
            logger.debug(
                "StopFallStrategy 排名前列:\n%s",
                selected[preview_cols].head(20).to_string(index=False),
            )

        return selected["symbol"].astype(str).tolist()

    def _load_daily_data(self) -> tuple[pd.DataFrame, str | None]:
        """按实际表结构读取字段，成交活跃度字段缺失时降级为价格模型。"""
        with sqlite3.connect(self.engine.db_path) as conn:
            schema_rows = conn.execute("PRAGMA table_info(stock_daily)").fetchall()
            available_columns = {str(row[1]) for row in schema_rows}

            required_columns = {"symbol", "date", "high", "low", "close"}
            missing_columns = required_columns - available_columns
            if missing_columns:
                missing_text = ", ".join(sorted(missing_columns))
                raise ValueError(f"stock_daily 缺少必要字段: {missing_text}")

            liquidity_col = next(
                (
                    col
                    for col in self.liquidity_column_candidates
                    if col in available_columns
                ),
                None,
            )

            selected_columns = ["symbol", "date", "high", "low", "close"]
            if liquidity_col is not None:
                selected_columns.append(liquidity_col)

            # 名称和 ST 标记仅在表中存在时读取，不强依赖具体股票基础表。
            for optional_col in ("name", "is_st"):
                if optional_col in available_columns:
                    selected_columns.append(optional_col)

            sql = f"SELECT {', '.join(selected_columns)} FROM stock_daily"
            df = pd.read_sql(sql, conn)

        if liquidity_col is None:
            logger.warning(
                "stock_daily 未找到换手率/成交额/成交量字段，"
                "将跳过抛压衰竭因子并自动重分配权重"
            )

        return df, liquidity_col

    def _build_features(
        self, df: pd.DataFrame, liquidity_col: str | None
    ) -> pd.DataFrame:
        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")

        numeric_columns = ["high", "low", "close"]
        if liquidity_col is not None:
            numeric_columns.append(liquidity_col)

        for col in numeric_columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df.dropna(subset=["symbol", "date", "high", "low", "close"])
        df = df[
            (df["close"] > 0.0)
            & (df["high"] > 0.0)
            & (df["low"] > 0.0)
            & (df["high"] >= df["low"])
        ].copy()

        # 如果日线表带股票名称或 ST 标记，则在这里进行时点过滤。
        if "name" in df.columns:
            st_name_mask = df["name"].astype(str).str.contains(
                r"(?:\*?ST|退)", case=False, regex=True, na=False
            )
            df = df[~st_name_mask].copy()

        if "is_st" in df.columns:
            st_values = (
                df["is_st"]
                .astype(str)
                .str.strip()
                .str.lower()
                .isin({"1", "1.0", "true", "t", "yes", "y", "st", "是"})
            )
            df = df[~st_values].copy()

        df = (
            df.sort_values(["symbol", "date"])
            .drop_duplicates(["symbol", "date"], keep="last")
            .reset_index(drop=True)
        )
        group = df.groupby("symbol", group_keys=False, sort=False)

        df["history_count"] = group.cumcount() + 1
        df["prev_close"] = group["close"].shift(1)
        df["return_1"] = group["close"].pct_change(fill_method=None)

        # ATR：使用真实波幅，所有计算只依赖当日和历史数据。
        true_range = pd.concat(
            [
                df["high"] - df["low"],
                (df["high"] - df["prev_close"]).abs(),
                (df["low"] - df["prev_close"]).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["true_range"] = true_range
        group = df.groupby("symbol", group_keys=False, sort=False)
        df["atr_5"] = group["true_range"].transform(
            lambda s: s.rolling(5, min_periods=5).mean()
        )
        df["atr_20"] = group["true_range"].transform(
            lambda s: s.rolling(self.atr_period, min_periods=self.atr_period).mean()
        )

        # 下跌背景：60日回撤、此前20日收益和距离60日低点的ATR倍数。
        df["rolling_high_60"] = group["close"].transform(
            lambda s: s.rolling(
                self.drawdown_period,
                min_periods=max(40, self.drawdown_period // 2),
            ).max()
        )
        df["rolling_low_60"] = group["low"].transform(
            lambda s: s.rolling(
                self.drawdown_period,
                min_periods=max(40, self.drawdown_period // 2),
            ).min()
        )
        df["close_shift_5"] = group["close"].shift(5)
        df["close_shift_20"] = group["close"].shift(self.trend_period)
        df["close_shift_25"] = group["close"].shift(self.trend_period + 5)
        df["return_20"] = df["close"] / df["close_shift_20"] - 1.0
        df["prior_return_20"] = (
            df["close_shift_5"] / df["close_shift_25"] - 1.0
        )
        df["drawdown_60"] = df["close"] / df["rolling_high_60"] - 1.0
        df["near_low_atr"] = (
            (df["close"] - df["rolling_low_60"]) / df["atr_20"]
        )

        # 价格结构：最近3日低点相对“此前10日低点”的ATR距离。
        df["recent_low_3"] = group["low"].transform(
            lambda s: s.rolling(3, min_periods=3).min()
        )
        df["previous_low_10"] = group["low"].transform(
            lambda s: s.shift(3).rolling(10, min_periods=8).min()
        )
        df["low_hold"] = (
            (df["recent_low_3"] - df["previous_low_10"]) / df["atr_20"]
        )

        # 下跌减速：比较互不重叠的窗口。
        # 最近5日收益减去此前20日平均下跌速度，避免同一段价格重复计分。
        valid_recent_5 = (df["close"] > 0.0) & (df["close_shift_5"] > 0.0)
        valid_prior_20 = (
            (df["close_shift_5"] > 0.0) & (df["close_shift_25"] > 0.0)
        )
        log_return_5 = pd.Series(np.nan, index=df.index, dtype=float)
        prior_log_return_20 = pd.Series(np.nan, index=df.index, dtype=float)
        log_return_5.loc[valid_recent_5] = np.log(
            df.loc[valid_recent_5, "close"]
            / df.loc[valid_recent_5, "close_shift_5"]
        )
        prior_log_return_20.loc[valid_prior_20] = np.log(
            df.loc[valid_prior_20, "close_shift_5"]
            / df.loc[valid_prior_20, "close_shift_25"]
        )
        df["deceleration"] = log_return_5 - 0.25 * prior_log_return_20

        # K线形态不逐一打分，统一为收盘位置 CLV。
        daily_range = (df["high"] - df["low"]).replace(0.0, np.nan)
        df["clv"] = (
            (2.0 * df["close"] - df["high"] - df["low"]) / daily_range
        ).clip(-1.0, 1.0)
        df["clv"] = df["clv"].fillna(0.0)
        group = df.groupby("symbol", group_keys=False, sort=False)
        df["clv_3"] = group["clv"].transform(
            lambda s: s.rolling(3, min_periods=2).mean()
        )

        # 抛压衰竭：最近5日下跌日的活跃度，和此前15日下跌日比较。
        if liquidity_col is not None:
            liquidity = df[liquidity_col].clip(lower=0.0)
            df["_down_flag"] = (df["close"] < df["prev_close"]).astype(float)
            df["_down_liquidity"] = liquidity * df["_down_flag"]
            group = df.groupby("symbol", group_keys=False, sort=False)

            recent_down_sum = group["_down_liquidity"].transform(
                lambda s: s.rolling(5, min_periods=3).sum()
            )
            recent_down_count = group["_down_flag"].transform(
                lambda s: s.rolling(5, min_periods=3).sum()
            )
            previous_down_sum = group["_down_liquidity"].transform(
                lambda s: s.shift(5).rolling(15, min_periods=10).sum()
            )
            previous_down_count = group["_down_flag"].transform(
                lambda s: s.shift(5).rolling(15, min_periods=10).sum()
            )

            recent_down_avg = recent_down_sum / recent_down_count.clip(lower=1.0)
            previous_down_avg = previous_down_sum / previous_down_count.where(
                previous_down_count >= 2.0
            )
            positive_prior = previous_down_avg[previous_down_avg > 0.0]
            epsilon = (
                float(positive_prior.median()) * 1e-6
                if not positive_prior.empty
                else 1e-12
            )
            df["sell_dry"] = -np.log(
                (recent_down_avg + epsilon) / (previous_down_avg + epsilon)
            )
            df["sell_dry"] = df["sell_dry"].replace(
                [np.inf, -np.inf], np.nan
            ).clip(-3.0, 3.0)
        else:
            df["sell_dry"] = np.nan

        # 波动率正常化：过去10日曾放大，当前ATR5/ATR20已回落。
        df["atr_ratio"] = df["atr_5"] / df["atr_20"]
        group = df.groupby("symbol", group_keys=False, sort=False)
        df["atr_ratio_peak_10"] = group["atr_ratio"].transform(
            lambda s: s.rolling(10, min_periods=6).max()
        )
        df["vol_normalization"] = df["atr_ratio_peak_10"] - df["atr_ratio"]

        # 相对强度：个股日收益减去全市场中位数日收益，再累计5日。
        df["market_return_1"] = df.groupby("date")["return_1"].transform("median")
        df["excess_return_1"] = df["return_1"] - df["market_return_1"]
        group = df.groupby("symbol", group_keys=False, sort=False)
        df["relative_strength_5"] = group["excess_return_1"].transform(
            lambda s: s.rolling(5, min_periods=4).sum()
        )

        # 短期突破：今日收盘价突破此前5日最高收盘价。
        df["previous_close_high"] = group["close"].transform(
            lambda s: s.shift(1).rolling(
                self.breakout_period,
                min_periods=self.breakout_period,
            ).max()
        )
        df["breakout_strength"] = (
            (df["close"] - df["previous_close_high"]) / df["atr_20"]
        )
        df["breakout"] = df["close"] > df["previous_close_high"]

        # 市场宽度用于调整分数线，不与个股因子重复打分。
        df["ma_20"] = group["close"].transform(
            lambda s: s.rolling(20, min_periods=20).mean()
        )
        df["above_ma_20"] = np.where(
            df["ma_20"].notna(),
            (df["close"] > df["ma_20"]).astype(float),
            np.nan,
        )

        # 通用的一字跌停近似过滤。精确涨跌停仍应由系统的板块/日期规则处理。
        price_spread_ratio = (df["high"] - df["low"]).abs() / df["close"]
        df["one_price_limit_down"] = (
            (price_spread_ratio <= 1e-6) & (df["return_1"] <= -0.08)
        )

        return df

    def _score_candidates(self, candidates: pd.DataFrame) -> pd.DataFrame:
        """对候选股进行横截面因子排名，并自动忽略缺失的因子。"""
        candidates = candidates.copy()

        factor_weights = {
            "low_hold": 0.25,
            "deceleration": 0.15,
            "sell_dry": 0.15,
            "clv_3": 0.10,
            "vol_normalization": 0.10,
            "relative_strength_5": 0.15,
            "breakout_strength": 0.10,
        }

        weighted_score = pd.Series(0.0, index=candidates.index)
        active_weight = 0.0

        for factor, weight in factor_weights.items():
            if factor not in candidates.columns:
                continue

            # 至少一半候选有有效值，才让该因子参与当日评分。
            valid_ratio = float(candidates[factor].notna().mean())
            if valid_ratio < 0.50:
                continue

            factor_score = self._percentile_score(candidates[factor])
            candidates[f"{factor}_score"] = factor_score
            weighted_score += factor_score * weight
            active_weight += weight

        if active_weight <= 0.0:
            candidates["stop_score"] = 0.0
        else:
            candidates["stop_score"] = weighted_score / active_weight

        confirmation_signals: list[pd.Series] = [
            candidates["low_hold"] >= 0.0,
            candidates["deceleration"] > 0.0,
            candidates["clv_3"] > 0.0,
            (
                (candidates["vol_normalization"] > 0.0)
                & (candidates["atr_ratio_peak_10"] > 1.05)
            ),
            candidates["relative_strength_5"] > 0.0,
            candidates["breakout"],
        ]

        if candidates["sell_dry"].notna().mean() >= 0.50:
            confirmation_signals.append(candidates["sell_dry"] > 0.0)

        candidates["confirmation_count"] = sum(
            signal.astype(int) for signal in confirmation_signals
        )

        # 横截面样本少于10只时，百分位分数没有足够区分度，
        # 改用确认信号覆盖率形成绝对分数，避免唯一候选固定只有50分。
        if len(candidates) < 10:
            max_confirmation = max(len(confirmation_signals), 1)
            candidates["stop_score"] = 50.0 + 50.0 * (
                candidates["confirmation_count"] / max_confirmation
            )

        return candidates

    @staticmethod
    def _percentile_score(series: pd.Series) -> pd.Series:
        """2%/98%缩尾后转成0-100横截面百分位，缺失值给中性50分。"""
        valid = series.dropna()
        if len(valid) < 2 or valid.nunique() < 2:
            return pd.Series(50.0, index=series.index)

        lower = float(valid.quantile(0.02))
        upper = float(valid.quantile(0.98))
        clipped = series.clip(lower=lower, upper=upper)
        return (clipped.rank(pct=True, method="average") * 100.0).fillna(50.0)

    def _get_market_regime(
        self, feature_df: pd.DataFrame, latest_date: pd.Timestamp
    ) -> tuple[float, float, str]:
        breadth_series = (
            feature_df.groupby("date")["above_ma_20"].mean().sort_index()
        )

        if latest_date not in breadth_series.index:
            return float("nan"), float("nan"), "neutral"

        breadth = float(breadth_series.loc[latest_date])
        breadth_change = (
            float(breadth_series.diff(5).loc[latest_date])
            if len(breadth_series) >= 6
            else float("nan")
        )

        if (
            breadth < self.weak_breadth_threshold
            and (not np.isfinite(breadth_change) or breadth_change <= 0.0)
        ):
            regime = "weak"
        elif (
            breadth >= self.improving_breadth_threshold
            or (
                np.isfinite(breadth_change)
                and breadth_change >= self.improving_breadth_change
            )
        ):
            regime = "improving"
        else:
            regime = "neutral"

        return breadth, breadth_change, regime

    def _score_floor_for_regime(self, regime: str) -> float:
        if regime == "weak":
            return self.weak_market_score_floor
        if regime == "improving":
            return self.improving_market_score_floor
        return self.neutral_market_score_floor

"""Sequoia-X V2 主程序入口。

两种运行模式：
  python main.py               # 日常模式：4进程增量补数据 + 跑策略 + 飞书推送
  python main.py --backfill    # 回填模式：单线程保守拉全市场历史K线（较慢，取决于限速）
"""

import argparse
import os
import signal
import socket
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed

from dotenv import load_dotenv

from sequoia_x.core.config import Settings, get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.notify.feishu import FeishuNotifier
from sequoia_x.notify.report import ReportGenerator
from sequoia_x.strategy.base import BaseStrategy
from sequoia_x.strategy.bowl_rebound import BowlReboundStrategy
from sequoia_x.strategy.FirstNewHigh30DaysBreakoutStrategy import FirstNewHigh30DaysBreakoutStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.RPS_YILIN import RpsYilinStrategy
from sequoia_x.strategy.ThreeTimeYilinStrategy_v3 import TripleVolumeBreakoutStrategy
from sequoia_x.strategy.trend_mean_reversion import TrendMeanReversionStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.stop_fall import StopFallStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

HIGH_VALUE_COMBOS: tuple[tuple[str, ...], ...] = (
    ("RpsYilinStrategy", "FirstNewHigh30DaysBreakoutStrategy"),
    ("RpsYilinStrategy", "TurtleTradeStrategy"),
    ("RpsYilinStrategy", "TripleVolumeBreakoutStrategy"),
    ("TurtleTradeStrategy", "FirstNewHigh30DaysBreakoutStrategy"),
    ("TripleVolumeBreakoutStrategy", "FirstNewHigh30DaysBreakoutStrategy"),
)

MEDIUM_VALUE_COMBOS: tuple[tuple[str, ...], ...] = (
    ("BowlReboundStrategy", "FirstNewHigh30DaysBreakoutStrategy"),
    ("TurtleTradeStrategy", "TripleVolumeBreakoutStrategy"),
    ("BowlReboundStrategy", "TripleVolumeBreakoutStrategy"),
    ("HighTightFlagStrategy", "RpsYilinStrategy"),
    ("MaVolumeStrategy", "RpsYilinStrategy"),
    ("MaVolumeStrategy", "RpsBreakoutStrategy"),
)


def _combo_label(combo: tuple[str, ...]) -> str:
    return " + ".join(combo)


def _build_resonance_hits(
    strategy_results: dict[str, list[str]],
) -> dict[str, list[dict[str, object]]]:
    """按策略组合分组构建高/中吸引力共振股票列表。"""
    symbol_to_strategies: dict[str, set[str]] = {}
    for strategy_name, symbols in strategy_results.items():
        for symbol in symbols:
            symbol_to_strategies.setdefault(symbol, set()).add(strategy_name)

    high_combos_hits: dict[tuple[str, ...], list[str]] = {}
    medium_combos_hits: dict[tuple[str, ...], list[str]] = {}

    for symbol, hit_strategies in symbol_to_strategies.items():
        for combo in HIGH_VALUE_COMBOS:
            if set(combo).issubset(hit_strategies):
                high_combos_hits.setdefault(combo, []).append(symbol)
        for combo in MEDIUM_VALUE_COMBOS:
            if set(combo).issubset(hit_strategies):
                medium_combos_hits.setdefault(combo, []).append(symbol)

    def _build_group(
        combos_hits: dict[tuple[str, ...], list[str]],
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for combo, symbols in combos_hits.items():
            result.append({
                "combo_label": _combo_label(combo),
                "symbols": sorted(symbols),
                "count": len(symbols),
            })
        result.sort(key=lambda x: x["count"], reverse=True)
        return result

    return {
        "high": _build_group(high_combos_hits),
        "medium": _build_group(medium_combos_hits),
    }


def _run_strategy_worker(
    strategy_cls: type[BaseStrategy],
    db_path: str,
    start_date: str,
) -> tuple[str, list[str], str | None]:
    """ProcessPool worker: instantiate engine+strategy and run, returning (name, selected, error_or_none)."""
    from sequoia_x.core.config import get_settings as _gs

    settings = _gs()
    engine = DataEngine(settings)
    instance = strategy_cls(engine=engine, settings=settings)
    name = type(instance).__name__
    try:
        selected = instance.run()
        return (name, selected, None)
    except Exception as exc:
        return (name, [], str(exc))


def main() -> None:
    load_dotenv()
    socket.setdefaulttimeout(10.0)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)

    parser = argparse.ArgumentParser(description="Sequoia-X V2 选股系统")
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="回填模式：单线程保守拉取全市场历史 K 线（较慢，取决于限速）",
    )
    parser.add_argument(
        "--parallel",
        action="store_true",
        help="并行模式：使用进程池并行执行策略（默认串行）",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=os.cpu_count() or 4,
        help="并行模式下的最大工作进程数（默认 CPU 核数）",
    )
    args = parser.parse_args()

    try:
        # 1. 初始化配置
        settings = get_settings()

        # 2. 初始化日志
        logger = get_logger(__name__)
        logger.info("Sequoia-X V2 启动")

        # 3. 初始化数据引擎
        engine = DataEngine(settings)

        if args.backfill:
            # ── 回填模式：单线程保守拉历史 K 线，自动多轮重跑 ──
            logger.info("进入回填模式...")
            all_symbols = engine.get_all_symbols()
            engine.backfill(all_symbols)
            logger.info("Sequoia-X V2 回填模式运行完成")
            return

        # ── 日常模式：单次 API 补今天 + 策略 + 推送 ──
        logger.info("开始拉取最新快照...")
        count = engine.sync_today_bulk()
        logger.info(f"快照同步完成，写入 {count} 只股票")

        # 4. 策略列表（新增策略在此追加即可）
        strategy_classes: list[type[BaseStrategy]] = [
            MaVolumeStrategy,
            TurtleTradeStrategy,
            HighTightFlagStrategy,
            LimitUpShakeoutStrategy,
            UptrendLimitDownStrategy,
            RpsBreakoutStrategy,
            RpsYilinStrategy,
            TripleVolumeBreakoutStrategy,
            BowlReboundStrategy,
            PrivatePlacementStrategy,
            FirstNewHigh30DaysBreakoutStrategy,
            TrendMeanReversionStrategy,
            StopFallStrategy,
        ]

        strategy_results: dict[str, list[str]] = {}

        if args.parallel:
            # ── 并行模式 ──
            logger.info(
                "策略执行（并行，max_workers=%d）...", args.max_workers,
            )
            strategy_start = time.perf_counter()
            with ProcessPoolExecutor(max_workers=args.max_workers) as executor:
                futures = {
                    executor.submit(
                        _run_strategy_worker,
                        cls,
                        settings.db_path,
                        settings.start_date,
                    ): cls.__name__
                    for cls in strategy_classes
                }
                for future in as_completed(futures):
                    cls_name = futures[future]
                    try:
                        name, selected, error = future.result()
                    except Exception as exc:
                        logger.warning(f"[{cls_name}] 进程异常：{exc}")
                        continue
                    if error:
                        logger.warning(f"[{name}] 执行失败：{error}")
                    else:
                        strategy_results[name] = selected
                        logger.info(f"{name} 选出 {len(selected)} 只股票")
            strategy_total = time.perf_counter() - strategy_start
            logger.info(f"── 策略全部完成（并行），总耗时 {strategy_total:.1f}s ──")
        else:
            # ── 串行模式 ──
            strategies = [cls(engine=engine, settings=settings) for cls in strategy_classes]
            logger.info("── 策略执行开始（串行）──")
            strategy_start = time.perf_counter()
            for strategy in strategies:
                strategy_name = type(strategy).__name__
                t0 = time.perf_counter()
                logger.info(f"执行策略：{strategy_name}")
                try:
                    selected: list[str] = strategy.run()
                except Exception:
                    logger.exception(f"{strategy_name} 执行失败，跳过该策略")
                    continue
                elapsed = time.perf_counter() - t0
                logger.info(f"{strategy_name} 选出 {len(selected)} 只股票，耗时 {elapsed:.1f}s")
                strategy_results[strategy_name] = selected
            strategy_total = time.perf_counter() - strategy_start
            logger.info(f"── 策略全部完成（串行），总耗时 {strategy_total:.1f}s ──")

        # 5. 推送各策略结果到飞书
        notifier = FeishuNotifier(settings)
        # 构建策略名→webhook_key 的映射（类属性）
        webhook_map: dict[str, str] = {
            cls.__name__: getattr(cls, "webhook_key", "default")
            for cls in strategy_classes
        }
        for name, symbols in strategy_results.items():
            webhook_key = webhook_map.get(name, "default")
            if symbols:
                try:
                    notifier.send(
                        symbols=symbols,
                        strategy_name=name,
                        webhook_key=webhook_key,
                    )
                except Exception:
                    logger.exception(f"{name} 飞书推送失败，继续")
            else:
                logger.info(f"{name} 无选股结果，跳过推送")

        # 6. 所有策略完成后，推送高/中吸引力策略共振股票
        resonance_hits = _build_resonance_hits(strategy_results)
        if resonance_hits["high"] or resonance_hits["medium"]:
            try:
                notifier.send_resonance(
                    hits=resonance_hits,
                    strategy_name="StrategyResonance",
                    webhook_key="strategy_resonance",
                )
            except Exception:
                logger.exception("StrategyResonance 飞书推送失败")
        else:
            logger.info("StrategyResonance 无共振股票，跳过推送")

        # 7. 生成 Markdown 选股报告
        try:
            report_gen = ReportGenerator(settings)
            report_path = report_gen.generate(strategy_results, resonance_hits)
            logger.info(f"选股报告已生成：{report_path}")
        except Exception:
            logger.exception("Markdown 报告生成失败")

    except Exception:
        try:
            _logger = get_logger(__name__)
            _logger.exception("主流程发生未捕获异常，程序终止")
        except Exception:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    logger.info("Sequoia-X V2 运行完成")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""基线测量脚本：运行所有策略并记录各自耗时（跳过数据同步、飞书推送、报告生成）。"""

import time
import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

from sequoia_x.core.config import get_settings
from sequoia_x.core.logger import get_logger
from sequoia_x.data.engine import DataEngine
from sequoia_x.strategy.bowl_rebound import BowlReboundStrategy
from sequoia_x.strategy.FirstNewHigh30DaysBreakoutStrategy import FirstNewHigh30DaysBreakoutStrategy
from sequoia_x.strategy.high_tight_flag import HighTightFlagStrategy
from sequoia_x.strategy.limit_up_shakeout import LimitUpShakeoutStrategy
from sequoia_x.strategy.ma_volume import MaVolumeStrategy
from sequoia_x.strategy.private_placement import PrivatePlacementStrategy
from sequoia_x.strategy.rps_breakout import RpsBreakoutStrategy
from sequoia_x.strategy.RPS_YILIN import RpsYilinStrategy
from sequoia_x.strategy.stop_fall import StopFallStrategy
from sequoia_x.strategy.ThreeTimeYilinStrategy_v3 import TripleVolumeBreakoutStrategy
from sequoia_x.strategy.trend_mean_reversion import TrendMeanReversionStrategy
from sequoia_x.strategy.turtle_trade import TurtleTradeStrategy
from sequoia_x.strategy.uptrend_limit_down import UptrendLimitDownStrategy

logger = get_logger(__name__)

STRATEGIES = [
    ("MaVolumeStrategy", MaVolumeStrategy),
    ("TurtleTradeStrategy", TurtleTradeStrategy),
    ("HighTightFlagStrategy", HighTightFlagStrategy),
    ("LimitUpShakeoutStrategy", LimitUpShakeoutStrategy),
    ("UptrendLimitDownStrategy", UptrendLimitDownStrategy),
    ("RpsBreakoutStrategy", RpsBreakoutStrategy),
    ("RpsYilinStrategy", RpsYilinStrategy),
    ("TripleVolumeBreakoutStrategy", TripleVolumeBreakoutStrategy),
    ("BowlReboundStrategy", BowlReboundStrategy),
    ("PrivatePlacementStrategy", PrivatePlacementStrategy),
    ("FirstNewHigh30DaysBreakoutStrategy", FirstNewHigh30DaysBreakoutStrategy),
    ("TrendMeanReversionStrategy", TrendMeanReversionStrategy),
    ("StopFallStrategy", StopFallStrategy),
]


def main():
    settings = get_settings()
    engine = DataEngine(settings)
    logger.info(f"DB: {settings.db_path}, symbols: {len(engine.get_local_symbols())}")

    timings = {}
    total_start = time.perf_counter()

    for name, cls in STRATEGIES:
        try:
            instance = cls(engine=engine, settings=settings)
            start = time.perf_counter()
            selected = instance.run()
            elapsed = time.perf_counter() - start
            timings[name] = elapsed
            logger.info(f"  {name}: {len(selected)} stocks in {elapsed:.2f}s")
        except Exception as exc:
            timings[name] = -1.0
            logger.warning(f"  {name}: FAILED ({exc})")

    total_elapsed = time.perf_counter() - total_start

    print("\n=== BASELINE TIMINGS ===")
    for name, elapsed in sorted(timings.items(), key=lambda x: x[1], reverse=True):
        status = f"{elapsed:.2f}s" if elapsed >= 0 else "FAILED"
        print(f"  {name:50s} {status:>10s}")
    print(f"  {'TOTAL':50s} {total_elapsed:>10.2f}s")


if __name__ == "__main__":
    main()

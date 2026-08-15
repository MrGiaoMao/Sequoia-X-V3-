"""数据引擎属性测试。"""

import sqlite3
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.data.engine import DataEngine


def make_engine_in(tmp_dir: str) -> tuple[DataEngine, Settings]:
    """创建使用临时数据库的 DataEngine 实例。"""
    settings = Settings(
        db_path=str(Path(tmp_dir) / "test.db"),
        start_date="2024-01-01",
        feishu_webhook_url="https://example.com/hook",
    )
    engine = DataEngine(settings)
    return engine, settings


def _seed_test_data(engine: DataEngine, symbols: list[str], dates: list[str]) -> None:
    """向 engine 的数据库写入多只股票、多天的模拟日线数据。"""
    rows = []
    for sym in symbols:
        for i, d in enumerate(dates):
            rows.append({
                "symbol": sym,
                "date": d,
                "open": 10.0 + i,
                "high": 11.0 + i,
                "low": 9.0 + i,
                "close": 10.5 + i,
                "volume": 1000.0 + i * 100,
                "turnover": 10500.0 + i * 1000,
            })
    with sqlite3.connect(engine.db_path) as conn:
        pd.DataFrame(rows).to_sql(
            "stock_daily", conn, if_exists="append",
            index=False, method="multi",
        )
        conn.commit()


# Property 4: (symbol, date) 唯一约束防止重复写入
@given(
    symbol=st.text(min_size=6, max_size=6, alphabet="0123456789"),
    trade_date=st.dates(min_value=date(2024, 1, 1), max_value=date(2025, 12, 31)),
)
@h_settings(max_examples=50, deadline=None)
def test_unique_symbol_date_constraint(symbol: str, trade_date: date) -> None:
    """相同 (symbol, date) 插入两次，数据库中该组合记录数应保持为 1。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        row = {
            "symbol": symbol, "date": str(trade_date),
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 1000.0, "turnover": 10500.0,
        }
        df = pd.DataFrame([row])
        with sqlite3.connect(engine.db_path) as conn:
            df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            try:
                df.to_sql("stock_daily", conn, if_exists="append", index=False, method="multi")
            except sqlite3.IntegrityError:
                pass
            count = conn.execute(
                "SELECT COUNT(*) FROM stock_daily WHERE symbol=? AND date=?",
                (symbol, str(trade_date)),
            ).fetchone()[0]
        assert count == 1


# ── 缓存测试 ──

def test_load_all_ohlcv_returns_grouped_data() -> None:
    """load_all_ohlcv 按 symbol 分组，且每只股票行数正确。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_test_data(engine, ["000001", "600519"], ["2024-01-01", "2024-01-02"])
        result = engine.load_all_ohlcv()
        assert set(result.keys()) == {"000001", "600519"}
        assert len(result["000001"]) == 2
        assert len(result["600519"]) == 2
        assert list(result["000001"]["date"]) == ["2024-01-01", "2024-01-02"]


def test_get_ohlcv_uses_cache_after_first_call() -> None:
    """首次 get_ohlcv 触发缓存填充，后续调用命中缓存（不查库）。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_test_data(engine, ["000001"], ["2024-01-01"])
        # 首次调用 — 填充缓存
        df1 = engine.get_ohlcv("000001")
        assert len(df1) == 1
        assert engine._ohlcv_cache is not None
        # 二次调用 — 命中缓存
        df2 = engine.get_ohlcv("000001")
        assert len(df2) == 1
        # 返回的是副本，互不影响
        df2.loc[0, "close"] = 999.0
        assert float(df1.loc[0, "close"]) == 10.5


def test_invalidate_cache_drops_cache() -> None:
    """invalidate_cache 清空缓存后，下次 get_ohlcv 重新查询。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        _seed_test_data(engine, ["000001"], ["2024-01-01"])
        engine.get_ohlcv("000001")
        assert engine._ohlcv_cache is not None
        engine.invalidate_cache()
        assert engine._ohlcv_cache is None
        # 清空后仍能正常读取
        df = engine.get_ohlcv("000001")
        assert len(df) == 1


def test_cache_covers_all_symbols() -> None:
    """缓存填充后应覆盖所有已入库股票。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        symbols = [f"{i:06d}" for i in range(1, 21)]  # 000001 ~ 000020
        _seed_test_data(engine, symbols, ["2024-01-01"])
        # 通过 _ensure_cache 触发
        engine._ensure_cache()
        assert engine._ohlcv_cache is not None
        assert set(engine._ohlcv_cache.keys()) == set(symbols)


def test_wal_mode_enabled() -> None:
    """数据库应启用 WAL 日志模式。"""
    with tempfile.TemporaryDirectory() as tmp_dir:
        engine, _ = make_engine_in(tmp_dir)
        with sqlite3.connect(engine.db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.upper() == "WAL"

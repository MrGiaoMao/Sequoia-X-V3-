"""数据引擎模块：负责 SQLite 行情数据存储与 baostock 增量同步。"""

import sqlite3
from pathlib import Path

import pandas as pd

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)

_MAX_BAOSTOCK_WORKERS = 4


def _init_worker_logging() -> None:
    """Initialize process-safe logging for multiprocessing workers.

    Strips RichHandler (which writes to stderr with rich formatting and
    can cause EPIPE when multiple child processes share inherited stderr
    fds) and replaces it with a plain StreamHandler.
    """
    import logging
    import sys

    target = logging.getLogger("sequoia_x.data.engine")
    for h in list(target.handlers):
        target.removeHandler(h)
    target.setLevel(logging.WARNING)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(name)s - %(message)s"))
    target.addHandler(handler)


def _sleep_with_jitter(base_seconds: float, jitter_seconds: float = 0.0) -> None:
    """Pause between baostock requests to avoid bursty traffic."""
    if base_seconds <= 0 and jitter_seconds <= 0:
        return

    import random
    import time

    wait_seconds = max(0.0, base_seconds)
    if jitter_seconds > 0:
        wait_seconds += random.uniform(0.0, jitter_seconds)

    if wait_seconds > 0:
        time.sleep(wait_seconds)


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS stock_daily (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol   TEXT    NOT NULL,
    date     TEXT    NOT NULL,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    turnover REAL,
    UNIQUE (symbol, date)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_symbol_date ON stock_daily (symbol, date);
"""


def _bs_fetch_batch(args: tuple[list, float, float, float]) -> list:
    """多进程 worker：独立 login，批量拉取 baostock 数据。"""
    import baostock as bs

    (
        tasks,
        request_delay_seconds,
        request_jitter_seconds,
        error_cooldown_seconds,
    ) = args

    def _login_worker() -> bool:
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return False
        return True

    def _relogin_worker(symbol: str, reason: str, attempt: int) -> bool:
        wait = 2 ** (attempt + 1)
        logger.warning(
            f"[{symbol}] 第{attempt + 1}次失败: {reason}，"
            f"{wait:.0f}s 后重新登录并重试"
        )
        try:
            bs.logout()
        except Exception:
            pass
        _sleep_with_jitter(float(wait), request_jitter_seconds)
        return _login_worker()

    if not _login_worker():
        return []

    results = []
    _sleep_with_jitter(0.0, request_jitter_seconds)

    try:
        for symbol, bs_code, start, end in tasks:
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    rs = bs.query_history_k_data_plus(
                        bs_code,
                        "date,open,high,low,close,volume,amount",
                        start_date=start,
                        end_date=end,
                        frequency="d",
                        adjustflag="1",  # 后复权
                    )
                    if rs.error_code != "0":
                        raise RuntimeError(rs.error_msg)

                    while rs.next():
                        results.append([symbol] + rs.get_row_data())
                    break
                except Exception as exc:
                    if attempt < max_attempts - 1 and _relogin_worker(
                        symbol, str(exc), attempt,
                    ):
                        continue
                    logger.warning(
                        f"[{symbol}] {max_attempts}次重试均失败，跳过: {exc}"
                    )
                    _sleep_with_jitter(error_cooldown_seconds, request_jitter_seconds)
                    break

            _sleep_with_jitter(request_delay_seconds, request_jitter_seconds)
    finally:
        try:
            bs.logout()
        except OSError:
            pass

    return results


class DataEngine:
    """行情数据引擎，负责 SQLite 存储和 baostock 数据同步。"""

    def __init__(self, settings: Settings) -> None:
        self.db_path: str = settings.db_path
        self.start_date: str = settings.start_date
        self.baostock_max_workers = max(
            1,
            min(settings.baostock_max_workers, _MAX_BAOSTOCK_WORKERS),
        )
        self.baostock_request_delay_seconds = max(
            0.0,
            settings.baostock_request_delay_seconds,
        )
        self.baostock_request_jitter_seconds = max(
            0.0,
            settings.baostock_request_jitter_seconds,
        )
        self.baostock_error_cooldown_seconds = max(
            0.0,
            settings.baostock_error_cooldown_seconds,
        )
        self._ohlcv_cache: dict[str, pd.DataFrame] | None = None
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA cache_size=-65536")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.commit()
        logger.info(f"数据库初始化完成：{self.db_path}")

    def _get_last_date(self, symbol: str) -> str | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT MAX(date) FROM stock_daily WHERE symbol = ?",
                (symbol,),
            ).fetchone()
        return row[0] if row and row[0] else None

    def load_all_ohlcv(self) -> dict[str, pd.DataFrame]:
        """一次性返回全市场日线数据（按 symbol 分组）。

        复用内存缓存；首次调用时从 SQLite 填充缓存。
        返回的 dict 引用缓存内的 DataFrame，调用方可原地修改（列追加等）。
        """
        self._ensure_cache()
        assert self._ohlcv_cache is not None
        return self._ohlcv_cache

    def _load_from_db(self) -> dict[str, pd.DataFrame]:
        """从 SQLite 一次性读取全表并按 symbol 分组。"""
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT symbol,date,open,high,low,close,volume,turnover "
                "FROM stock_daily ORDER BY symbol, date",
                conn,
            )
        if df.empty:
            return {}
        result: dict[str, pd.DataFrame] = {}
        for sym, grp in df.groupby("symbol", sort=False):
            result[str(sym)] = grp.reset_index(drop=True)
        return result

    def _ensure_cache(self) -> None:
        """惰性填充内存缓存（如已存在则跳过）。"""
        if self._ohlcv_cache is not None:
            return
        self._ohlcv_cache = self._load_from_db()
        logger.info(
            "OHLCV 缓存已就绪: %d 只股票",
            len(self._ohlcv_cache),
        )

    def invalidate_cache(self) -> None:
        """强制清空内存缓存，下次 get_ohlcv / load_all_ohlcv 将重新读取数据库。"""
        self._ohlcv_cache = None

    def get_ohlcv(self, symbol: str) -> pd.DataFrame:
        """读取单只股票日线数据（优先命中内存缓存，未命中则直接查库）。"""
        if self._ohlcv_cache is not None and symbol in self._ohlcv_cache:
            return self._ohlcv_cache[symbol].copy()
        self._ensure_cache()
        if self._ohlcv_cache is not None and symbol in self._ohlcv_cache:
            return self._ohlcv_cache[symbol].copy()
        with sqlite3.connect(self.db_path) as conn:
            df = pd.read_sql(
                "SELECT * FROM stock_daily WHERE symbol = ? ORDER BY date",
                conn,
                params=(symbol,),
            )
        return df

    @staticmethod
    def _to_baostock_code(symbol: str) -> str:
        """将纯数字代码转为 baostock 格式：6/9开头 -> sh，其余 -> sz。"""
        prefix = "sh" if symbol.startswith(("6", "9")) else "sz"
        return f"{prefix}.{symbol}"

    def pause_between_baostock_requests(self) -> None:
        """Apply the configured baostock request pacing."""
        _sleep_with_jitter(
            self.baostock_request_delay_seconds,
            self.baostock_request_jitter_seconds,
        )

    # ── 数据同步 ──

    def sync_today_bulk(self) -> int:
        """多进程并行通过 baostock 拉取增量数据（后复权），写入 SQLite。"""
        from datetime import date, timedelta
        from multiprocessing import Pool

        today_str = date.today().strftime("%Y-%m-%d")

        tasks = []
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT symbol, MAX(date) FROM stock_daily GROUP BY symbol"
            ).fetchall()

        if not rows:
            logger.warning("本地无股票数据，请先执行 --backfill")
            return 0

        for symbol, last_date in rows:
            if last_date and last_date >= today_str:
                continue
            start = today_str
            if last_date:
                start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            tasks.append((symbol, self._to_baostock_code(symbol), start, today_str))

        if not tasks:
            logger.info("所有股票已是最新，无需更新")
            return 0

        n_workers = min(self.baostock_max_workers, len(tasks))
        logger.info(
            f"需要更新 {len(tasks)} 只股票，启动 {n_workers} 进程并行拉取，"
            f"单进程请求间隔 {self.baostock_request_delay_seconds:.1f}s "
            f"+ 随机抖动 {self.baostock_request_jitter_seconds:.1f}s..."
        )

        chunks = [tasks[i::n_workers] for i in range(n_workers)]
        worker_args = [
            (
                chunk,
                self.baostock_request_delay_seconds,
                self.baostock_request_jitter_seconds,
                self.baostock_error_cooldown_seconds,
            )
            for chunk in chunks
        ]

        with Pool(n_workers, initializer=_init_worker_logging) as pool:
            batch_results = pool.map(_bs_fetch_batch, worker_args)

        all_rows = []
        for batch in batch_results:
            all_rows.extend(batch)

        if not all_rows:
            logger.info("无新数据（可能非交易日）")
            return 0

        df = pd.DataFrame(
            all_rows,
            columns=[
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "turnover",
            ],
        )
        for col in ["open", "high", "low", "close", "volume", "turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        df = df[df["volume"] > 0]

        count = len(df)
        with sqlite3.connect(self.db_path) as conn:
            # 安全 DELETE：只删除即将写入的具体 (symbol, date) 行，
            # 不影响同日期其他股票的数据，避免中途崩溃造成数据丢失
            pairs = df[["symbol", "date"]].drop_duplicates().values.tolist()
            conn.executemany(
                "DELETE FROM stock_daily WHERE symbol=? AND date=?",
                pairs,
            )
            df.to_sql(
                "stock_daily",
                conn,
                if_exists="append",
                index=False,
                method="multi",
                chunksize=500,
            )
            conn.commit()

        self.invalidate_cache()
        logger.info(f"sync_today_bulk: 写入 {count} 条数据")
        return count

    def backfill(self, symbols: list[str]) -> None:
        """通过 baostock 批量回填历史日 K 线数据（后复权）。

        容错机制：
        - 单只股票失败自动重试 3 次，间隔递增（2s/4s/8s）
        - 每 200 只股票自动重连 baostock（防止长连接超时）
        - 已入库的自动 skip，中断后可重跑续传
        """
        import time
        from datetime import date, timedelta

        import baostock as bs

        today_str = date.today().strftime("%Y-%m-%d")
        max_retries = 3
        reconnect_interval = 200  # 每处理 N 只股票重连一次

        def _login():
            lg = bs.login()
            if lg.error_code != "0":
                logger.error(f"baostock 登录失败: {lg.error_msg}")
                return False
            return True

        if not _login():
            return

        success = 0
        skipped = 0
        failed = 0
        since_reconnect = 0

        try:
            for i, symbol in enumerate(symbols):
                last_date = self._get_last_date(symbol)
                if last_date and last_date >= today_str:
                    skipped += 1
                    if (i + 1) % 500 == 0:
                        logger.info(
                            f"已处理 {i + 1}/{len(symbols)}，"
                            f"成功 {success} 跳过 {skipped} 失败 {failed}"
                        )
                    continue

                # 定期重连，防止长连接超时
                since_reconnect += 1
                if since_reconnect >= reconnect_interval:
                    try:
                        bs.logout()
                    except OSError:
                        pass
                    time.sleep(1)
                    if not _login():
                        logger.error("重连失败，终止回填")
                        return
                    since_reconnect = 0

                start = last_date or self.start_date
                if last_date:
                    start = (date.fromisoformat(last_date) + timedelta(days=1)).strftime("%Y-%m-%d")

                bs_code = self._to_baostock_code(symbol)

                # 带重试的查询
                rows = []
                query_ok = False
                for attempt in range(max_retries):
                    try:
                        rs = bs.query_history_k_data_plus(
                            bs_code,
                            "date,open,high,low,close,volume,amount",
                            start_date=start,
                            end_date=today_str,
                            frequency="d",
                            adjustflag="1",  # 后复权
                        )

                        if rs.error_code != "0":
                            raise RuntimeError(rs.error_msg)

                        rows = []
                        while rs.next():
                            rows.append(rs.get_row_data())
                        query_ok = True
                        break

                    except Exception as exc:
                        if attempt < max_retries - 1:
                            wait = 2 ** (attempt + 1)
                            logger.warning(
                                f"[{symbol}] 第{attempt + 1}次失败: {exc}，{wait}s 后重试"
                            )
                            time.sleep(wait)
                            # 重连 baostock
                            try:
                                bs.logout()
                            except OSError:
                                pass
                            time.sleep(1)
                            _login()
                        else:
                            logger.warning(f"[{symbol}] {max_retries}次重试均失败，跳过")

                if not query_ok:
                    failed += 1
                    self.pause_between_baostock_requests()
                    continue

                if not rows:
                    skipped += 1
                    self.pause_between_baostock_requests()
                    continue

                df = pd.DataFrame(rows, columns=rs.fields)
                for col in ["open", "high", "low", "close", "volume", "amount"]:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["volume"] > 0]

                if df.empty:
                    skipped += 1
                    self.pause_between_baostock_requests()
                    continue

                df["symbol"] = symbol
                df = df.rename(columns={"amount": "turnover"})
                df = df[["symbol", "date", "open", "high", "low", "close", "volume", "turnover"]]

                try:
                    with sqlite3.connect(self.db_path) as conn:
                        df.to_sql(
                            "stock_daily", conn, if_exists="append",
                            index=False, method="multi", chunksize=500,
                        )
                except sqlite3.IntegrityError:
                    pass

                success += 1
                self.pause_between_baostock_requests()

                if (i + 1) % 500 == 0:
                    logger.info(
                        f"已处理 {i + 1}/{len(symbols)}，"
                        f"成功 {success} 跳过 {skipped} 失败 {failed}"
                    )

        finally:
            try:
                bs.logout()
            except OSError:
                pass

        self.invalidate_cache()
        logger.info(f"回填完成 — 成功: {success} | 跳过: {skipped} | 失败: {failed}")

    # ── 股票列表 ──

    def get_all_symbols(self) -> list[str]:
        """通过 baostock 获取全市场 A 股代码列表。"""
        import baostock as bs

        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"baostock 登录失败: {lg.error_msg}")
            return []

        try:
            rs = bs.query_stock_basic(code_name="", code="")
            symbols = []
            while rs.next():
                row = rs.get_row_data()
                code = row[0]           # "sh.600000" or "sz.000001"
                status = row[4]         # "1" = 上市
                stock_type = row[5]     # "1" = 股票
                if status == "1" and stock_type == "1":
                    symbols.append(code.split(".")[1])  # 提取纯数字代码
            logger.info(f"获取股票列表完成，共 {len(symbols)} 只")
            return symbols
        except Exception as e:
            logger.error(f"获取股票列表失败: {e}")
            return []
        finally:
            try:
                bs.logout()
            except OSError:
                pass

    def get_local_symbols(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM stock_daily"
            ).fetchall()
        return [row[0] for row in rows]

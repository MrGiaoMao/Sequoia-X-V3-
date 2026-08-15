"""Markdown 报告生成模块：将每日选股结果输出为 Markdown 文件。"""

from datetime import date
from pathlib import Path

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class ReportGenerator:
    """Markdown 选股报告生成器。

    将各策略选股结果及策略共振结果汇总成一份 Markdown 文件，
    保存到用户指定的输出目录（默认量化交易/今日选股结果）。

    Attributes:
        settings: Settings 实例，获取输出目录配置。
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stock_name_cache: dict[str, str] = {}

    # ── 输出目录 ──

    def _resolve_output_dir(self) -> Path:
        """解析报告输出目录，默认 Documents/量化交易/今日选股结果。"""
        configured = (self.settings.report_output_dir or "").strip()
        if configured:
            output_dir = Path(configured).expanduser().resolve()
        else:
            output_dir = Path.home() / "Documents" / "量化交易" / "今日选股结果"
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir

    # ── 股票名称查询 ──

    @staticmethod
    def _is_bj_code(code: str) -> bool:
        """北交所代码段：4/8 开头旧段，以及 2024 年启用的 92 新段。"""
        return code.startswith(("4", "8", "92"))

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8/92开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        if ReportGenerator._is_bj_code(code):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _tencent_code(code: str) -> str:
        """纯数字代码转腾讯行情代码：6开头→sh，4/8/92开头→bj，其余→sz。"""
        if code.startswith("6"):
            return f"sh{code}"
        if ReportGenerator._is_bj_code(code):
            return f"bj{code}"
        return f"sz{code}"

    def _fetch_tencent_names(self, codes: list[str]) -> None:
        """通过腾讯行情接口批量查询股票名称（沪深/北交所通用），结果写入缓存。

        用于 baostock 无北交所数据或 baostock 不可用时的兜底。
        """
        try:
            import requests

            for i in range(0, len(codes), 60):
                batch = codes[i:i + 60]
                q = ",".join(self._tencent_code(c) for c in batch)
                resp = requests.get(
                    f"https://qt.gtimg.cn/q={q}",
                    timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                resp.encoding = "gbk"
                for line in resp.text.split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    var_name, value = line.split("=", 1)
                    key = var_name.removeprefix("v_")  # 形如 sh600519 / bj920002
                    code = key[2:]
                    fields = value.strip('"').split("~")
                    if len(fields) > 1 and fields[1]:
                        self._stock_name_cache[code] = fields[1]
        except Exception as exc:
            logger.warning(
                f"腾讯行情名称查询异常，将使用代码作为名称：{exc}"
            )

    def _get_stock_names(self, symbols: list[str]) -> dict[str, str]:
        """查询股票名称，返回 {code: name} 映射。

        先查缓存，缺失的才查询：沪深股票优先走 baostock，baostock 失败或缺漏时
        用腾讯行情接口兜底；北交所股票（4/8/92 开头）直接走腾讯行情接口
        （baostock 无北交所数据）。单只失败不影响其他。
        """
        unique_symbols = list(dict.fromkeys(symbols))
        missing = [
            s for s in unique_symbols if s not in self._stock_name_cache
        ]

        if not missing:
            return {
                s: self._stock_name_cache[s]
                for s in unique_symbols
                if s in self._stock_name_cache
            }

        bj_missing = [c for c in missing if self._is_bj_code(c)]
        bs_missing = [c for c in missing if not self._is_bj_code(c)]

        # 北交所：baostock 无数据，直接走腾讯行情接口
        if bj_missing:
            self._fetch_tencent_names(bj_missing)

        # 沪深：优先 baostock，失败或缺漏时用腾讯行情接口兜底
        if bs_missing:
            try:
                import baostock as bs

                lg = bs.login()
                if lg.error_code != "0":
                    logger.warning(
                        f"baostock 登录失败，股票名称查询跳过：{lg.error_msg}"
                    )
                else:
                    try:
                        for code in bs_missing:
                            try:
                                prefix = "sh" if code.startswith(("6", "9")) else "sz"
                                rs = bs.query_stock_basic(code=f"{prefix}.{code}")
                                if rs.error_code != "0":
                                    logger.warning(
                                        f"[{code}] 股票名称查询失败：{rs.error_msg}"
                                    )
                                    continue
                                while rs.next():
                                    row = rs.get_row_data()
                                    # 第2个字段是股票名称
                                    self._stock_name_cache[code] = row[1]
                            except Exception:
                                logger.warning(f"[{code}] 股票名称查询异常，跳过")
                    finally:
                        try:
                            bs.logout()
                        except OSError:
                            pass
            except Exception as exc:
                logger.warning(
                    f"股票名称查询异常，将使用代码作为名称：{exc}"
                )

            # baostock 查询后仍缺漏的，用腾讯行情接口兜底
            still_missing = [
                c for c in bs_missing if c not in self._stock_name_cache
            ]
            if still_missing:
                self._fetch_tencent_names(still_missing)

        return {
            s: self._stock_name_cache.get(s, s) for s in unique_symbols
        }

    # ── Markdown 内容构建 ──

    def _build_markdown(
        self,
        strategy_results: dict[str, list[str]],
        resonance_hits: dict[str, list[dict[str, object]]],
    ) -> str:
        """组装完整的 Markdown 报告内容。"""
        today = date.today().strftime("%Y-%m-%d")
        lines: list[str] = []

        # ── 标题 ──
        lines.append(f"# 📈 Sequoia-X 选股报告 — {today}")
        lines.append("")

        # ── 概览 ──
        total_strategies = len(strategy_results)
        active_strategies = sum(
            1 for v in strategy_results.values() if v
        )
        all_symbols: set[str] = set()
        for symbols in strategy_results.values():
            all_symbols.update(symbols)

        # 共振的去重股票数
        resonance_symbols: set[str] = set()
        for groups in resonance_hits.values():
            for group in groups:
                for s in group["symbols"]:
                    resonance_symbols.add(str(s))

        lines.append("## 概览")
        lines.append("")
        lines.append(f"- **策略总数**：{total_strategies}")
        lines.append(f"- **有选股的策略**：{active_strategies}")
        lines.append(
            f"- **总选股数（去重）**：{len(all_symbols)}"
        )
        if resonance_symbols:
            lines.append(
                f"- **策略共振股票数（去重）**：{len(resonance_symbols)}"
            )
        lines.append("")

        if not all_symbols and not resonance_symbols:
            lines.append("> ⚠️ 今日无选股结果")
            lines.append("")
            return "\n".join(lines)

        # ── 收集所有股票代码，一次性查询名称 ──
        all_codes = sorted(all_symbols | resonance_symbols)
        names = self._get_stock_names(all_codes)

        # ── 各策略详情 ──
        lines.append("---")
        lines.append("")
        lines.append("## 各策略选股详情")
        lines.append("")

        # 按策略名排序，有结果优先
        sorted_strategies = sorted(
            strategy_results.items(),
            key=lambda item: (not item[1], item[0]),
        )
        for strategy_name, symbols in sorted_strategies:
            count = len(symbols)
            lines.append(f"### {strategy_name}（{count}只）")
            lines.append("")
            if not symbols:
                lines.append("（无选股结果）")
                lines.append("")
                continue

            lines.append(
                "| # | 代码 | 名称 | 雪球链接 |"
            )
            lines.append(
                "|---|------|------|----------|"
            )
            for i, code in enumerate(symbols, 1):
                name = names.get(code, code)
                xq_code = self._to_xueqiu_code(code)
                link = f"https://xueqiu.com/S/{xq_code}"
                lines.append(
                    f"| {i} | {code} | {name} "
                    f"| [{name}]({link}) |"
                )
            lines.append("")

        # ── 策略共振 ──
        if resonance_hits.get("high") or resonance_hits.get("medium"):
            lines.append("---")
            lines.append("")
            lines.append("## 🔥 策略共振")
            lines.append("")

            def _section(
                label: str,
                emoji: str,
                groups: list[dict[str, object]],
            ) -> None:
                if not groups:
                    return
                lines.append(f"### {emoji} {label}")
                lines.append("")
                for group in groups:
                    combo_label = str(group["combo_label"])
                    count = group["count"]
                    lines.append(
                        f"#### {combo_label}（{count}只）"
                    )
                    lines.append("")
                    lines.append(
                        "| # | 代码 | 名称 | 雪球链接 |"
                    )
                    lines.append(
                        "|---|------|------|----------|"
                    )
                    for i, code in enumerate(group["symbols"], 1):
                        code_str = str(code)
                        name = names.get(code_str, code_str)
                        xq_code = self._to_xueqiu_code(code_str)
                        link = (
                            f"https://xueqiu.com/S/{xq_code}"
                        )
                        lines.append(
                            f"| {i} | {code_str} | {name} "
                            f"| [{name}]({link}) |"
                        )
                    lines.append("")

            _section("高吸引力共振", "🔴", resonance_hits.get("high", []))
            _section("中吸引力共振", "🟡", resonance_hits.get("medium", []))

        # ── 页脚 ──
        lines.append("---")
        lines.append("")
        lines.append(
            f"*报告由 Sequoia-X 自动生成于 {today}*"
        )
        lines.append("")

        return "\n".join(lines)

    # ── 主入口 ──

    def generate(
        self,
        strategy_results: dict[str, list[str]],
        resonance_hits: dict[str, list[dict[str, object]]],
    ) -> Path:
        """生成 Markdown 报告并写入磁盘。

        Args:
            strategy_results: 策略名 → 选股代码列表。
            resonance_hits: 共振结果，结构同 main.py 中
                _build_resonance_hits 返回值。

        Returns:
            生成的文件路径。

        Raises:
            OSError: 文件写入失败时抛出（调用方应捕获）。
        """
        md_content = self._build_markdown(strategy_results, resonance_hits)
        output_dir = self._resolve_output_dir()
        today = date.today().strftime("%Y-%m-%d")
        filename = f"Sequoia-X_选股报告_{today}.md"
        filepath = output_dir / filename

        filepath.write_text(md_content, encoding="utf-8")
        logger.info(f"Markdown 报告已写入：{filepath}")
        return filepath

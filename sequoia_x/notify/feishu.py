"""飞书通知模块：将选股结果通过 Webhook 推送至飞书群。"""

import json
from datetime import date

import requests

from sequoia_x.core.config import Settings
from sequoia_x.core.logger import get_logger

logger = get_logger(__name__)


class FeishuNotifier:
    """飞书 Webhook 推送器。

    根据策略的 webhook_key 路由到对应的飞书机器人。
    若 webhook_key 未在 Settings.strategy_webhooks 中配置，
    则 fallback 到 Settings.feishu_webhook_url。
    """

    def __init__(self, settings: Settings) -> None:
        """
        初始化 FeishuNotifier。

        Args:
            settings: Settings 实例，提供 Webhook URL 配置。
        """
        self.settings = settings
        self._stock_name_cache: dict[str, str] = {}

    @staticmethod
    def _is_bj_code(code: str) -> bool:
        """北交所代码段：4/8 开头旧段，以及 2024 年启用的 92 新段。"""
        return code.startswith(("4", "8", "92"))

    @staticmethod
    def _to_xueqiu_code(code: str) -> str:
        """将纯数字代码转为雪球格式：6开头→SH，4/8/92开头→BJ，其余→SZ。"""
        if code.startswith("6"):
            return f"SH{code}"
        elif FeishuNotifier._is_bj_code(code):
            return f"BJ{code}"
        return f"SZ{code}"

    @staticmethod
    def _tencent_code(code: str) -> str:
        """纯数字代码转腾讯行情代码：6开头→sh，4/8/92开头→bj，其余→sz。"""
        if code.startswith("6"):
            return f"sh{code}"
        if FeishuNotifier._is_bj_code(code):
            return f"bj{code}"
        return f"sz{code}"

    def _fetch_tencent_names(self, codes: list[str]) -> None:
        """通过腾讯行情接口批量查询股票名称（沪深/北交所通用），结果写入缓存。

        用于 baostock 无北交所数据或 baostock 不可用时的兜底。
        """
        try:
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
        missing_symbols = [
            symbol for symbol in unique_symbols if symbol not in self._stock_name_cache
        ]

        if not missing_symbols:
            return {
                symbol: self._stock_name_cache[symbol]
                for symbol in unique_symbols
                if symbol in self._stock_name_cache
            }

        bj_missing = [c for c in missing_symbols if self._is_bj_code(c)]
        bs_missing = [c for c in missing_symbols if not self._is_bj_code(c)]

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
            symbol: self._stock_name_cache.get(symbol, symbol)
            for symbol in unique_symbols
        }

    def _build_xueqiu_links(self, symbols: list[str]) -> str:
        """构建雪球超链接文本，格式：[名称](雪球链接)。"""
        names = self._get_stock_names(symbols)
        links: list[str] = []
        for code in symbols:
            xq_code = self._to_xueqiu_code(code)
            name = names.get(code, code)
            links.append(f"[{name}](https://xueqiu.com/S/{xq_code})")
        return " ".join(links) if links else "（无选股结果）"

    def _post_payload(self, payload: dict, webhook_key: str) -> None:
        """POST 到飞书 Webhook，带重试和 BrokenPipe 容错。"""
        url = self.settings.get_webhook_url(webhook_key)
        max_retries = 3

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    url,
                    data=json.dumps(payload),
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )
                try:
                    resp_json = resp.json()
                except ValueError:
                    resp_json = {}

                # 飞书真正的成功标志是内部的 code == 0
                if resp.status_code != 200 or resp_json.get("code") != 0:
                    logger.error(
                        f"飞书推送失败 [{webhook_key}] "
                        f"HTTP状态={resp.status_code} 飞书响应={resp.text}"
                    )
                else:
                    logger.info(f"飞书推送成功 [{webhook_key}]")
                return  # success or known failure, don't retry

            except (requests.RequestException, OSError) as exc:
                if attempt < max_retries - 1:
                    import time
                    wait = 2 ** attempt
                    logger.warning(
                        f"飞书推送连接异常 [{webhook_key}]：{exc}，{wait}s 后重试 "
                        f"({attempt + 1}/{max_retries})"
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        f"飞书推送请求异常 [{webhook_key}]，"
                        f"已重试 {max_retries} 次：{exc}"
                    )
            except Exception as exc:
                logger.error(f"飞书推送异常 [{webhook_key}]：{exc}")
                return

    def _build_card(self, symbols: list[str], strategy_name: str) -> dict:
        today = date.today().strftime("%Y-%m-%d")
        symbol_text = self._build_xueqiu_links(symbols)

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"📈 Sequoia-X 选股播报 | {strategy_name}",
                    },
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": (
                                f"**日期：** {today}\n"
                                f"**策略：** {strategy_name}\n"
                                f"**选股数量：** {len(symbols)}"
                            ),
                        },
                    },
                    {"tag": "hr"},
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"**选股列表：**\n{symbol_text}",
                        },
                    },
                ],
            },
        }

    def _build_resonance_card(
        self,
        hits: dict[str, list[dict[str, object]]],
        strategy_name: str,
    ) -> dict:
        today = date.today().strftime("%Y-%m-%d")

        all_symbols: list[str] = []
        for groups in hits.values():
            for group in groups:
                all_symbols.extend(group["symbols"])
        names = self._get_stock_names(all_symbols)
        unique_total = len(set(all_symbols))

        def _build_section(
            label: str, emoji: str, groups: list[dict[str, object]],
        ) -> str:
            if not groups:
                return ""
            section_lines = [f"**{emoji} {label}**\n"]
            for group in groups:
                combo_label = str(group["combo_label"])
                count = group["count"]
                links = []
                for code in group["symbols"]:
                    xq_code = self._to_xueqiu_code(str(code))
                    name = names.get(str(code), str(code))
                    links.append(
                        f"[**{name}**](https://xueqiu.com/S/{xq_code})"
                    )
                section_lines.append(
                    f"**{combo_label}**（{count}只）\n"
                    f"{'  '.join(links)}"
                )
                section_lines.append("")
            return "\n".join(section_lines)

        high_section = _build_section(
            "高吸引力共振", "🔴", hits.get("high", []),
        )
        medium_section = _build_section(
            "中吸引力共振", "🟡", hits.get("medium", []),
        )

        content = (
            f"**日期：** {today}\n"
            f"**共振股票总数：** {unique_total}\n\n"
            f"---\n\n"
            f"{high_section}\n"
            f"{medium_section}"
        ).strip()

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"🔥 Sequoia-X 策略共振 | {strategy_name}",
                    },
                    "template": "red",
                },
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": content,
                        },
                    },
                ],
            },
        }

    def send(
        self,
        symbols: list[str],
        strategy_name: str,
        webhook_key: str = "default",
    ) -> None:
        """
        将选股结果格式化为飞书卡片消息并 POST 至对应 Webhook。

        根据 webhook_key 从 Settings 中查找专属 URL；
        若未配置，则 fallback 到 feishu_webhook_url。

        Args:
            symbols: 选股结果代码列表。
            strategy_name: 策略名称，用于卡片标题。
            webhook_key: 策略标识，用于路由到对应飞书机器人。

        Raises:
            不抛出异常，HTTP 失败时记录 ERROR 日志。
        """
        payload = self._build_card(symbols, strategy_name)
        self._post_payload(payload, webhook_key)
        logger.info(f"飞书卡片已生成 [{webhook_key}]，共 {len(symbols)} 只股票")

    def send_resonance(
        self,
        hits: dict[str, list[dict[str, object]]],
        strategy_name: str,
        webhook_key: str = "strategy_resonance",
    ) -> None:
        """推送策略共振股票，按组合分组展示。"""
        payload = self._build_resonance_card(hits, strategy_name)
        self._post_payload(payload, webhook_key)
        all_symbols: set[str] = set()
        for groups in hits.values():
            for group in groups:
                all_symbols.update(group["symbols"])
        logger.info(
            f"策略共振飞书卡片已生成 [{webhook_key}]，"
            f"共 {len(all_symbols)} 只股票"
        )

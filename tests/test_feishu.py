"""飞书通知属性测试。"""

import json
from unittest.mock import MagicMock, patch

from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from sequoia_x.core.config import Settings
from sequoia_x.notify.feishu import FeishuNotifier


def make_settings(webhook_url: str = "https://example.com/default") -> Settings:
    return Settings(
        db_path="data/test.db",
        start_date="2024-01-01",
        feishu_webhook_url=webhook_url,
    )


def _successful_response() -> MagicMock:
    response = MagicMock(status_code=200, text='{"code":0}')
    response.json.return_value = {"code": 0}
    return response


def _posted_card_text(mock_post: MagicMock) -> str:
    call_args = mock_post.call_args
    if len(call_args.args) > 1:
        body_data = call_args.kwargs.get("data") or call_args.args[1]
    else:
        body_data = call_args.kwargs["data"]
    return json.dumps(json.loads(body_data), ensure_ascii=False)


# Feature: sequoia-x-v2, Property 10: 飞书通知包含所有选股结果
@given(
    symbols=st.lists(
        st.text(min_size=6, max_size=6, alphabet="0123456789"),
        min_size=1,
        max_size=10,
        unique=True,
    )
)
@h_settings(max_examples=50)
def test_notification_contains_stock_names_not_codes(symbols: list[str]) -> None:
    """send() 发出的请求体应显示股票名称，不显示股票代码。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)
    stock_names = {symbol: f"股票{idx}" for idx, symbol in enumerate(symbols)}

    with patch.object(notifier, "_get_stock_names", return_value=stock_names):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _successful_response()
            notifier.send(symbols=symbols, strategy_name="TestStrategy")

    card_text = _posted_card_text(mock_post)
    for symbol, name in stock_names.items():
        assert name in card_text
        # 雪球链接格式：[名称](https://xueqiu.com/S/SH600000)
        assert "xueqiu.com/S/" in card_text


def test_bj_stock_names_and_links() -> None:
    """北交所股票（4/8/92 开头）应查询出名称，雪球链接使用 BJ 前缀。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)

    assert notifier._to_xueqiu_code("920002") == "BJ920002"
    assert notifier._to_xueqiu_code("830799") == "BJ830799"
    assert notifier._to_xueqiu_code("600519") == "SH600519"
    assert notifier._to_xueqiu_code("000001") == "SZ000001"

    # 北交所名称走腾讯行情接口
    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            text='v_bj920002="62~万达轴承~920002~55.95~56.60";'
        )
        names = notifier._get_stock_names(["920002"])
        assert names == {"920002": "万达轴承"}
        assert mock_get.call_args.args[0].startswith("https://qt.gtimg.cn/q=")


def test_baostock_unavailable_falls_back_to_tencent() -> None:
    """baostock 不可用时，沪深股票名称应回退到腾讯行情接口，而不是显示代码。"""
    import builtins

    settings = make_settings()
    notifier = FeishuNotifier(settings)

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "baostock":
            raise ModuleNotFoundError("No module named 'baostock'")
        return real_import(name, *args, **kwargs)

    with patch("requests.get") as mock_get:
        mock_get.return_value = MagicMock(
            text=(
                'v_sh600519="1~贵州茅台~600519~1700.0";'
                'v_sz000001="1~平安银行~000001~11.0";'
            )
        )
        builtins.__import__ = _fake_import
        try:
            names = notifier._get_stock_names(["600519", "000001"])
        finally:
            builtins.__import__ = real_import

    assert names == {"600519": "贵州茅台", "000001": "平安银行"}


def test_resonance_notification_contains_names_not_codes() -> None:
    """策略共振推送应显示股票名称、等级和组合，并包含雪球链接。"""
    settings = make_settings()
    notifier = FeishuNotifier(settings)
    hits: dict[str, list[dict[str, object]]] = {
        "high": [
            {
                "combo_label": "RpsYilinStrategy + TurtleTradeStrategy",
                "symbols": ["000001"],
                "count": 1,
            },
        ],
        "medium": [],
    }

    with patch.object(notifier, "_get_stock_names", return_value={"000001": "平安银行"}):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _successful_response()
            notifier.send_resonance(hits=hits, strategy_name="StrategyResonance")

    card_text = _posted_card_text(mock_post)

    assert "平安银行" in card_text
    assert "🔴 高吸引力共振" in card_text
    assert "RpsYilinStrategy + TurtleTradeStrategy" in card_text
    assert "1只" in card_text
    assert "xueqiu.com/S/" in card_text


# Feature: sequoia-x-v2, Property 11: 飞书通知使用 ConfigManager 中的 Webhook URL
@given(
    webhook_url=st.from_regex(
        r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[a-z0-9\-]{8,36}",
        fullmatch=True,
    )
)
@h_settings(max_examples=50)
def test_notification_uses_config_url(webhook_url: str) -> None:
    """send() 发出的 HTTP 请求目标 URL 应等于 settings.feishu_webhook_url。"""
    settings = make_settings(webhook_url=webhook_url)
    notifier = FeishuNotifier(settings)

    with patch.object(notifier, "_get_stock_names", return_value={"000001": "平安银行"}):
        with patch("requests.post") as mock_post:
            mock_post.return_value = _successful_response()
            notifier.send(symbols=["000001"], strategy_name="Test", webhook_key="default")

    called_url = (
        mock_post.call_args.args[0]
        if mock_post.call_args.args
        else mock_post.call_args.kwargs.get("url")
    )
    assert called_url == webhook_url


# Feature: sequoia-x-v2, Property 12: HTTP 失败时记录 ERROR 日志
@given(status_code=st.integers(min_value=400, max_value=599))
@h_settings(max_examples=50)
def test_http_failure_logs_error(status_code: int) -> None:
    """非 200 响应时，send() 应记录 ERROR 级别日志，不抛出异常。"""
    import logging as _logging

    import sequoia_x.notify.feishu as feishu_module

    settings = make_settings()
    notifier = FeishuNotifier(settings)

    # feishu logger 设置了 propagate=False，需直接在其上挂 handler
    feishu_logger = _logging.getLogger(feishu_module.__name__)
    log_records: list[_logging.LogRecord] = []

    class _ListHandler(_logging.Handler):
        def emit(self, record: _logging.LogRecord) -> None:
            log_records.append(record)

    handler = _ListHandler(_logging.ERROR)
    feishu_logger.addHandler(handler)
    try:
        with patch.object(notifier, "_get_stock_names", return_value={"000001": "平安银行"}):
            with patch("requests.post") as mock_post:
                mock_post.return_value = MagicMock(status_code=status_code, text="error")
                notifier.send(symbols=["000001"], strategy_name="Test")
    finally:
        feishu_logger.removeHandler(handler)

    assert any(r.levelno == _logging.ERROR for r in log_records)

"""主程序入口属性测试。"""

from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

# 预先导入 main 模块，避免在 @given 循环中重复导入
import main as main_module


# Feature: sequoia-x-v2, Property 13: 主程序异常以非零退出码终止
@given(error_msg=st.text(min_size=1, max_size=100))
@h_settings(max_examples=30, deadline=None)
def test_main_exits_nonzero_on_exception(error_msg: str) -> None:
    """属性 13：main() 中任意未捕获异常应导致 sys.exit(1)。"""
    # patch main 模块中直接引用的 get_settings
    with patch.object(main_module, "get_settings", side_effect=RuntimeError(error_msg)):
        with pytest.raises(SystemExit) as exc_info:
            main_module.main()
        assert exc_info.value.code != 0


def test_build_resonance_hits_uses_configured_combos() -> None:
    """策略共振只应保留高/中吸引力组合命中的股票。"""
    strategy_results = {
        "RpsYilinStrategy": ["000001", "000002"],
        "TurtleTradeStrategy": ["000001"],
        "HighTightFlagStrategy": ["000002"],
        "PrivatePlacementStrategy": ["000003"],
        "LimitUpShakeoutStrategy": ["000003"],
    }

    hits = main_module._build_resonance_hits(strategy_results)

    # 新结构：{"high": [...], "medium": [...]}，按组合分组
    high_symbols: set[str] = set()
    for group in hits["high"]:
        high_symbols.update(group["symbols"])

    medium_symbols: set[str] = set()
    for group in hits["medium"]:
        medium_symbols.update(group["symbols"])

    # 000001 在 RpsYilin + TurtleTrade 高吸引力组合中
    assert "000001" in high_symbols
    high_combos_000001 = [
        str(g["combo_label"]) for g in hits["high"] if "000001" in g["symbols"]
    ]
    assert "RpsYilinStrategy + TurtleTradeStrategy" in high_combos_000001

    # 000002 在 HighTightFlag + RpsYilin 中吸引力组合中
    assert "000002" in medium_symbols
    medium_combos_000002 = [
        str(g["combo_label"]) for g in hits["medium"] if "000002" in g["symbols"]
    ]
    assert "HighTightFlagStrategy + RpsYilinStrategy" in medium_combos_000002

    # 000003 不在任何共振组合中
    all_symbols = high_symbols | medium_symbols
    assert "000003" not in all_symbols

"""事件研究与回测统计单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.radar.backtest import _forward_stats, run_event_study


def _px(n: int = 300) -> pd.Series:
    """构造一段可预测的指数序列(每日 +0.1%)。"""
    dates = pd.bdate_range("2020-01-01", periods=n)
    return pd.Series(100.0 * 1.001 ** np.arange(n), index=dates)


class TestForwardStats:
    """_forward_stats() 的前向收益与回撤。"""

    def test_known_return(self):
        """20 日前向收益应等于构造的复利。"""
        px = _px()
        st = _forward_stats(px, px.index[0], 20)
        assert st is not None
        assert abs(st["ret"] - (1.001 ** 20 - 1)) < 1e-9
        assert st["mdd"] == 0.0

    def test_truncated_window(self):
        """接近序列末尾时按实际可得天数计算。"""
        px = _px(100)
        st = _forward_stats(px, px.index[95], 120)
        assert st is not None
        assert st["days"] == 4

    def test_out_of_range(self):
        """超出序列范围返回 None。"""
        px = _px(10)
        assert _forward_stats(px, px.index[-1] + pd.Timedelta(days=1), 20) is None


class TestEventStudy:
    """run_event_study() 的汇总结构。"""

    def test_summary_keys_and_win_rate(self):
        """顶部信号后指数上涨应为低胜率。"""
        px = _px()
        sig = pd.DataFrame([
            {"date": px.index[50], "type": "top", "score": 90.0},
            {"date": px.index[150], "type": "bottom", "score": 10.0},
        ])
        res = run_event_study(sig, px, pd.DataFrame())
        assert set(res["summary"]) >= {"全样本", "样本内(<2021)", "样本外(>=2021)"}
        top = res["summary"]["全样本"]["top"]
        assert top["20日胜率"] == 0.0
        bottom = res["summary"]["全样本"]["bottom"]
        assert bottom["20日胜率"] == 1.0

    def test_segments_split_by_oos_start(self):
        """2021 年前后应分入样本内/样本外。"""
        px = _px()
        sig = pd.DataFrame([
            {"date": pd.Timestamp("2020-06-01"), "type": "top", "score": 88.0},
            {"date": pd.Timestamp("2021-06-01"), "type": "bottom", "score": 12.0},
        ])
        res = run_event_study(sig, px, pd.DataFrame())
        segs = {e["date"]: e["segment"] for e in res["events"]}
        assert segs["2020-06-01"] == "样本内"
        assert segs["2021-06-01"] == "样本外"

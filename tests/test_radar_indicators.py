"""指标与信号单元测试(纯函数, 不依赖网络与本地数据)。"""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.radar.indicators import rolling_percentile


class TestRollingPercentile:
    """rolling_percentile() 的分位与无未来函数特性。"""

    def test_percentile_is_monotonic(self):
        """递增序列的分位应单调不减。"""
        s = pd.Series(np.arange(1, 51, dtype=float))
        p = rolling_percentile(s, window=30, min_days=20).dropna()
        assert p.is_monotonic_increasing

    def test_no_lookahead(self):
        """当日分位不应受未来数据影响。"""
        base = pd.Series(np.arange(1, 41, dtype=float))
        p1 = rolling_percentile(base, window=20, min_days=10)
        modified = base.copy()
        modified.iloc[30:] = 1000.0
        p2 = rolling_percentile(modified, window=20, min_days=10)
        pd.testing.assert_series_equal(p1.iloc[:30], p2.iloc[:30])

    def test_min_days_required(self):
        """观测不足 min_days 时返回缺失。"""
        s = pd.Series(np.arange(1, 11, dtype=float))
        p = rolling_percentile(s, window=20, min_days=12)
        assert p.isna().all()

    def test_max_value_is_top_percentile(self):
        """窗口内最大值分位为 100。"""
        s = pd.Series(np.arange(1, 21, dtype=float))
        p = rolling_percentile(s, window=20, min_days=10)
        assert p.iloc[-1] == 100.0

"""顶底雷达配置: 指标权重、分位窗口、阈值与回测参数。

权重可自由调整; direction 语义:
- hot_high: 指标越高表示市场越热(越接近顶部)
- hot_low : 指标越低表示市场越热(如股债性价比、PCR、减持)
综合分 100 = 极度亢奋(顶部风险), 0 = 极度冰点(底部机会)。
"""

RADAR_CONFIG: dict = {
    "history_start": "2014-01-01",
    "index_codes": {"000300": "sh.000300", "000001": "sh.000001"},
    "pool": {
        "index_codes": ["000300", "000905"],
        "note": "宽度/TRIN 使用沪深300+中证500当前成分股(约800只)固定样本池, 非全市场",
    },
    "percentile": {
        "window_days": 2520,
        "min_days": 250,
        "note": "滚动十年窗口; 不足十年用扩展窗口, 至少250个观测, 否则当日该指标缺失",
    },
    "composite": {
        "top_threshold": 85,
        "bottom_threshold": 15,
        "min_weight_coverage": 0.5,
        "calibrate": True,
        "calibrate_min_days": 60,
        "calibrate_note": (
            "多项百分位加权平均后分布集中在 25-75, 直接对 85/15 几乎没有信号; "
            "因此对加权均值再做一次其自身滚动十年历史分位映射(0-100), "
            "使 85/15 等价于『综合分处于历史最高的15%/最低的15%』。该映射同样只用当日及之前数据。"
        ),
        "weights": {
            "hs300_pe": {"name": "沪深300 PE分位", "group": "估值", "weight": 6, "direction": "hot_high"},
            "hs300_pb": {"name": "沪深300 PB分位", "group": "估值", "weight": 6, "direction": "hot_high"},
            "alla_pe": {"name": "全A PE分位", "group": "估值", "weight": 5, "direction": "hot_high"},
            "erp": {"name": "股债性价比", "group": "估值", "weight": 5, "direction": "hot_low"},
            "turnover": {"name": "全市场换手率(样本池中位数)", "group": "情绪", "weight": 7, "direction": "hot_high"},
            "amount_floatcap": {"name": "成交额/流通市值", "group": "情绪", "weight": 4, "direction": "hot_high"},
            "limit_net": {"name": "涨跌停家数差(样本池近似, 5日)", "group": "情绪", "weight": 7, "direction": "hot_high"},
            "margin_ratio": {"name": "两融余额/流通市值", "group": "情绪", "weight": 7, "direction": "hot_high"},
            "adv_pct_5d": {"name": "上涨家数占比(5日)", "group": "宽度", "weight": 7, "direction": "hot_high"},
            "above_ma60": {"name": "站上MA60比例", "group": "宽度", "weight": 7, "direction": "hot_high"},
            "nh_nl": {"name": "52周新高-新低差", "group": "宽度", "weight": 6, "direction": "hot_high"},
            "trin_30d": {"name": "阿姆氏值TRIN(30日均)", "group": "宽度", "weight": 5, "direction": "hot_low"},
            "bias_yearline": {"name": "指数偏离年线乖离率", "group": "趋势", "weight": 8, "direction": "hot_high"},
            "vol20": {"name": "20日实现波动率", "group": "趋势", "weight": 7, "direction": "hot_high"},
            "pcr_5d": {"name": "期权PCR 50ETF+300ETF(5日均)", "group": "情绪", "weight": 5, "direction": "hot_low"},
            "net_increase_20w": {"name": "股东净增持(20周累计)", "group": "情绪", "weight": 5, "direction": "hot_low"},
        },
    },
    "backtest": {
        "forward_days": [20, 60, 120],
        "cooldown_days": 60,
        "oos_start": "2021-01-01",
        "benchmark": "000300",
        "ood_periods": [
            {"label": "2015年异常波动(分布外)", "start": "2015-01-01", "end": "2015-12-31"},
            {"label": "2018年单边熊市(分布外)", "start": "2018-01-01", "end": "2018-12-31"},
            {"label": "2024年初微盘股流动性危机(分布外)", "start": "2024-01-01", "end": "2024-06-30"},
        ],
    },
    # 仅展示、不参与综合分的参考指标; 权重为 0, 不进入 composite.weights
    "display": {
        "trin_raw": {"name": "阿姆氏值TRIN(日频原始)", "group": "宽度", "unit": "比值",
                     "note": "全市场口径(trin_2020_now.csv), 随行情大幅波动, 建议与30日均线对照"},
        "pcr_chg5": {"name": "期权PCR 5日变化率", "group": "情绪", "unit": "%",
                     "note": "PCR(5日均)的5日变化率, 变化方向比绝对水平更有参考价值"},
        "breadth_ema5": {"name": "上涨家数占比EMA5", "group": "宽度", "unit": "%",
                         "note": "样本池口径, 0-1 表示占比; 平滑短期噪声"},
        "breadth_ema20": {"name": "上涨家数占比EMA20", "group": "宽度", "unit": "%",
                          "note": "样本池口径, 中期扩散趋势"},
        "nh_nl_ema5": {"name": "新高新低差EMA5", "group": "宽度", "unit": "家数",
                       "note": "样本池口径, 短期动量"},
        "nh_nl_ema20": {"name": "新高新低差EMA20", "group": "宽度", "unit": "家数",
                        "note": "样本池口径, 中期趋势"},
    },
}

"""抓取层纯函数测试(不联网)。"""

import pandas as pd

from scripts.radar.fetch_inputs import _pcr_to_long


def test_pcr_wide_to_long_keeps_all_rows():
    wide = pd.DataFrame({
        "date": ["2019-12-23", "2019-12-24"],
        "put_vol_510050": [1, 2], "call_vol_510050": [10, 20],
        "put_vol_510300": [3, 4], "call_vol_510300": [30, 40],
        "pcr": [0.4, 0.4],
    })
    long = _pcr_to_long(wide)
    assert len(long) == 4
    assert set(long["underlying"]) == {"510050", "510300"}
    assert long.columns.tolist() == ["date", "underlying", "put_vol", "call_vol"]


def test_pcr_long_passthrough():
    long = pd.DataFrame({"date": ["2019-12-23"], "underlying": ["510050"],
                         "put_vol": [1], "call_vol": [2]})
    assert _pcr_to_long(long).equals(long)


def test_pcr_merge_wide_and_long_has_no_gap():
    """宽表旧数据 + 长表新数据合并后, 任一段都不应被清空(历史 PCR 断档回归测试)。"""
    old = pd.DataFrame({
        "date": ["2021-11-19", "2021-11-22"],
        "put_vol_510050": [1, 2], "call_vol_510050": [2, 4],
        "put_vol_510300": [1, 2], "call_vol_510300": [2, 4],
    })
    new = pd.DataFrame({
        "date": ["2019-12-23", "2019-12-23", "2019-12-24", "2019-12-24"],
        "underlying": ["510050", "510300"] * 2,
        "put_vol": [1, 1, 2, 2], "call_vol": [2, 2, 4, 4],
    })
    merged = pd.concat([_pcr_to_long(old), new], ignore_index=True)
    wide = merged.pivot(index="date", columns="underlying", values=["put_vol", "call_vol"])
    wide.columns = [f"{c[0]}_{c[1]}" for c in wide.columns]
    wide = wide.reset_index()
    wide["pcr"] = wide[["put_vol_510050", "put_vol_510300"]].sum(axis=1) / \
        wide[["call_vol_510050", "call_vol_510300"]].sum(axis=1)
    assert wide["pcr"].notna().all()

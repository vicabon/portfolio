"""股票分割還原：依價格跳動找實際分割日（事件日 −21 / +7 天內、倍數容許 ±35%），重複套用不會重複還原。"""

import datetime
import json

import pytest

import portfolio_data as pdata
from fakes import chart, utc
from make_golden import business_days

DATES = business_days(40, start=datetime.date(2026, 10, 1))  # 2026-10-01 ~ 2026-11-25


def series(jump_at, before, after):
    return [before if i < jump_at else after for i in range(len(DATES))]


def index_of(day):
    return DATES.index(day)


def test_split_inside_window_is_restored():
    closes = series(index_of("2026-11-10"), 400.0, 100.0)
    out = pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0)])
    assert out == [100.0] * len(DATES)
    assert closes[0] == 400.0  # 不改動輸入


def test_jump_outside_window_is_left_alone():
    closes = series(index_of("2026-10-15"), 400.0, 100.0)  # 早於事件日 21 天以上
    assert pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0)]) == closes


def test_already_adjusted_data_is_unchanged_and_idempotent():
    closes = series(index_of("2026-11-10"), 400.0, 100.0)
    once = pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0)])
    twice = pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0), ("2026-11-17", 4.0)])
    assert once == twice == pdata.apply_splits(DATES, once, [("2026-11-17", 4.0)])


@pytest.mark.parametrize("jump, restored", [(4.0 * 1.3, True), (4.0 / 1.3, True), (4.0 * 1.4, False)])
def test_ratio_tolerance(jump, restored):
    closes = series(index_of("2026-11-12"), 100.0 * jump, 100.0)
    out = pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0)])
    assert (out[0] != closes[0]) is restored


def test_reverse_split():
    closes = series(index_of("2026-11-12"), 10.0, 40.0)
    out = pdata.apply_splits(DATES, closes, [("2026-11-12", 0.25)])
    assert out == [40.0] * len(DATES)


def test_best_matching_jump_wins():
    """窗口內有兩個符合容許範圍的跳動（3.0 倍與 4.1 倍）：取最接近 ratio 的那一天。"""
    a, b = index_of("2026-11-05"), index_of("2026-11-12")
    closes = [1230.0] * a + [410.0] * (b - a) + [100.0] * (len(DATES) - b)
    out = pdata.apply_splits(DATES, closes, [("2026-11-17", 4.0)])
    assert out == [307.5] * a + [102.5] * (b - a) + [100.0] * (len(DATES) - b)


def test_bad_split_date_is_ignored():
    closes = series(index_of("2026-11-10"), 400.0, 100.0)
    assert pdata.apply_splits(DATES, closes, [("2026-02-30", 4.0), (None, 4.0)]) == closes


# ------------------------------------------------------------------ splits.json

def _write(tmp_path, items):
    path = tmp_path / "splits.json"
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


def test_load_splits(tmp_path):
    path = _write(tmp_path, [
        {"ticker": " fake.tw ", "date": "2026-11-17", "ratio": 4},
        {"ticker": "FAKE.TW", "date": "2025-01-02", "ratio": "2"},
        {"ticker": "OTHER", "date": "2024-02-29", "ratio": 0.5, "_note": "反向分割"},
    ])
    assert pdata.load_splits(path) == {
        "FAKE.TW": [("2026-11-17", 4.0), ("2025-01-02", 2.0)], "OTHER": [("2024-02-29", 0.5)]}
    assert pdata.load_splits(str(tmp_path / "missing.json")) == {}


@pytest.mark.parametrize("item", [
    {"date": "2026-11-17", "ratio": 4},
    {"ticker": "FAKE", "date": "2026-02-30", "ratio": 4},
    {"ticker": "FAKE", "date": "20261117", "ratio": 4},
    {"ticker": "FAKE", "date": "2026-11-17", "ratio": 0},
    {"ticker": "FAKE", "date": "2026-11-17", "ratio": 1},
    {"ticker": "FAKE", "date": "2026-11-17", "ratio": "abc"},
    "FAKE 2026-11-17 4",
    None,
])
def test_load_splits_rejects_bad_entries(tmp_path, item):
    with pytest.raises(ValueError, match="splits.json 格式錯誤"):
        pdata.load_splits(_write(tmp_path, [item]))


def test_load_splits_requires_a_list(tmp_path):
    with pytest.raises(ValueError, match="splits.json 格式錯誤"):
        pdata.load_splits(_write(tmp_path, {"ticker": "FAKE", "date": "2026-11-17", "ratio": 4}))


def test_repo_splits_file_is_valid():
    splits = pdata.load_splits()
    for ticker, events in splits.items():
        assert ticker == ticker.strip().upper()
        assert all(ratio > 0 and ratio != 1 for _, ratio in events)


# ------------------------------------------------------------------ Yahoo 分割事件 / 歷史

def test_yahoo_split_events():
    result = chart({"gmtoffset": 28800}, splits={
        "a": {"date": utc(2026, 11, 17, 1), "numerator": 4, "denominator": 1},
        "b": {"date": utc(2026, 1, 5, 1), "numerator": 1, "denominator": 1},  # 1:1 不算分割
        "c": {"date": utc(2026, 2, 2, 1), "numerator": 0, "denominator": 1},
        "d": None,
    })
    assert pdata._yahoo_split_events(result) == [("2026-11-17", 4.0)]
    assert pdata._yahoo_split_events(chart(splits=[1, 2])) == []
    assert pdata._yahoo_split_events(None) == []


def test_yahoo_history_restores_splits_once(monkeypatch):
    stamps = [utc(2026, 11, d, 1) for d in (5, 6, 9, 10, 11)]
    result = chart({"gmtoffset": 28800}, stamps, [400.0, 404.0, 101.5, None, 102.0],
                   splits={"x": {"date": utc(2026, 11, 9, 1), "numerator": 4, "denominator": 1}})
    monkeypatch.setattr(pdata, "_yahoo_chart", lambda *_a, **_k: result)
    monkeypatch.setattr(pdata, "SPLITS", {"FAKE.TW": [("2026-11-09", 4.0)]})  # 與 Yahoo 事件重複
    out = pdata._yahoo_history("fake.tw", utc(2026, 11, 6, 0))
    assert out == {"2026-11-06": 101.0, "2026-11-09": 101.5, "2026-11-11": 102.0}


def test_yahoo_history_without_data(monkeypatch):
    monkeypatch.setattr(pdata, "_yahoo_chart", lambda *_a, **_k: None)
    assert pdata._yahoo_history("FAKE", 0) == {}

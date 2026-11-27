"""樂活五線譜：與獨立實作（statistics.linear_regression / pstdev）比對，並測 get_lohas 的兩種輸出格式。"""

import statistics

import pytest

import portfolio_data as pdata
from make_golden import business_days, fake_series

BANDS = ("m4sd", "m3sd", "m2sd", "m1sd", "tl", "p1sd", "p2sd")
SD_MULTIPLIER = {"m4sd": -4, "m3sd": -3, "m2sd": -2, "m1sd": -1, "tl": 0, "p1sd": 1, "p2sd": 2}


def reference(closes, window, display_window):
    shown = closes[-display_window:]
    reg = shown[-window:]
    xs = list(range(len(reg)))
    slope, intercept = statistics.linear_regression(xs, reg)
    sd = statistics.pstdev([y - (slope * x + intercept) for x, y in zip(xs, reg)])
    offset = len(shown) - len(reg)
    return [{band: slope * (i - offset) + intercept + k * sd for band, k in SD_MULTIPLIER.items()}
            for i in range(len(shown))]


@pytest.mark.parametrize("n, window, display_window, seed", [
    (400, 300, 350, 2),
    (120, 60, 100, 7),
    (90, 80, 80, 3),
    (50, 40, 25, 5),
])
def test_matches_independent_regression(n, window, display_window, seed):
    dates, closes = business_days(n), fake_series(n, 120.0, seed)
    out = pdata.compute_lohas(dates, closes, window, display_window)
    expected = reference(closes, window, display_window)
    rows = out["chartData"]
    assert len(rows) == min(n, display_window)
    assert out["n"] == min(window, len(rows)) and out["window"] == window
    assert out["insufficient"] is (out["n"] < window)
    assert [r["date"] for r in rows] == dates[-len(rows):]
    for row, ref in zip(rows, expected):
        for band in BANDS:
            # 各線四捨五入到 0.01：與未捨入的參考值最多差 0.005（再加浮點誤差）
            assert abs(row[band] - ref[band]) <= 0.0051, (row["date"], band, row[band], ref[band])
        assert [row[b] for b in BANDS] == sorted(row[b] for b in BANDS)


def test_insufficient_sample_is_flagged():
    out = pdata.compute_lohas(business_days(30), fake_series(30), 60, 100)
    assert (out["n"], out["insufficient"], len(out["chartData"])) == (30, True, 30)


def test_empty_series():
    assert pdata.compute_lohas([], [], 10, 10) == {"chartData": [], "n": 0, "window": 10, "insufficient": True}


@pytest.mark.parametrize("closes", [[50.0] * 20, [42.0]])
def test_flat_series_collapses_all_bands(closes):
    out = pdata.compute_lohas(business_days(len(closes)), closes, 10, 20)
    for row in out["chartData"]:
        assert {row[b] for b in BANDS} == {closes[0]}


def test_perfect_line_has_zero_sd():
    closes = [10 + 0.5 * i for i in range(30)]
    out = pdata.compute_lohas(business_days(30), closes, 30, 30)
    for row, close in zip(out["chartData"], closes):
        assert row["tl"] == close and row["p2sd"] == close and row["m4sd"] == close


# ------------------------------------------------------------------ get_lohas

@pytest.fixture
def history(monkeypatch):
    calls = []
    data = dict(zip(business_days(40), [100.123 + i for i in range(40)]))

    def fake(ticker, start_ts):
        calls.append((ticker, start_ts))
        return dict(data) if ticker != "NODATA" else {}

    monkeypatch.setattr(pdata, "_yahoo_history", fake)
    monkeypatch.setitem(pdata.CONFIG, "lohas", {"window": 30, "displayWindow": 35, "start": "2023-01-02"})
    return calls


def test_get_lohas_full_and_compact_agree(history):
    full = pdata.get_lohas("9901.tw")
    compact = pdata.get_lohas("9901.TW", compact=True)
    assert full["ticker"] == compact["ticker"] == "9901.TW"
    assert full["currencySymbol"] == "NT$"
    assert (full["n"], full["window"], full["insufficient"]) == (30, 30, False)
    assert {k: v for k, v in compact.items() if k not in ("dates", "closes")} == {
        k: v for k, v in full.items() if k != "chartData"}
    assert compact["dates"] == [r["date"] for r in full["chartData"]]
    assert compact["closes"] == [r["close"] for r in full["chartData"]]
    assert compact["closes"][0] == 105.12  # 先四捨五入到 0.01 再回歸
    assert full["lastDate"] == compact["dates"][-1] and len(compact["dates"]) == 35
    # 靜態版前端用 dates/closes 重算，結果必須和 Flask 版 chartData 相同
    assert pdata.compute_lohas(compact["dates"], compact["closes"])["chartData"] == full["chartData"]


def test_get_lohas_us_ticker_and_start(history):
    out = pdata.get_lohas("FAKE", start="2024-01-02")
    assert out["currencySymbol"] == "$"
    assert history[-1] == ("FAKE", pdata._date_to_ts("2024-01-02"))
    pdata.get_lohas("FAKE")
    assert history[-1] == ("FAKE", pdata._date_to_ts("2023-01-02"))


def test_get_lohas_errors(history, fixed_config):
    assert pdata.get_lohas("cash") == {"error": "現金無五線譜資料", "chartData": []}
    assert pdata.get_lohas("nodata") == {"error": "找不到 NODATA 的歷史數據", "chartData": []}
    assert [t for t, _ in history] == ["NODATA"]  # 現金不查歷史

"""歷史回溯：台股 + 美股（歷史匯率）+ 現金、日期對齊、上市前回補、缺資料與匯率近似（全部虛構資料）。"""

import pytest

import portfolio_data as pdata

pytestmark = pytest.mark.usefixtures("fixed_config")

D1, D2, D3 = "2026-11-02", "2026-11-03", "2026-11-04"


@pytest.fixture
def histories(monkeypatch):
    """設定 state["data"] 決定每檔的歷史；state["requests"] 記錄被要求抓哪些代號。"""
    state = {"data": {}, "requests": [], "rate": 32.0}

    def fetch(tickers, start_ts):
        state["requests"].append((list(tickers), start_ts))
        return {t: dict(state["data"].get(t, {})) for t in tickers}

    def rate():
        state["rate_calls"] = state.get("rate_calls", 0) + 1
        return state["rate"]

    monkeypatch.setattr(pdata, "_fetch_histories", fetch)
    monkeypatch.setattr(pdata, "get_usd_twd_rate", rate)
    return state


def test_tw_us_and_cash_totals(histories):
    histories["data"] = {
        "9901.TW": {D1: 100.0, D2: 101.0, D3: 102.0},
        "FAKE": {D1: 10.0, D3: 11.0},  # D2 缺值 → 沿用 D1
        "TWD=X": {D1: 30.0, D2: 30.5, D3: 31.0},
    }
    holdings = [{"ticker": "9901.TW", "shares": 10}, {"ticker": "FAKE", "shares": 2}, {"ticker": "cash", "shares": 1000}]
    out = pdata.get_history(holdings, start="2026-11-01")
    assert out["dates"] == [D1, D2, D3]
    assert out["totalValue"] == [2600.0, 2620.0, 2702.0]
    assert (out["missing"], out["backfilled"], out["fxApproximated"]) == ([], [], False)
    assert histories["requests"] == [(["9901.TW", "FAKE", "TWD=X"], pdata._date_to_ts("2026-11-01"))]


def test_taiwan_only_does_not_fetch_fx(histories):
    histories["data"] = {"9901.TW": {D1: 100.0}}
    out = pdata.get_history([{"ticker": "9901.TW", "shares": 1}])
    assert out["totalValue"] == [100.0]
    assert histories["requests"][0][0] == ["9901.TW"]


def test_listing_after_start_is_backfilled_and_reported(histories):
    days = ["2026-10-%02d" % d for d in (1, 2, 5, 12, 13)]
    histories["data"] = {
        "9901.TW": dict(zip(days, [10.0] * 5)),
        "9902.TW": {"2026-10-05": 20.0, "2026-10-13": 21.0},  # 只晚 4 天：不列入提示
        "9903.TW": {"2026-10-12": 30.0, "2026-10-13": 33.0},  # 晚 11 天：以首日價格回補並提示
    }
    holdings = [{"ticker": t, "shares": 1} for t in ("9901.TW", "9902.TW", "9903.TW")]
    out = pdata.get_history(holdings)
    assert out["dates"] == days
    assert out["totalValue"] == [60.0, 60.0, 60.0, 60.0, 64.0]
    assert out["backfilled"] == [{"ticker": "9903.TW", "firstDate": "2026-10-12"}]


def test_missing_history_is_listed_not_guessed(histories):
    histories["data"] = {"9901.TW": {D1: 100.0}}
    out = pdata.get_history([{"ticker": "9901.TW", "shares": 1}, {"ticker": "9909.TW", "shares": 5}])
    assert out["totalValue"] == [100.0]
    assert out["missing"] == ["9909.TW"]


def test_everything_missing(histories):
    out = pdata.get_history([{"ticker": "9909.TW", "shares": 5}, {"ticker": "GONE", "shares": 1}])
    assert out["dates"] == [] and out["missing"] == ["9909.TW", "GONE"]


def test_fx_history_failure_uses_current_rate(histories):
    histories["data"] = {"FAKE": {D1: 10.0, D2: 11.0}}
    out = pdata.get_history([{"ticker": "FAKE", "shares": 1}])
    assert out["fxApproximated"] is True
    assert out["totalValue"] == [320.0, 352.0]
    assert histories["rate_calls"] == 1


def test_duplicate_tickers_are_summed(histories):
    histories["data"] = {"FAKE": {D1: 10.0}, "TWD=X": {D1: 30.0}}
    out = pdata.get_history([{"ticker": "FAKE", "shares": 1}, {"ticker": " fake ", "shares": 2, "market": "TW"}])
    assert out["totalValue"] == [900.0]


def test_2025_curve(histories):
    histories["data"] = {"9901.TW": {D1: 100.0, D2: 110.0}, "9902.TW": {D1: 50.0, D2: 40.0}}
    out = pdata.get_history([{"ticker": "9901.TW", "shares": 1}],
                            holdings_2025=[{"ticker": "9902.TW", "shares": 2}, {"ticker": "CASH", "shares": 7}])
    assert out["totalValue"] == [100.0, 110.0]
    assert out["totalValue2025"] == [107.0, 87.0]
    assert out["holdings2025"] == [{"ticker": "9902.TW", "shares": 2.0}, {"ticker": "CASH", "shares": 7.0}]
    assert sorted(histories["requests"][0][0]) == ["9901.TW", "9902.TW"]


@pytest.mark.parametrize("holdings", [[], None, [{"ticker": "9901.TW", "shares": 0}],
                                      [{"ticker": "9901.TW", "shares": "-"}]])
def test_no_holdings_returns_empty_history(histories, holdings):
    out = pdata.get_history(holdings)
    assert out == pdata._empty_history()
    assert histories["requests"] == []

"""多源共識選價規則與報價流程（快取、缺值不冒充、單一來源失敗不影響其他來源）。"""

import pytest

import portfolio_data as pdata
from fakes import utc

pytestmark = pytest.mark.usefixtures("fixed_config")

NOW = utc(2026, 11, 20, 5, 30)


def q(price, as_of=NOW, prev=None, name=None):
    return {"price": price, "prevClose": prev, "asOf": as_of, "name": name}


def resolve(quotes, priority=pdata.TW_PRIORITY):
    return pdata.resolve_consensus(quotes, priority)


# ------------------------------------------------------------------ 共識規則（門檻固定為 2% / 6 小時）

def test_single_source_is_used():
    out = resolve({"twse": q(None), "cnyes": q(100.0), "yahoo": {}})
    assert (out["price"], out["source"], out["conflict"], out["stale"]) == (100.0, "cnyes", False, False)
    assert out["sources"] == {"cnyes": 100.0}


def test_three_sources_take_median():
    out = resolve({"twse": q(100.0), "cnyes": q(100.5), "yahoo": q(101.0)})
    assert (out["price"], out["source"], out["conflict"]) == (100.5, "cnyes", False)


def test_three_sources_all_disagree_is_conflict():
    out = resolve({"twse": q(100.0), "cnyes": q(110.0), "yahoo": q(125.0)})
    assert (out["price"], out["source"], out["conflict"]) == (110.0, "cnyes", True)


def test_two_agreeing_sources_follow_priority():
    out = resolve({"twse": q(100.0), "yahoo": q(101.9)})
    assert (out["price"], out["source"], out["conflict"]) == (100.0, "twse", False)


def test_two_disagreeing_sources_take_newer_and_flag_conflict():
    out = resolve({"twse": q(100.0, as_of=NOW - 60), "yahoo": q(105.0, as_of=NOW)})
    assert (out["price"], out["source"], out["conflict"]) == (105.0, "yahoo", True)


def test_stale_source_does_not_vote():
    out = resolve({"twse": q(100.0), "cnyes": q(150.0, as_of=NOW - 6 * 3600 - 1)})
    assert (out["price"], out["source"], out["conflict"]) == (100.0, "twse", False)


def test_millisecond_timestamp_is_ignored_not_trusted():
    out = resolve({"twse": q(100.0, as_of=NOW * 1000), "yahoo": q(100.5)})
    assert out["source"] == "twse" and out["asOf"] is None


@pytest.mark.parametrize("bad", [0, -1, True, "100", float("nan"), float("inf")])
def test_invalid_prices_do_not_vote(bad):
    out = resolve({"twse": q(bad), "yahoo": q(100.0)})
    assert (out["price"], out["source"]) == (100.0, "yahoo")


def test_no_live_price_uses_prev_close_and_flags_stale():
    out = resolve({"twse": q(None, prev=None), "cnyes": q(None, prev=99.0), "yahoo": q(None, prev=98.0)})
    assert (out["price"], out["source"], out["stale"], out["sources"]) == (99.0, "cnyes:prevClose", True, {})


def test_nothing_at_all_returns_none():
    out = resolve({"twse": {}, "cnyes": None, "yahoo": "bad"})
    assert out["price"] is None and out["source"] is None and out["stale"] is False


# ------------------------------------------------------------------ 報價流程

@pytest.fixture
def sources(monkeypatch):
    """可調整的假來源；calls 記錄 Yahoo 被查了哪些代號。"""
    state = {
        "twse": {"9901.TW": q(100.0, name="假股")},
        "cnyes": {"9901.TW": q(100.5), "FAKE": q(10.0)},
        "yahoo": {"9901.TW": q(101.0), "FAKE": q(10.1, name="Fake Corp")},
        "fx": {"value": 30.0, "source": "yahoo", "asOf": NOW, "isFallback": False},
        "calls": [],
    }

    def yahoo(ticker):
        state["calls"].append(ticker)
        value = state["yahoo"].get(ticker, {})
        if isinstance(value, Exception):
            raise value
        return value

    def maybe_raise(key):
        def fetch(*_args):
            value = state[key]
            if isinstance(value, Exception):
                raise value
            return value
        return fetch

    monkeypatch.setattr(pdata, "_twse_quotes", lambda tickers: maybe_raise("twse")())
    monkeypatch.setattr(pdata, "_cnyes_quotes", lambda tickers: maybe_raise("cnyes")())
    monkeypatch.setattr(pdata, "_yahoo_quote_detail", yahoo)
    monkeypatch.setattr(pdata, "get_usd_twd_rate_info", maybe_raise("fx"))
    return state


def test_get_quotes_tw_us_and_cash(sources):
    out = pdata.get_quotes(["9901.tw", " fake ", "cash", "FAKE", ""])
    assert list(out) == ["9901.TW", "FAKE", "CASH"]
    tw, us, cash = out["9901.TW"], out["FAKE"], out["CASH"]
    assert (tw["price"], tw["source"], tw["name"], tw["currency"]) == (100.5, "cnyes", "假股", "TWD")
    assert (us["priceUsd"], us["price"], us["fxRate"], us["source"]) == (10.1, 303.0, 30.0, "yahoo")
    assert us["sources"] == {"yahoo": 10.1, "cnyes": 10.0}
    assert (cash["price"], cash["source"]) == (1.0, "cash")
    assert sorted(sources["calls"]) == ["9901.TW", "FAKE"]  # 現金不查價


def test_quotes_are_cached_and_copied(sources):
    first = pdata.get_quotes(["FAKE"])
    first["FAKE"]["sources"]["yahoo"] = 0
    second = pdata.get_quotes(["FAKE"])
    assert second["FAKE"]["sources"]["yahoo"] == 10.1
    assert sources["calls"] == ["FAKE"]


def test_missing_price_is_not_cached(sources):
    sources["yahoo"], sources["cnyes"] = {}, {}
    assert pdata.get_quote("FAKE")["price"] is None
    sources["yahoo"] = {"FAKE": q(10.1)}
    assert pdata.get_quote("FAKE")["price"] == 303.0
    assert sources["calls"] == ["FAKE", "FAKE"]


def test_one_failing_source_does_not_break_others(sources):
    sources["cnyes"] = RuntimeError("cnyes down")
    sources["twse"] = RuntimeError("twse down")
    out = pdata.get_quotes(["9901.TW", "FAKE"])
    assert (out["9901.TW"]["price"], out["9901.TW"]["source"]) == (101.0, "yahoo")
    assert out["FAKE"]["price"] == 303.0


def test_missing_fx_leaves_twd_price_empty(sources):
    sources["fx"] = RuntimeError("fx down")
    out = pdata.get_quotes(["FAKE"])["FAKE"]
    assert out["price"] is None and out["priceUsd"] == 10.1 and out["fxRate"] is None


def test_get_quote_for_cash_and_unknown(sources):
    assert pdata.get_quote("twd-cash")["price"] == 1.0
    sources["yahoo"], sources["cnyes"] = {}, {}
    out = pdata.get_quote("NOPE")
    assert out["price"] is None and out["stale"] is False and out["conflict"] is False

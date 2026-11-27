"""外部來源的解析與失敗處理：TWSE、鉅亨網、Yahoo、匯率、CNN、DollarLiquidity、VIX（全部假回應，不連網）。"""

import datetime

import pytest
import requests

import portfolio_data as pdata
from fakes import FakeResponse, chart, chart_response, utc

TWSE = pdata._TWSE_URL
CNYES = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/"
Q1 = "https://query1.finance.yahoo.com/v8/finance/chart/"
Q2 = "https://query2.finance.yahoo.com/v8/finance/chart/"
GOOGLE_FX = pdata._GOOGLE_FX_URL
CNN = pdata._CNN_FG_URL
PE_OVERVIEW = pdata._DOLLARLIQUIDITY_OVERVIEW_URL
PE_DETAIL = pdata._DOLLARLIQUIDITY_URL
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ------------------------------------------------------------------ TWSE

def twse_row(code, ex="tse", z="-", b="-", a="-", y="100.0"):
    return {"c": code, "ex": ex, "ch": code + ".tw", "z": z, "b": b, "a": a, "y": y,
            "tlong": "1795152600000", "n": "假股" + code}


@pytest.mark.parametrize("fields, expected", [
    ({"z": "101.5"}, 101.5),
    ({"z": "0.0000", "b": "0.0000_100.5000_100.0000_"}, 100.5),  # 市價單編碼 0.0000 要跳過
    ({"z": "-", "a": "0.0000_101.0000_"}, 101.0),
    ({}, None),  # 尚未成交：price 為 None，不拿昨收冒充
])
def test_twse_row_price_order(fields, expected):
    parsed = pdata._parse_twse_row(twse_row("9901", **fields))
    assert parsed == {"price": expected, "prevClose": 100.0, "asOf": 1795152600, "name": "假股9901"}


def test_twse_quotes_batches_and_maps_channels(fake_http):
    tickers = ["99%02d.TW" % i for i in range(25)] + ["9988.TWO", "FAKE"]

    def handler(url, params=None, **_kwargs):
        rows = []
        for channel in params["ex_ch"].split("|"):
            ex, rest = channel.split("_", 1)
            rows.append(twse_row(rest.split(".")[0], ex=ex, z="10.5"))
        rows.append(twse_row("0000", z="1"))  # 沒查詢的頻道要忽略
        rows.append("not-a-dict")
        return FakeResponse({"msgArray": rows})

    fake_http.route(TWSE, handler)
    out = pdata._twse_quotes(tickers)
    assert fake_http.count(TWSE) == 2  # 26 個台股頻道，每批 20
    assert set(out) == {t for t in tickers if t != "FAKE"}
    assert out["9988.TWO"]["price"] == 10.5
    assert pdata.get_source_stats()["twse"] == {"ok": 2, "fail": 0, "empty": 0}


def test_twse_failed_batch_keeps_other_batches(fake_http):
    tickers = ["99%02d.TW" % i for i in range(21)]
    state = {"n": 0}

    def handler(url, params=None, **_kwargs):
        state["n"] += 1
        if state["n"] == 1:
            return FakeResponse(text="busy", status=503)
        return FakeResponse({"msgArray": [twse_row(c.split(".")[0][4:], z="5") for c in params["ex_ch"].split("|")]})

    fake_http.route(TWSE, handler)
    out = pdata._twse_quotes(tickers)
    assert list(out) == ["9920.TW"]
    assert pdata.get_source_stats()["twse"] == {"ok": 1, "fail": 1, "empty": 0}


def test_twse_empty_msgarray_is_recorded(fake_http):
    fake_http.route(TWSE, FakeResponse({"msgArray": []}))
    assert pdata._twse_quotes(["9901.TW"]) == {}
    assert pdata.get_source_stats()["twse"]["empty"] == 1


# ------------------------------------------------------------------ 鉅亨網

def test_cnyes_quotes_maps_symbols_and_skips_cash(fake_http, fixed_config):
    fake_http.route_prefix(CNYES, FakeResponse({"data": [
        {"0": "TWS:9901:STOCK", "6": 50.5, "21": 50.0, "200007": 1795152600, "200009": "假股"},
        {"800013": "USS:FAKE:STOCK", "6": "12.34", "21": "-", "200007": "1795152600.9"},
        {"0": "USS:OTHER:STOCK", "6": 1},
        {"0": None, "800013": None},
        "not-a-dict",
    ]}))
    out = pdata._cnyes_quotes(["9901.TW", "fake", "CASH"])
    assert out == {
        "9901.TW": {"price": 50.5, "prevClose": 50.0, "asOf": 1795152600, "name": "假股"},
        "FAKE": {"price": 12.34, "prevClose": None, "asOf": 1795152600, "name": None},
    }
    url = fake_http.calls[0][0]
    assert url == CNYES + "TWS:9901:STOCK,USS:FAKE:STOCK"


def test_cnyes_only_cash_makes_no_request(fake_http, fixed_config):
    assert pdata._cnyes_quotes(["CASH", "twd"]) == {}
    assert fake_http.calls == []


def test_cnyes_failure_returns_empty(fake_http):
    assert pdata._cnyes_quotes(["FAKE"]) == {}
    assert pdata.get_source_stats()["cnyes"] == {"ok": 0, "fail": 1, "empty": 0}


# ------------------------------------------------------------------ Yahoo

def test_yahoo_chart_falls_back_to_query2(fake_http):
    result = chart({"regularMarketPrice": 10.0})
    fake_http.route(Q1 + "FAKE", FakeResponse(text="err", status=500))
    fake_http.route(Q2 + "FAKE", chart_response(result))
    assert pdata._yahoo_chart("FAKE") == result
    assert pdata.get_source_stats()["yahoo"] == {"ok": 1, "fail": 1, "empty": 0}


def test_yahoo_chart_without_result_returns_none(fake_http):
    fake_http.route(Q1 + "FAKE", chart_response(None))
    fake_http.route(Q2 + "FAKE", FakeResponse({"chart": {"result": None, "error": {"code": "Not Found"}}}))
    assert pdata._yahoo_chart("FAKE") is None
    assert pdata.get_source_stats()["yahoo"] == {"ok": 2, "fail": 0, "empty": 2}


def test_yahoo_chart_params(fake_http):
    fake_http.route(Q1 + "FAKE", chart_response(chart()))
    pdata._yahoo_chart("FAKE", period1=1700000000, period2=1700100000, events="split")
    pdata._yahoo_chart("FAKE")
    assert fake_http.calls[0][1]["params"] == {
        "interval": "1d", "period1": 1700000000, "period2": 1700100000, "events": "split"}
    assert fake_http.calls[1][1]["params"] == {"interval": "1d", "range": "5d"}


TW_OFFSET = 28800
DAY1, DAY2 = utc(2026, 11, 19, 1), utc(2026, 11, 20, 1)  # 台北 09:00


@pytest.mark.parametrize("meta, expected", [
    ({"previousClose": 99.0, "regularMarketTime": utc(2026, 11, 20, 5), "gmtoffset": TW_OFFSET}, 99.0),
    ({"regularMarketTime": utc(2026, 11, 20, 5), "gmtoffset": TW_OFFSET}, 98.0),  # 最後一根是今天 → 取倒數第二根
    ({"regularMarketTime": utc(2026, 11, 21, 5), "gmtoffset": TW_OFFSET}, 100.0),  # 最後一根是昨天 → 取最後一根
    ({"previousClose": 0, "chartPreviousClose": 90.0, "regularMarketTime": utc(2026, 11, 20, 5),
      "gmtoffset": TW_OFFSET}, 98.0),  # previousClose 為 0 不可用
])
def test_prev_close_rules(meta, expected):
    assert pdata._prev_close_from_chart(chart(meta, [DAY1, DAY2], [98.0, 100.0])) == expected


def test_prev_close_chart_previous_close_is_last_resort():
    assert pdata._prev_close_from_chart(chart({"chartPreviousClose": 90.0})) == 90.0
    assert pdata._prev_close_from_chart(None) is None


def test_yahoo_quote_without_market_price_uses_last_bar():
    quote = pdata.yahoo_quote_from_chart(chart({"gmtoffset": TW_OFFSET, "longName": "假基金"}, [DAY1, DAY2],
                                               [98.0, None]))
    assert (quote["price"], quote["asOf"], quote["name"]) == (98.0, DAY1, "假基金")


# ------------------------------------------------------------------ 匯率

def fx_response(rate):
    return chart_response(chart({"regularMarketPrice": rate, "regularMarketTime": utc(2026, 11, 20, 6)}))


def test_fx_prefers_yahoo(fake_http, fixed_config):
    fake_http.route(Q1 + "TWD=X", fx_response(31.25))
    assert pdata.get_usd_twd_rate_info() == {
        "value": 31.25, "source": "yahoo", "asOf": utc(2026, 11, 20, 6), "isFallback": False}


def test_fx_implausible_yahoo_falls_back_to_google(fake_http, fixed_config):
    fake_http.route(Q1 + "TWD=X", fx_response(3125.0))
    fake_http.route(GOOGLE_FX, FakeResponse(text='<div data-last-price="31.4021" data-currency="TWD">'))
    info = pdata.get_usd_twd_rate_info()
    assert (info["value"], info["source"], info["isFallback"]) == (31.4021, "google", False)


def test_fx_google_implausible_value_is_rejected(fake_http, fixed_config):
    fake_http.route(GOOGLE_FX, FakeResponse(text='<div data-last-price="3.14">'))
    info = pdata.get_usd_twd_rate_info()
    assert info["isFallback"] and info["source"] == "fallback"
    assert pdata.get_source_stats()["google-fx"] == {"ok": 1, "fail": 0, "empty": 1}


def test_fx_fallback_when_every_source_fails(offline, fixed_config):
    assert pdata.get_usd_twd_rate_info() == {
        "value": float(fixed_config["fx"]["fallbackRate"]), "source": "fallback", "asOf": None, "isFallback": True}


def test_fx_is_cached_and_returns_copies(fake_http, fixed_config):
    fake_http.route(Q1 + "TWD=X", fx_response(31.25))
    first = pdata.get_usd_twd_rate_info()
    first["value"] = 0
    assert pdata.get_usd_twd_rate_info()["value"] == 31.25
    assert fake_http.count(Q1) == 1


def test_fx_fallback_cache_expires_sooner(fake_http, fixed_config):
    assert pdata.get_usd_twd_rate_info()["isFallback"]
    calls = len(fake_http.calls)
    pdata.get_usd_twd_rate_info()
    assert len(fake_http.calls) == calls  # 保底值 60 秒內也會快取
    pdata._fx_cache["ts"] -= pdata._FX_FALLBACK_TTL_SECONDS + 1
    fake_http.route(Q1 + "TWD=X", fx_response(31.25))
    assert pdata.get_usd_twd_rate_info()["source"] == "yahoo"
    pdata._fx_cache["ts"] -= pdata._FX_FALLBACK_TTL_SECONDS + 1  # 真實匯率的快取比較久（5 分鐘）
    calls = len(fake_http.calls)
    pdata.get_usd_twd_rate_info()
    assert len(fake_http.calls) == calls


# ------------------------------------------------------------------ CNN Fear & Greed

def test_fear_greed_offline(offline):
    out = pdata.get_fear_greed()
    assert out["score"] is None and out["rating"] is None and out["error"] == "CNN 無回應"


@pytest.mark.parametrize("response", [
    FakeResponse(text="<html>blocked</html>"),
    FakeResponse({"fear_and_greed": {"score": 140}}),
    FakeResponse({"unexpected": True}),
])
def test_fear_greed_bad_payload(fake_http, response):
    fake_http.route(CNN, response)
    out = pdata.get_fear_greed()
    assert out["score"] is None and out["error"] == "CNN 資料格式異常"
    assert pdata.get_source_stats()["cnn"] == {"ok": 1, "fail": 0, "empty": 1}


def test_fear_greed_ok(fake_http):
    fake_http.route(CNN, FakeResponse({"fear_and_greed": {
        "score": 61.66, "rating": "greed", "timestamp": "2026-11-20T23:59:57+00:00", "previous_close": 60.04}}))
    out = pdata.get_fear_greed()
    assert (out["score"], out["rating"], out["previousClose"], out["previous1Week"], out["error"]) == (
        61.7, "GREED", 60.0, None, None)


# ------------------------------------------------------------------ DollarLiquidity Forward P/E

def english(day):
    return MONTHS[day.month - 1] + " " + str(day.day) + ", " + str(day.year)


def overview_html(value, day, change="−0.12"):
    return ('<div class="card"><h3>Nasdaq-100 Forward P/E</h3><div class="val">' + value + "<span>×</span></div>"
            "<small>As of " + english(day) + '</small><span class="chg">' + change + "</span></div>")


def detail_text(value, day):
    return ("<p>Forward P/E Ratio Today: " + value + "× (" + day.isoformat() + ")</p>"
            '[{\\"date\\":\\"2026-01-01\\",\\"pe\\":26.5},{\\"date\\":\\"2026-01-02\\",\\"pe\\":26.6}],'
            '\\"historyPoints\\":2')


def test_pe_overview_ok(fake_http, fixed_config):
    today = pdata.today_tpe()
    fake_http.route(PE_OVERVIEW, FakeResponse(text=overview_html("27.35", today)))
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert (out["value"], out["change"], out["previousValue"], out["previousValueDerived"]) == (27.35, -0.12, 27.47, True)
    assert (out["isFallback"], out["error"], out["stale"], out["source"]) == (False, None, False, "DollarLiquidity")
    assert out["date"] == today.isoformat() and out["url"] == PE_DETAIL
    assert fake_http.count(PE_OVERVIEW) == 1
    kwargs = fake_http.calls[0][1]
    assert "_" in kwargs["params"] and kwargs["headers"]["Cache-Control"] == "no-cache"


def test_pe_overview_react_markup_needs_no_detail_page(fake_http, fixed_config):
    """網站實際版面：正負號與數字之間夾著 React 的 <!-- --> 註解；總覽頁就要解析成功，不必再抓明細頁。"""
    today = pdata.today_tpe()
    html = ('<h2>Nasdaq-100 Forward P/E</h2><p>27.35<span>×</span></p>'
            "<span>As of<!-- --> <!-- -->" + english(today) + '</span><span>+<!-- -->0.05</span>')
    fake_http.route(PE_OVERVIEW, FakeResponse(text=html))
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert (out["value"], out["change"], out["previousValue"], out["isFallback"]) == (27.35, 0.05, 27.3, False)
    assert len(fake_http.calls) == 1 and fake_http.count(PE_DETAIL) == 0
    assert pdata.get_source_stats()["dollarliquidity"]["empty"] == 0


@pytest.mark.parametrize("overview", [
    FakeResponse(text=overview_html("600", datetime.date(2026, 11, 21))),  # 超出合理範圍
    FakeResponse(text="<html>layout changed</html>"),
    requests.ConnectionError("down"),
])
def test_pe_falls_back_to_detail_page(fake_http, fixed_config, overview):
    today = pdata.today_tpe()
    fake_http.route(PE_OVERVIEW, overview)
    fake_http.route(PE_DETAIL, FakeResponse(text=detail_text("27.35", today)))
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert (out["value"], out["previousValue"], out["previousDate"], out["previousValueDerived"]) == (
        27.35, 26.5, "2026-01-01", False)
    assert out["isFallback"] is False and out["stale"] is False


def test_pe_reuses_last_good_value(fake_http, fixed_config):
    today = pdata.today_tpe()
    fake_http.route(PE_OVERVIEW, FakeResponse(text=overview_html("27.35", today)))
    assert pdata.get_nasdaq100_forward_pe_dollarliquidity()["isFallback"] is False
    fake_http.route(PE_OVERVIEW, FakeResponse(text="broken"))
    fake_http.route(PE_DETAIL, FakeResponse(text="broken"))
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert (out["value"], out["isFallback"], out["error"]) == (27.35, True, "本次解析失敗，沿用上次成功值")


def test_pe_unavailable_when_nothing_ever_parsed(offline, fixed_config):
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert (out["value"], out["error"], out["stale"], out["isFallback"]) == (
        None, "DollarLiquidity 解析失敗", False, False)


@pytest.mark.parametrize("age_days, stale", [(0, False), (7, False), (8, True)])
def test_pe_stale_flag(fake_http, fixed_config, age_days, stale):
    day = pdata.today_tpe() - datetime.timedelta(days=age_days)
    fake_http.route(PE_OVERVIEW, FakeResponse(text=overview_html("27.35", day)))
    assert pdata.get_nasdaq100_forward_pe_dollarliquidity()["stale"] is stale


def test_pe_unparseable_date_is_stale(fake_http, fixed_config):
    html = overview_html("27.35", datetime.date(2026, 2, 28)).replace("Feb 28", "Feb 30")
    fake_http.route(PE_OVERVIEW, FakeResponse(text=html))
    out = pdata.get_nasdaq100_forward_pe_dollarliquidity()
    assert out["value"] == 27.35 and out["date"] is None and out["stale"] is True


# ------------------------------------------------------------------ VIX / 市場指標

def test_vix_ok(fake_http):
    fake_http.route(Q1 + "%5EVIX", chart_response(chart({
        "regularMarketPrice": 18.234, "previousClose": 17.5, "regularMarketTime": utc(2026, 11, 20, 21),
        "gmtoffset": -18000})))
    assert pdata.get_vix() == {"value": 18.23, "previousValue": 17.5, "date": "2026-11-20"}


def test_market_indicators_offline_never_raises(offline, fixed_config):
    out = pdata.get_market_indicators()
    assert out["vix"] == {"value": None, "previousValue": None, "date": None}
    pe = out["nasdaq100ForwardPe"]["dollarliquidity"]
    assert pe["value"] is None and pe["error"]

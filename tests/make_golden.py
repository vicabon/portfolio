#!/usr/bin/env python3
"""產生 tests/golden/*.json：同一批輸入在 Python 端的期望輸出，core.js 必須算出一模一樣的結果。

    python tests/make_golden.py           # 重新產生全部 golden
    python tests/make_golden.py --check   # 只比對、不寫檔（有差異 exit 1）

golden 是 Python 與 core.js 之間的契約：
  - tests/test_golden.py   ：Python 目前的輸出必須等於 golden。改了 Python 規則 → 重新產生 golden，
                             並同步修改 core.js，否則下一項會失敗。
  - tests/js/run_golden.js ：core.js 對同一批輸入必須得到相同輸出（tests/test_js_golden.py 會呼叫它）。
只有前端才有的邏輯（借貸攤銷、upsertLastPoint、HTML 跳脫、新鮮度分級、交易所日期），
期望值來自本檔獨立撰寫的參考實作（照 README 描述的規則寫，不是從 core.js 抄過來）。

所有輸入都是虛構資料（假代號、假股數、假金額、假借款），不含任何真實持股或負債。
門檻用本檔的 TEST_CONFIG，不讀 config.json：調整 config.json 不會讓測試失敗。

case 格式：{"name", "js": core.js 函式名, "args": [...], "expected": ...}
  "throws": true        預期丟出錯誤（Python 端為 ValueError），沒有 expected
  "tol": 1e-12          數值以相對誤差比較（只用於二分搜尋解利率：pow 在不同平台可能差 1 ulp）
  "argTypes": [...]     "date" 代表該參數是 YYYY-MM-DD，JS 端轉成本機時區的 Date
  "resultType": "date"  JS 回傳 Date，轉成 YYYY-MM-DD 後再比較
"""

import argparse
import calendar
import contextlib
import copy
import datetime
import json
import math
import os
import re
import sys
from unittest import mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
GOLDEN_DIR = os.path.join(TESTS_DIR, "golden")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app  # noqa: E402
import portfolio_data  # noqa: E402

COMMENT = "由 tests/make_golden.py 產生，請勿手動修改（輸入皆為虛構資料）"

TEST_CONFIG = {
    "cashTickers": ["TWD-CASH", "CASH", "TWD"],
    "consensus": {"twoSourceMaxDiff": 0.02, "staleGapSeconds": 21600},
    "betaBuckets": {"cashBeta": 0, "leveragedMin": 2},
    "fx": {"fallbackRate": 32.0, "min": 20, "max": 50},
    "forwardPe": {"min": 8, "max": 60, "staleDays": 7},
    "freshness": {"warnMinutes": 90, "alertMinutes": 360},
}
CONS = TEST_CONFIG["consensus"]
CASH = TEST_CONFIG["cashTickers"]


@contextlib.contextmanager
def patched_config():
    """Python 端以 TEST_CONFIG 計算（與傳給 JS 的 cfg 相同）。"""
    keys = ("consensus", "betaBuckets", "fx", "forwardPe", "freshness")
    saved = {k: portfolio_data.CONFIG[k] for k in keys}
    saved_cash = portfolio_data._CASH_TICKERS
    try:
        for k in keys:
            portfolio_data.CONFIG[k] = copy.deepcopy(TEST_CONFIG[k])
        portfolio_data._CASH_TICKERS = frozenset(CASH)
        yield
    finally:
        portfolio_data.CONFIG.update(saved)
        portfolio_data._CASH_TICKERS = saved_cash


def _json(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False, allow_nan=False))


def case(name, js, args, compute, **extra):
    """compute：不帶參數的 callable，回傳期望值；丟出 ValueError 代表 JS 端也必須丟錯。"""
    out = {"name": name, "js": js, "args": _json(list(args))}
    try:
        result = compute()
    except ValueError:
        out["throws"] = True
    else:
        out["expected"] = _json(result)
    out.update(extra)
    return out


def utc(y, mo, d, hh=0, mi=0):
    return calendar.timegm((y, mo, d, hh, mi, 0, 0, 0, 0))


def chart(meta=None, stamps=(), closes=()):
    """Yahoo chart API 的 result[0]。"""
    return {"meta": dict(meta or {}), "timestamp": list(stamps), "indicators": {"quote": [{"close": list(closes)}]}}


# ------------------------------------------------------------------ consensus

def build_consensus():
    t = utc(2026, 11, 20, 5, 30)
    q = lambda price=None, prev=None, as_of=None, name=None: {  # noqa: E731
        "price": price, "prevClose": prev, "asOf": as_of, "name": name,
    }
    tw = list(portfolio_data.TW_PRIORITY)
    rc = portfolio_data.resolve_consensus
    md, gap = CONS["twoSourceMaxDiff"], CONS["staleGapSeconds"]
    scenarios = [
        ("三源一致取中位數", {"twse": q(100.0, 99, t), "cnyes": q(100.5, 99, t), "yahoo": q(99.8, 99, t)}, tw),
        ("三源一源離群仍取中位數", {"twse": q(100.0, None, t), "cnyes": q(100.2, None, t), "yahoo": q(150.0, None, t)}, tw),
        ("三源兩兩不一致標 conflict", {"twse": q(100.0, None, t), "cnyes": q(125.0, None, t), "yahoo": q(110.0, None, t)}, tw),
        ("兩源一致依優先序", {"twse": q(None, 98, t), "cnyes": q(100.0, 98, t), "yahoo": q(101.0, 98, t)}, tw),
        ("兩源不一致取較新者並標 conflict", {"cnyes": q(100.0, None, t - 60), "yahoo": q(110.0, None, t)}, tw),
        ("兩源不一致且都沒有時間取優先序第一個", {"cnyes": q(100.0), "yahoo": q(110.0)}, tw),
        ("比最新報價舊超過 staleGap 的來源不投票",
         {"twse": q(100.0, None, t), "cnyes": q(100.1, None, t - 30000), "yahoo": q(140.0, None, t)}, tw),
        ("剛好等於 staleGap 仍可投票",
         {"twse": q(100.0, None, t), "cnyes": q(100.1, None, t - gap), "yahoo": q(140.0, None, t)}, tw),
        ("沒有即時價時依優先序沿用昨收", {"twse": q(None, 99.0, t), "cnyes": q(None, 98.0, t), "yahoo": {}}, tw),
        ("只有 yahoo 有昨收", {"twse": q(), "cnyes": q(), "yahoo": q(None, 97.5)}, tw),
        ("完全沒有資料", {}, tw),
        ("字串、布林、0 都不是有效價格", {"twse": q("100"), "cnyes": q(True), "yahoo": q(0)}, tw),
        ("毫秒時間戳視為沒有時間", {"cnyes": q(100.0, None, t * 1000), "yahoo": q(110.0, None, t)}, tw),
        ("字串時間戳視為沒有時間", {"cnyes": q(100.0, None, t), "yahoo": q(110.0, None, str(t))}, tw),
        ("不是物件的來源略過", {"twse": None, "cnyes": q(100.0, None, t), "yahoo": "bad"}, tw),
        ("優先序以外的來源依名稱排在後面", {"zeta": q(100.0), "alpha": q(100.1)}, tw),
        ("單一來源直接採用", {"yahoo": q(55.5, 54.0, t)}, list(portfolio_data.US_PRIORITY)),
        ("負價與 0 不投票", {"twse": q(-5.0), "cnyes": q(0.0), "yahoo": q(50.0, None, t)}, tw),
        ("美股兩源差距剛好 2% 視為一致", {"yahoo": q(100.0), "cnyes": q(98.0)}, list(portfolio_data.US_PRIORITY)),
    ]
    cases = [
        case(name, "resolveConsensus", [quotes, prio, CONS],
             lambda quotes=quotes, prio=prio: rc(copy.deepcopy(quotes), tuple(prio), md, gap))
        for name, quotes, prio in scenarios
    ]

    tw_cases = [
        ("台股三源名稱取證交所", q(100.0, 99, t, "測試一"), q(100.2, 99, t, "測試一號"), q(99.9, 99, t, "TEST ONE")),
        ("台股證交所缺資料時名稱取鉅亨網", None, q(100.0, 99, t, "測試二"), q(100.1, 99, t, "TEST TWO")),
        ("台股三源皆缺", None, None, None),
        ("台股只剩昨收", q(None, 88.0, t, "測試三"), None, None),
    ]
    for name, a, b, c in tw_cases:
        cases.append(case(name, "twQuote", [a, b, c, CONS],
                          lambda a=a, b=b, c=c: portfolio_data._tw_quote(copy.deepcopy(a), copy.deepcopy(b),
                                                                         copy.deepcopy(c))))

    us_cases = [
        ("美股兩源一致換算台幣", q(123.45, 120, t, "Fake Corp"), q(123.5, 120, t, "Fake"), 31.234),
        ("美股沒有匯率時台幣價為 null", q(123.45, 120, t, "Fake Corp"), None, None),
        ("美股匯率為 0 視為沒有匯率", q(123.45, 120, t), None, 0),
        ("美股只有鉅亨網", None, q(77.7, 70, t, "Only Cnyes"), 30.5),
        ("美股只剩昨收也換算台幣", q(None, 50.0, t), q(None, 49.0, t), 32.1),
    ]
    for name, a, b, fx in us_cases:
        cases.append(case(name, "usQuote", [a, b, fx, CONS],
                          lambda a=a, b=b, fx=fx: portfolio_data._us_quote(copy.deepcopy(a), copy.deepcopy(b), fx)))
    return cases


# ------------------------------------------------------------------ Yahoo chart（昨收 / 報價 / VIX / 匯率）

def build_prev_close():
    tw_meta = {"gmtoffset": 28800}
    tw_days = [utc(2026, 11, d, 1, 0) for d in (18, 19, 20)]  # 台北 09:00 開盤時間戳
    tw_now = utc(2026, 11, 20, 5, 30)
    us_days = [utc(2026, 11, d, 14, 30) for d in (18, 19, 20)]  # 紐約 09:30
    us_after_hours = utc(2026, 11, 21, 1, 0)  # 紐約 11-20 20:00（UTC 已是 11-21）

    charts = {
        "previousClose 優先": chart(
            dict(tw_meta, previousClose=101.5, chartPreviousClose=95, regularMarketPrice=102.0,
                 regularMarketTime=tw_now, shortName="測試一"),
            tw_days, [100.0, 101.0, 102.0]),
        "最後一根是今天取倒數第二根": chart(
            dict(tw_meta, regularMarketPrice=102.0, regularMarketTime=tw_now, chartPreviousClose=95),
            tw_days, [100.0, 101.0, 102.0]),
        "最後一根不是今天取最後一根": chart(
            dict(tw_meta, regularMarketPrice=103.0, regularMarketTime=utc(2026, 11, 23, 1, 30), chartPreviousClose=95),
            tw_days, [100.0, 101.0, 102.0]),
        "只有今天一根改用 chartPreviousClose": chart(
            dict(tw_meta, regularMarketPrice=102.0, regularMarketTime=tw_now, chartPreviousClose=99.0),
            tw_days[-1:], [102.0]),
        "沒有日線改用 chartPreviousClose": chart(
            dict(tw_meta, regularMarketPrice=102.0, regularMarketTime=tw_now, chartPreviousClose=99.0)),
        "收盤含 null 與 0 會略過": chart(
            dict(tw_meta, regularMarketTime=tw_now),
            [utc(2026, 11, 17, 1, 0)] + tw_days, [100.0, None, 0, 102.0]),
        "美股盤後依 gmtoffset 判斷當地日期": chart(
            {"gmtoffset": -18000, "regularMarketPrice": 50.5, "regularMarketTime": us_after_hours,
             "longName": "Fake Long Name"},
            us_days, [48.0, 49.0, 50.0]),
        "字串數字": chart(
            dict(tw_meta, previousClose="88.8", regularMarketPrice="90.1", regularMarketTime=str(tw_now)),
            tw_days, ["87", "88.8", "90.1"]),
        "previousClose 為 0 視為缺值": chart(
            dict(tw_meta, previousClose=0, regularMarketTime=tw_now), tw_days, [100.0, 101.0, 102.0]),
        "沒有 regularMarketPrice 取最後一根": chart(
            dict(tw_meta, regularMarketTime=tw_now), tw_days, [100.0, 101.0, 102.5]),
        "空的 result": {},
    }
    cases = []
    for name, result in charts.items():
        cases.append(case(name, "prevCloseFromChart", [result],
                          lambda r=result: portfolio_data._prev_close_from_chart(r)))
        cases.append(case(name, "yahooQuoteFromChart", [result],
                          lambda r=result: portfolio_data.yahoo_quote_from_chart(r)))
        cases.append(case(name, "dailyBars", [result], lambda r=result: portfolio_data._daily_bars(r)))
    for result in (None,):
        cases.append(case("null result", "prevCloseFromChart", [result],
                          lambda: portfolio_data._prev_close_from_chart(None)))
        cases.append(case("null result", "yahooQuoteFromChart", [result],
                          lambda: portfolio_data.yahoo_quote_from_chart(None)))

    vix_meta = {"gmtoffset": -21600}
    vix_days = [utc(2026, 11, d, 14, 30) for d in (18, 19, 20)]
    vix_charts = {
        "VIX 一般情況": chart(dict(vix_meta, regularMarketPrice=17.234, previousClose=16.785,
                                 regularMarketTime=utc(2026, 11, 20, 21, 15)), vix_days, [18.0, 16.785, 17.234]),
        "VIX 沒有 regularMarketTime 取最後一根日期": chart(dict(vix_meta), vix_days, [18.0, 16.5, 17.25]),
        "VIX 沒有資料": chart({}),
    }
    for name, result in vix_charts.items():
        cases.append(case(name, "vixFromChart", [result], lambda r=result: portfolio_data.vix_from_chart(r)))

    fx_cfg = TEST_CONFIG["fx"]

    def py_fx_info(result):
        with mock.patch.object(portfolio_data, "_yahoo_quote_detail",
                               lambda _t: portfolio_data.yahoo_quote_from_chart(result)):
            return portfolio_data._yahoo_usd_twd_info()

    fx_charts = {
        "匯率合理": chart({"regularMarketPrice": 31.25, "regularMarketTime": tw_now, "gmtoffset": 0}),
        "匯率超出合理範圍": chart({"regularMarketPrice": 3125.0, "regularMarketTime": tw_now}),
        "匯率缺值": chart({}),
        "匯率取最後一根日線": chart({"gmtoffset": 0}, tw_days, [30.1, 30.3, 30.5]),
    }
    for name, result in fx_charts.items():
        cases.append(case(name, "fxInfoFromYahoo", [result, fx_cfg], lambda r=result: py_fx_info(r)))
    for rate in [31.5, 20, 50, 19.99, 50.01, 0, -31.5, None]:
        cases.append(case("fxPlausible " + repr(rate), "fxPlausible", [rate, fx_cfg],
                          lambda rate=rate: portfolio_data._fx_plausible(rate)))

    market_dates = [
        (tw_now, 28800), (us_after_hours, -18000), (str(tw_now), "28800"), (None, 0),
        (tw_now, None), ("abc", 0), (tw_now + 0.9, 0), (1e20, 0),
    ]
    for ts, off in market_dates:
        cases.append(case("marketDate " + repr((ts, off)), "marketDate", [ts, off],
                          lambda ts=ts, off=off: portfolio_data._market_date(ts, off)))
    return cases


# ------------------------------------------------------------------ TWSE / 鉅亨網列解析

def build_quote_rows():
    tickers = ["1111.TW", " 2222.two ", "FAKE", "CASH", "3333.TW", "   ", "twd-cash"]
    cases = [
        case("台股頻道對照（只含台股）", "twseChannelMap", [tickers], lambda: portfolio_data.twse_channel_map(tickers)),
        case("鉅亨網符號對照（排除現金）", "cnyesSymbolMap", [tickers, CASH],
             lambda: portfolio_data.cnyes_symbol_map(tickers)),
    ]
    for t in ["CASH", "twd-cash", " TWD ", "FAKE", "", None, "1111.TW"]:
        cases.append(case("isCash " + repr(t), "isCash", [t, CASH], lambda t=t: portfolio_data.is_cash(t)))
    for t in ["1111.TW", "2222.two", "FAKE", "TW", "1111.TWX", " 3333.TW "]:
        cases.append(case("isTaiwanTicker " + repr(t), "isTaiwanTicker", [t],
                          lambda t=t: portfolio_data.is_taiwan_ticker(t)))

    ms = str(utc(2026, 11, 20, 5, 30) * 1000)
    twse_rows = [
        {"ex": "tse", "ch": "1111.tw", "z": "1050.0000", "b": "1049.0000_1048.0000_", "a": "1051.0000_",
         "y": "1040.0000", "tlong": ms, "n": "測試一"},
        {"ex": "otc", "ch": "2222.tw", "z": "-", "b": "0.0000_55.1000_55.0000_", "a": "55.3000_",
         "y": "54.0000", "tlong": ms, "n": "測試櫃買"},
        {"ex": "tse", "ch": "3333.tw", "z": "0.0000", "b": "-", "a": "0.0000_70.5000_", "y": "70.0000",
         "tlong": "", "n": ""},
        {"ex": "tse", "ch": "4444.tw", "z": "-", "b": "-", "a": "-", "y": "12.3400", "tlong": ms, "n": "未成交"},
        {"ex": "tse", "ch": "9999.tw", "z": "10"},
        "not a dict",
        {"ch": "5555.tw", "z": "1"},
    ]
    twse_map = {"tse_1111.tw": "1111.TW", "otc_2222.tw": "2222.TWO", "tse_3333.tw": "3333.TW",
                "tse_4444.tw": "4444.TW"}
    cases.append(case("證交所列解析（跳過市價單 0.0000、未成交不拿昨收冒充）", "matchTwseRows", [twse_rows, twse_map],
                      lambda: portfolio_data.match_twse_rows(copy.deepcopy(twse_rows), twse_map)))
    cases.append(case("證交所空回應", "matchTwseRows", [[], twse_map], lambda: portfolio_data.match_twse_rows([], twse_map)))

    ts = utc(2026, 11, 20, 21, 0)
    cnyes_rows = [
        {"0": "TWS:1111:STOCK", "6": 1050.0, "21": 1040.0, "200007": ts, "200009": "測試一"},
        {"800013": "USS:FAKE:STOCK", "6": "123.45", "21": "120", "200007": str(ts), "200009": "Fake Corp"},
        {"0": "USS:NOPE:STOCK", "6": 1},
        {"0": 123, "6": 1},
        {"0": "TWS:2222:STOCK", "6": 0, "21": None, "200007": None},
        None,
    ]
    cnyes_map = {"TWS:1111:STOCK": "1111.TW", "USS:FAKE:STOCK": "FAKE", "TWS:2222:STOCK": "2222.TWO"}
    cases.append(case("鉅亨網列解析", "matchCnyesRows", [cnyes_rows, cnyes_map],
                      lambda: portfolio_data.match_cnyes_rows(copy.deepcopy(cnyes_rows), cnyes_map)))
    return cases


# ------------------------------------------------------------------ 樂活五線譜

def business_days(n, start=datetime.date(2023, 1, 2)):
    out = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += datetime.timedelta(days=1)
    return out


def fake_series(n, start=100.0, seed=7):
    """固定種子的假股價（LCG 隨機漫步，四捨五入到小數 2 位，與 get_lohas 的輸入一樣）。"""
    x = seed
    price = start
    out = []
    for _ in range(n):
        x = (1103515245 * x + 12345) % 2147483648
        price = max(1.0, price * (1 + 0.03 * (x / 2147483648 - 0.5)) + 0.02)
        out.append(round(price, 2))
    return out


def build_lohas():
    scenarios = [
        ("window 小於 displayWindow", business_days(120), fake_series(120), 60, 100),
        ("window 等於 displayWindow", business_days(90), fake_series(90, 50.0, 3), 80, 80),
        ("樣本不足標 insufficient", business_days(30), fake_series(30, 20.0, 11), 60, 100),
        ("window 大於 displayWindow", business_days(50), fake_series(50, 300.0, 5), 40, 25),
        ("空資料", [], [], 10, 10),
        ("只有一筆", business_days(1), [42.0], 10, 10),
        ("固定價格 SD 為 0", business_days(20), [50.0] * 20, 10, 15),
        ("完美直線", business_days(30), [10 + 0.5 * i for i in range(30)], 30, 30),
        ("dates 與 closes 長度不同取較短", business_days(12), fake_series(10, 80.0, 9), 8, 9),
        ("整數收盤價", business_days(15), [100 + (i % 4) for i in range(15)], 10, 12),
        ("較大樣本", business_days(400), fake_series(400, 150.0, 2), 300, 350),
    ]
    return [
        case(name, "computeLohas", [dates, closes, w, dw],
             lambda dates=dates, closes=closes, w=w, dw=dw: portfolio_data.compute_lohas(dates, closes, w, dw))
        for name, dates, closes, w, dw in scenarios
    ]


# ------------------------------------------------------------------ Beta 分桶 / 持股彙總

def build_beta_bucket():
    cfg = TEST_CONFIG["betaBuckets"]
    alt = {"cashBeta": 0, "leveragedMin": 1.5}
    scenarios = [
        (True, None, cfg), (True, 1.5, cfg), (False, None, cfg), (False, 0, cfg), (False, 0.0, cfg),
        (False, 1.0, cfg), (False, 1.99, cfg), (False, 2, cfg), (False, 3.0, cfg), (False, -0.5, cfg),
        (False, 1.6, alt), (False, 1.4, alt),
    ]
    return [
        case("betaBucket " + repr((is_cash, beta, cfg["leveragedMin"])), "betaBucket", [is_cash, beta, cfg],
             lambda is_cash=is_cash, beta=beta, cfg=cfg: app.beta_bucket(is_cash, beta, cfg))
        for is_cash, beta, cfg in scenarios
    ]


def _pos(ticker, value, is_taiwan, beta, shares, is_cash=False):
    return {"ticker": ticker, "value": value, "isCash": is_cash, "isTaiwan": is_taiwan, "beta": beta,
            "shares": shares}


def build_portfolio_summary():
    cfg = TEST_CONFIG["betaBuckets"]
    mixed = [
        _pos("1111.TW", 250000.0, True, 1.0, 2500.0),
        _pos("2222.TW", 180000.0, True, 2.0, 1000.0),
        _pos("FAKE", 312500.0, False, 1.15, 100.0),
        _pos("FAKEX", 93750.0, True, None, 30.0),
        _pos("NOPX", None, False, 1.0, 10.0),
        _pos("ZERO.TW", 0.0, True, 1.0, 0.0),
        _pos("CASH", 50000.0, True, None, 50000.0, is_cash=True),
        _pos("BOND", 40000.0, False, 0, 400.0),
    ]
    scenarios = [
        ("混合持股", mixed, 31.25),
        ("沒有匯率", mixed[:4], None),
        ("空清單", [], 31.25),
        ("只有現金", [_pos("CASH", 1000.0, True, None, 1000.0, is_cash=True)], 31.25),
        ("現金填了 Beta", [_pos("CASH", 1000.0, True, 0.5, 1000.0, is_cash=True),
                         _pos("3333.TW", 3000.0, True, 1.0, 30.0)], 30.0),
        ("同市值維持原順序", [_pos("AAA.TW", 500.0, True, 1.0, 5.0), _pos("BBB.TW", 500.0, True, 1.0, 5.0),
                         _pos("CCC", 700.0, False, None, 7.0)], 29.9),
        ("全部缺價", [_pos("NOPX", None, False, 1.0, 10.0), _pos("NOPY.TW", None, True, None, 5.0)], 31.0),
    ]
    return [
        case(name, "summarizePositions", [positions, fx, cfg],
             lambda positions=positions, fx=fx: app.summarize_positions(copy.deepcopy(positions), fx, cfg))
        for name, positions, fx in scenarios
    ]


# ------------------------------------------------------------------ CNN Fear & Greed

def build_fear_greed():
    payloads = [
        ("一般情況", {"fear_and_greed": {"score": 55.4321, "rating": "greed", "timestamp": "2026-11-20T23:59:57+00:00",
                                     "previous_close": 50.06, "previous_1_week": 41.25, "previous_1_month": 60.0,
                                     "previous_1_year": 33.35}}),
        ("分數為字串", {"fear_and_greed": {"score": "12.25", "rating": "Extreme Fear"}}),
        ("分數 0 有效", {"fear_and_greed": {"score": 0, "rating": ""}}),
        ("分數 100 有效", {"fear_and_greed": {"score": 100}}),
        ("分數超過 100", {"fear_and_greed": {"score": 100.04}}),
        ("分數為負", {"fear_and_greed": {"score": -1}}),
        ("缺分數", {"fear_and_greed": {"rating": "fear"}}),
        ("缺 fear_and_greed", {"other": 1}),
        ("payload 為 null", None),
        ("歷史值缺漏或為破折號", {"fear_and_greed": {"score": 45.0, "previous_close": "-", "previous_1_week": None}}),
    ]
    return [
        case(name, "parseFearGreed", [payload], lambda payload=payload: portfolio_data.parse_fear_greed(payload))
        for name, payload in payloads
    ]


# ------------------------------------------------------------------ Forward P/E 解析

OVERVIEW_OK = (
    '<div class="card"><h3>Nasdaq-100 Forward P/E</h3>\n<div class="val">27.35<span>×</span></div>'
    "<p>Forward 12-month estimates</p><small>As of Nov 21, 2026</small><span class=\"chg\">−0.12</span></div>"
)

# 網站實際的 React 伺服器端輸出：相鄰文字節點之間有 <!-- --> 註解，正負號和數字被隔開（數值為虛構）
OVERVIEW_REACT = (
    '<a class="group block" href="/en/valuation/nasdaq-100-forward-pe"><h2>Nasdaq-100 Forward P/E</h2>'
    '<p class="val">27.35<span>×</span></p><p>Forward 12-month estimates</p>'
    '<span>As of<!-- --> <!-- -->Nov 21, 2026</span><span class="chg">+<!-- -->0.05</span></a>'
)


def build_pe_parse():
    cases = []
    overview = [
        ("總覽頁一般情況（U+2212 負號）", OVERVIEW_OK),
        ("總覽頁 x 與正號", OVERVIEW_OK.replace("×", "x").replace("−0.12", "+0.05")),
        ("總覽頁整數與無正負號", OVERVIEW_OK.replace("27.35", "27").replace("−0.12", "0.3")),
        ("總覽頁日期不合法仍回傳數值", OVERVIEW_OK.replace("Nov 21, 2026", "Feb 30, 2026")),
        ("總覽頁缺 As of", OVERVIEW_OK.replace("As of", "Updated")),
        ("總覽頁版面改變", "<html><body>Nothing here</body></html>"),
        ("總覽頁 React 註解隔開正負號（實際版面）", OVERVIEW_REACT),
        ("總覽頁多行註解", OVERVIEW_REACT.replace("<!-- -->", "<!--\n x \n-->")),
        ("總覽頁正負號與數字在不同元素", OVERVIEW_OK.replace("−0.12", "<b>−</b> <b>0.12</b>")),
    ]
    for name, html in overview:
        cases.append(case(name, "parseDollarLiquidityOverview", [html],
                          lambda html=html: portfolio_data._parse_dollarliquidity_overview(html)))

    history = (
        '[{\\"date\\":\\"2026-11-19\\",\\"pe\\":27.01},{\\"date\\":\\"2026-11-20\\",\\"pe\\":27.2},'
        '{\\"date\\":\\"2026-11-21\\",\\"pe\\":27.35}],\\"historyPoints\\":3'
    )
    detail = [
        ("明細頁 Today 與 historyPoints", "<p>Forward P/E Ratio Today: 27.35× (2026-11-21)</p>" + history),
        ("明細頁不分大小寫且沒有 ×", "forward p/e ratio today: 27.35 (2026-11-21) " + history),
        ("明細頁 JSON-LD 備援",
         '{"name": "Forward P/E", "unitText": "ratio", "value": 26.9} {"latestDate": "2026-11-21"}'),
        ("明細頁 JSON-LD 沒有日期", '{"name":"Forward P/E","unitText":"ratio","value":26.9}'),
        ("明細頁 historyPoints 只有一筆",
         "Forward P/E Ratio Today: 27.35× (2026-11-21) [{\"date\":\"2026-11-21\",\"pe\":27.35}],\"historyPoints\""),
        ("明細頁未跳脫的 historyPoints",
         'Forward P/E Ratio Today: 28.1x (2026-11-22) [{"date":"2026-11-20","pe":27.9},'
         '{"date":"2026-11-21","pe":28.0},{"date":"2026-11-22","pe":28.1}],"historyPoints"'),
        ("明細頁沒有任何數值", "<html>maintenance</html>"),
    ]
    for name, text in detail:
        cases.append(case(name, "parseDollarLiquidityDetail", [text],
                          lambda text=text: portfolio_data._parse_dollarliquidity_detail(text)))

    for text in ["Nov 21, 2026", "Feb 29, 2024", "Feb 29, 2025", "Foo 1, 2026", " Nov 1, 2026 ", "Nov 31, 2026",
                 "nov 21, 2026", "Nov 21 2026", "Dec 31, 1999", ""]:
        cases.append(case("englishDate " + repr(text), "englishDate", [text],
                          lambda text=text: portfolio_data._english_date(text)))

    cfg = TEST_CONFIG["forwardPe"]
    for value in [27.35, 7.99, 8, 60, 60.01, None]:
        cases.append(case("pePlausible " + repr(value), "pePlausible", [value, cfg],
                          lambda value=value: portfolio_data._pe_plausible(value)))
    return cases


# ------------------------------------------------------------------ 借貸攤銷（獨立參考實作）
# 規則（README「借貸」）：每月 dueDay 還款（遇小月取月底）；基準日 asOfDate 當天若是還款日算尚未扣款；
# 以基準日剩餘期數 + 月付金反推月利率（年金公式、二分搜尋 100 次）；
# 每經過一個「今天之前」的還款日，本金扣掉當期本金部分；今日本金四捨五入到整數（JS Math.round）。

_YMD = re.compile(r"(\d{4})-(\d{2})-(\d{2})", re.ASCII)
MAX_PAYMENTS = 1200


def ref_parse_date(text):
    match = _YMD.fullmatch(text) if isinstance(text, str) else None
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def ref_valid_due_day(day):
    return (isinstance(day, (int, float)) and not isinstance(day, bool) and math.isfinite(day)
            and float(day).is_integer() and 1 <= day <= 31)


def ref_payment_date(year, month0, due_day):
    y = year + month0 // 12
    m = month0 % 12 + 1
    return datetime.date(y, m, min(int(due_day), calendar.monthrange(y, m)[1]))


def ref_first_payment_month(from_date, due_day):
    m = from_date.month - 1
    return m + 1 if ref_payment_date(from_date.year, m, due_day) < from_date else m


def ref_count_remaining(due_day, maturity_text, from_date):
    maturity = ref_parse_date(maturity_text)
    if maturity is None or not ref_valid_due_day(due_day):
        return 0
    m = ref_first_payment_month(from_date, due_day)
    count = 0
    while count < MAX_PAYMENTS and ref_payment_date(from_date.year, m + count, due_day) <= maturity:
        count += 1
    return count


def ref_solve_rate(principal, payment, periods):
    if periods <= 0 or payment * periods <= principal:
        return 0
    lo, hi = 0.0, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        pv = payment * periods if mid == 0 else payment * (1 - (1 + mid) ** -periods) / mid
        if pv > principal:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def js_math_round(x):
    floor = math.floor(x)
    return floor + 1 if x - floor >= 0.5 else floor


def ref_amortize(loan, today):
    as_of = ref_parse_date(loan.get("asOfDate")) or today
    due = loan.get("dueDay")
    n0 = ref_count_remaining(due, loan.get("maturityDate"), as_of)
    rate = ref_solve_rate(loan["principal"], loan["monthlyPayment"], n0)
    principal = loan["principal"]
    elapsed = 0
    if ref_valid_due_day(due):
        m = ref_first_payment_month(as_of, due)
        while elapsed < n0 and ref_payment_date(as_of.year, m + elapsed, due) < today:
            interest = principal * rate
            principal = max(0, principal - (loan["monthlyPayment"] - interest))
            elapsed += 1
    return {
        "principalToday": js_math_round(principal),
        "remainingToday": ref_count_remaining(due, loan.get("maturityDate"), today),
        "monthlyRate": rate,
        "elapsed": elapsed,
    }


def build_debt():
    d = datetime.date.fromisoformat
    cases = []
    for args in [(2026, 0, 15), (2026, 1, 31), (2028, 1, 30), (2026, 13, 31), (2026, -1, 15), (2026, 11, 31)]:
        cases.append(case("paymentDate " + repr(args), "paymentDate", args,
                          lambda args=args: ref_payment_date(*args).isoformat(), resultType="date"))

    counts = [
        ("還款日當天算一期", 15, "2026-12-15", "2026-11-15"),
        ("還款日已過從下個月起算", 15, "2026-12-15", "2026-11-16"),
        ("31 日遇小月取月底", 31, "2027-03-31", "2026-11-30"),
        ("到期日早於起算日", 15, "2026-01-15", "2026-11-24"),
        ("到期日格式錯誤", 15, "2030/01/15", "2026-11-24"),
        ("到期日不存在", 15, "2030-02-30", "2026-11-24"),
        ("還款日不合法（0）", 0, "2030-01-15", "2026-11-24"),
        ("還款日不合法（小數）", 15.5, "2030-01-15", "2026-11-24"),
        ("還款日不合法（32）", 32, "2030-01-15", "2026-11-24"),
        ("超長期數有上限", 1, "2199-12-31", "2026-11-24"),
    ]
    for name, due, maturity, start in counts:
        cases.append(case("countRemainingPayments " + name, "countRemainingPayments", [due, maturity, start],
                          lambda due=due, maturity=maturity, start=start: ref_count_remaining(due, maturity, d(start)),
                          argTypes=[None, None, "date"]))

    for args in [(1000000, 20000, 60), (100000, 10000, 12), (100000, 5000, 12), (100000, 10000, 0),
                 (500000, 4500.5, 180)]:
        cases.append(case("solveMonthlyRate " + repr(args), "solveMonthlyRate", args,
                          lambda args=args: ref_solve_rate(*args), tol=1e-12))

    base = {"bank": "測試銀行", "principal": 1200000, "monthlyPayment": 25000, "dueDay": 15,
            "maturityDate": "2030-06-15", "asOfDate": "2026-01-10"}
    loans = [
        ("一般攤銷", base, "2026-11-24"),
        ("今天剛好是還款日不算已扣款", base, "2026-02-15"),
        ("還款日隔天算已扣款", base, "2026-02-16"),
        ("沒有基準日視為今天", {k: v for k, v in base.items() if k != "asOfDate"}, "2026-11-24"),
        ("基準日格式錯誤視為今天", dict(base, asOfDate="2026/01/10"), "2026-11-24"),
        ("31 日遇小月取月底", dict(base, dueDay=31, asOfDate="2026-01-31"), "2026-03-01"),
        ("閏年 2 月", dict(base, dueDay=30, asOfDate="2028-01-31", maturityDate="2031-01-30"), "2028-03-01"),
        ("還款日不合法", dict(base, dueDay=0), "2026-11-24"),
        ("已過到期日", dict(base, maturityDate="2026-06-15"), "2027-01-01"),
        ("月付總額不足本金（零利率）", dict(base, monthlyPayment=10000), "2026-11-24"),
        ("今天在基準日之前", base, "2025-12-01"),
    ]
    for name, loan, today in loans:
        cases.append(case("amortizeToToday " + name, "amortizeToToday", [loan, today],
                          lambda loan=loan, today=today: ref_amortize(loan, d(today)),
                          argTypes=[None, "date"], tol=1e-12))
    return cases


# ------------------------------------------------------------------ 其他小工具

def ref_upsert(dates, values, date, value):
    out_dates, out_values = list(dates), list(values)
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if not date or not numeric:
        return {"dates": out_dates, "values": out_values}
    last = out_dates[-1] if out_dates else None
    if last is None or date > last:
        out_dates.append(date)
        out_values.append(value)
    elif date == last:
        out_values[-1] = value
    return {"dates": out_dates, "values": out_values}


def ref_esc(value):
    if value is None:
        return ""
    table = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}
    return "".join(table.get(ch, ch) for ch in str(value))


def ref_freshness(minutes, cfg):
    if minutes is None or minutes >= cfg["alertMinutes"]:
        return "alert"
    if minutes >= cfg["warnMinutes"]:
        return "warn"
    return "fresh"


def _us_eastern_offset(moment):
    """美東 2007 年起的日光節約規則：3 月第二個星期日 02:00 EST 起、11 月第一個星期日 02:00 EDT 止。"""
    y = moment.year
    march = datetime.date(y, 3, 8)
    nov = datetime.date(y, 11, 1)
    dst_start = datetime.datetime(y, 3, 8 + (6 - march.weekday()) % 7, 7)  # 02:00 EST = 07:00 UTC
    dst_end = datetime.datetime(y, 11, 1 + (6 - nov.weekday()) % 7, 6)  # 02:00 EDT = 06:00 UTC
    return -4 if dst_start <= moment < dst_end else -5


def ref_exchange_date(epoch, is_taiwan):
    valid = isinstance(epoch, (int, float)) and not isinstance(epoch, bool) and 946684800 <= epoch <= 4102444800
    if not valid:
        return None
    moment = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=epoch)
    hours = 8 if is_taiwan else _us_eastern_offset(moment)
    return (moment + datetime.timedelta(hours=hours)).date().isoformat()


def build_misc():
    cases = []
    for value, digits in [(0.125, 2), (0.375, 2), (2.5, 0), (3.5, 0), (-0.125, 2), (-2.5, 0), (1.005, 2),
                          (2.675, 2), (0.5, 0), (1.5, 0), (1234.5678, 2), (0.045, 2), (1e-7, 2), (-0.001, 2),
                          (123456789.125, 2), (0.0, 2), (5, 2), (1e21, 2), (12345.6789, 3), (0.00015, 4),
                          (1.23456785, 7)]:
        cases.append(case("round " + repr((value, digits)), "round", [value, digits],
                          lambda value=value, digits=digits: round(value, digits)))

    for values in [[0.1] * 10, [1e100, 1.0, -1e100], [], [1, 2, 3], [0.1, 0.2, 0.3, -0.6]]:
        cases.append(case("compensatedSum " + repr(values)[:40], "compensatedSum", [values],
                          lambda values=values: portfolio_data._compensated_sum(values)))

    dates = ["2026-11-19", "2026-11-20"]
    values = [100.0, 101.0]
    for name, date, value in [("較新日期附加", "2026-11-21", 102.0), ("同一天取代", "2026-11-20", 101.5),
                              ("較舊日期不變", "2026-11-18", 99.0), ("缺值不變", "2026-11-21", None),
                              ("空日期不變", "", 102.0), ("布林值不變", "2026-11-21", True)]:
        cases.append(case("upsertLastPoint " + name, "upsertLastPoint", [dates, values, date, value],
                          lambda date=date, value=value: ref_upsert(dates, values, date, value)))
    cases.append(case("upsertLastPoint 空序列", "upsertLastPoint", [[], [], "2026-11-21", 1.0],
                      lambda: ref_upsert([], [], "2026-11-21", 1.0)))

    for date_str, today in [("2026-11-20", "2026-11-24"), ("2026-11-25", "2026-11-24"), ("2026-02-30", "2026-11-24"),
                            ("20261124", "2026-11-24"), ("2025-11-24", "2026-11-24"), ("2024-02-29", "2024-03-01")]:
        cases.append(case("daysOld " + repr((date_str, today)), "daysOld", [date_str, today],
                          lambda date_str=date_str, today=today: portfolio_data.days_old(
                              date_str, today=datetime.date.fromisoformat(today))))

    for raw in ["1.5", " 2 ", "-", "", None, "abc", "1e3", "1_000.5", True, False, "inf", "nan", "0x10", ".5",
                "5.", "1__0", "_1", "+1.5", "-0", 0, -3.7, "1e400", [], {}, "  "]:
        for js, py in (("toFloat", portfolio_data._to_float), ("toPositive", portfolio_data._to_positive_float),
                       ("toInt", portfolio_data._to_int)):
            cases.append(case(js + " " + repr(raw), js, [raw], lambda raw=raw, py=py: py(copy.deepcopy(raw))))

    for text in ["<b>Tom & \"Jerry\"</b>", "it's", None, "", "純文字", 42]:
        cases.append(case("esc " + repr(text), "esc", [text], lambda text=text: ref_esc(text)))

    fresh_cfg = TEST_CONFIG["freshness"]
    for minutes in [0, 89, 90, 359, 360, None, -5]:
        cases.append(case("freshnessLevel " + repr(minutes), "freshnessLevel", [minutes, fresh_cfg],
                          lambda minutes=minutes: ref_freshness(minutes, fresh_cfg)))

    # 美東時間挑 UTC 04:00–05:00：EDT（UTC−4）與 EST（UTC−5）會落在不同日期，日光節約判斷錯就抓得到
    for epoch, is_tw in [(utc(2026, 11, 20, 17, 0), True), (utc(2026, 11, 20, 15, 59), True),
                         (utc(2026, 11, 21, 3, 0), False), (utc(2026, 7, 1, 3, 30), False),
                         (utc(2026, 7, 1, 4, 30), False), (utc(2026, 3, 8, 4, 30), False),
                         (utc(2026, 3, 9, 4, 30), False), (utc(2026, 11, 1, 4, 30), False),
                         (utc(2026, 11, 2, 4, 30), False), (utc(2026, 11, 20, 5, 30) * 1000, True),
                         (946684799, True), (None, False), ("1795152600", True), (True, False)]:
        cases.append(case("exchangeDateStr " + repr((epoch, is_tw)), "exchangeDateStr", [epoch, is_tw],
                          lambda epoch=epoch, is_tw=is_tw: ref_exchange_date(epoch, is_tw)))

    for value in [1, 0.01, 0, -1, None, True, "1", 1e308]:
        cases.append(case("isValidPrice " + repr(value), "isValidPrice", [value],
                          lambda value=value: portfolio_data._is_valid_price(value)))
    for value in [946684800, 946684799, 4102444800, 4102444801, 1795152600.5, 1795152600000, "1795152600",
                  None, True]:
        cases.append(case("isValidTs " + repr(value), "isValidTs", [value],
                          lambda value=value: portfolio_data._is_valid_ts(value)))
    for raw in ["0.0000_2230.0000_2225.0000_", "-", "", None, "0.0000_0.0000_", "12.5", "abc_3_", "_"]:
        cases.append(case("firstPositiveFromBook " + repr(raw), "firstPositiveFromBook", [raw],
                          lambda raw=raw: portfolio_data._first_positive_from_book(raw)))
    for text in ["2026-11-24", "2024-02-29", "2025-02-29", "2026-13-01", "2026/11/24", "2026-1-5", "", None,
                 " 2026-11-24"]:
        cases.append(case("parseLocalDate " + repr(text), "parseLocalDate", [text],
                          lambda text=text: (ref_parse_date(text) or None) and ref_parse_date(text).isoformat(),
                          resultType="date"))
    return cases


BUILDERS = {
    "consensus": build_consensus,
    "prev_close": build_prev_close,
    "quote_rows": build_quote_rows,
    "lohas": build_lohas,
    "beta_bucket": build_beta_bucket,
    "portfolio_summary": build_portfolio_summary,
    "fear_greed": build_fear_greed,
    "pe_parse": build_pe_parse,
    "debt_amortize": build_debt,
    "misc": build_misc,
}


def build(name):
    with patched_config():
        cases = BUILDERS[name]()
    names = [c["js"] + " / " + c["name"] for c in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError("golden " + name + " 有重複的 case 名稱：" + ", ".join(duplicates))
    return {"_comment": COMMENT, "cases": cases}


def golden_path(name):
    return os.path.join(GOLDEN_DIR, name + ".json")


def dumps(payload):
    """一個 case 一行，diff 時看得出是哪個 case 改變。"""
    lines = ['{"_comment": ' + json.dumps(payload["_comment"], ensure_ascii=False) + ', "cases": [']
    body = [json.dumps(c, ensure_ascii=False, allow_nan=False, separators=(",", ":")) for c in payload["cases"]]
    lines.append(",\n".join(body))
    lines.append("]}")
    return "\n".join(lines) + "\n"


def _diff(actual, expected, tol, path):
    if isinstance(expected, bool) or isinstance(actual, bool):
        ok = type(actual) is type(expected) and actual == expected
        return None if ok else path + ": " + repr(actual) + " != " + repr(expected)
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        if actual == expected:
            return None
        if tol and abs(actual - expected) <= tol * max(abs(actual), abs(expected)):
            return None
        return path + ": " + repr(actual) + " != " + repr(expected)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return path + ": 長度 " + str(len(actual)) + " != " + str(len(expected))
        for i, (a, e) in enumerate(zip(actual, expected)):
            problem = _diff(a, e, tol, path + "[" + str(i) + "]")
            if problem:
                return problem
        return None
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(set(actual) | set(expected)):
            if key not in expected:
                return path + "." + key + ": 多出的欄位"
            if key not in actual:
                return path + "." + key + ": 缺少欄位"
            problem = _diff(actual[key], expected[key], tol, path + "." + key)
            if problem:
                return problem
        return None
    return None if actual == expected else path + ": " + repr(actual)[:80] + " != " + repr(expected)[:80]


def compare(fresh, stored):
    """fresh（剛算出來）與 stored（golden 檔）的差異清單；空清單代表一致。"""
    problems = []
    fresh_cases = {c["js"] + " / " + c["name"]: c for c in fresh["cases"]}
    stored_cases = {c.get("js", "?") + " / " + c.get("name", "?"): c for c in stored.get("cases", [])}
    for key in sorted(set(fresh_cases) | set(stored_cases)):
        if key not in stored_cases:
            problems.append(key + "：golden 缺少此 case")
            continue
        if key not in fresh_cases:
            problems.append(key + "：golden 多出此 case")
            continue
        a, b = fresh_cases[key], stored_cases[key]
        problem = _diff(a, b, a.get("tol"), key)
        if problem:
            problems.append(problem)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description="產生或檢查 tests/golden/*.json")
    parser.add_argument("--check", action="store_true", help="只比對，不寫檔")
    args = parser.parse_args(argv)
    os.makedirs(GOLDEN_DIR, exist_ok=True)
    failed = False
    for name in BUILDERS:
        payload = build(name)
        path = golden_path(name)
        if args.check:
            try:
                with open(path, encoding="utf-8") as handle:
                    stored = json.load(handle)
            except FileNotFoundError:
                stored = {"cases": []}
            problems = compare(payload, stored)
            if problems:
                failed = True
                print(name + "：" + str(len(problems)) + " 項差異")
                for problem in problems[:10]:
                    print("  " + problem)
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dumps(payload))
        print("wrote " + os.path.relpath(path, ROOT) + "（" + str(len(payload["cases"])) + " cases）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

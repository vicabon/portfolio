"""測試用假物件：假 HTTP 回應、假 Yahoo chart、假的靜態頁資料（全部虛構資料）。"""

import calendar
import datetime
import json

import requests


class FakeResponse:
    def __init__(self, payload=None, text=None, status=200):
        self.text = text if text is not None else json.dumps(payload, ensure_ascii=False)
        self.status_code = status

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code) + " error")


class FakeHttp:
    """取代 SESSION.get：完全相同的網址優先，其次前綴；沒有設定的網址一律 ConnectionError（等同連不上）。

    handler 可以是 FakeResponse、例外物件，或 callable(url, **kwargs) 回傳前兩者之一。
    """

    def __init__(self):
        self.exact = {}
        self.prefixes = []
        self.calls = []

    def route(self, url, handler):
        self.exact[url] = handler
        return self

    def route_prefix(self, prefix, handler):
        self.prefixes.append((prefix, handler))
        return self

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        handler = self.exact.get(url)
        if handler is None:
            handler = next((h for p, h in self.prefixes if url.startswith(p)), None)
        if handler is None:
            raise requests.ConnectionError("沒有設定假回應：" + url)
        result = handler(url, **kwargs) if callable(handler) else handler
        if isinstance(result, BaseException):
            raise result
        return result

    def count(self, prefix):
        return sum(1 for url, _ in self.calls if url.startswith(prefix))


def utc(y, mo, d, hh=0, mi=0):
    return calendar.timegm((y, mo, d, hh, mi, 0, 0, 0, 0))


def chart(meta=None, stamps=(), closes=(), splits=None):
    """Yahoo chart API 的 result[0]。splits：{key: {"date", "numerator", "denominator"}}。"""
    result = {"meta": dict(meta or {}), "timestamp": list(stamps), "indicators": {"quote": [{"close": list(closes)}]}}
    if splits is not None:
        result["events"] = {"splits": splits}
    return result


def chart_response(result):
    return FakeResponse({"chart": {"result": [result] if result is not None else [], "error": None}})


# 靜態頁資料：數字刻意挑「不會在其他地方出現」的值，方便檢查 log 有沒有洩漏金額或股數
FAKE_SHARES = 4321.0
FAKE_PRICE = 135.79
FAKE_TOTALS = [987654.0, 987655.0, 987656.0]


def static_data(today=datetime.date(2026, 11, 20)):
    """一份可通過 validate_static 的 window.STATIC_DATA（每次呼叫都回傳新的物件）。"""
    dates = [(today - datetime.timedelta(days=n)).isoformat() for n in (2, 1, 0)]
    day = dates[-1]
    return {
        "generatedAt": day + "T14:17:00+08:00",
        "portfolio": {
            "positions": [
                {"ticker": "9901.TW", "shares": FAKE_SHARES, "price": FAKE_PRICE, "isCash": False,
                 "isTaiwan": True, "beta": 1.0, "source": "twse", "conflict": False, "stale": False},
                {"ticker": "FAKE", "shares": 2.0, "price": 300.0, "isCash": False, "isTaiwan": False,
                 "beta": 2.0, "source": "yahoo", "conflict": False, "stale": False},
                {"ticker": "CASH", "shares": 1000.0, "price": 1.0, "isCash": True, "isTaiwan": True, "beta": 0.0},
            ],
            "totalValue": FAKE_TOTALS[-1],
            "missingPrice": [],
            "betaUnknown": [],
            "fx": {"value": 30.0, "source": "yahoo", "asOf": None, "isFallback": False},
        },
        "history": {
            "dates": dates,
            "totalValue": list(FAKE_TOTALS),
            "totalValue2025": [1000.0, 1010.0, 1005.0],
            "holdings2025": [],
            "backfilled": [],
            "missing": [],
            "fxApproximated": False,
        },
        "lohas": {
            "tickers": ["9901.TW", "FAKE"],
            "charts": {
                "9901.TW": {"ticker": "9901.TW", "dates": dates, "closes": [100.0, 101.0, 100.5]},
                "FAKE": {"ticker": "FAKE", "chartData": [{"date": d, "close": c} for d, c in zip(dates, [10.0, 10.2, 10.1])]},
            },
            "failed": [],
            "fearGreed": {"score": 55.0, "rating": "neutral", "timestamp": day + "T12:00:00+00:00"},
            "marketIndicators": {
                "vix": {"value": 18.0, "date": day},
                "nasdaq100ForwardPe": {"dollarliquidity": {"value": 27.0, "date": day, "isFallback": False}},
            },
        },
    }

"""資料層：即時報價、匯率、歷史回溯、樂活五線譜、市場指標。

環境限制：公司 SSL 代理會讓 yfinance（curl_cffi）握手失敗，但標準 requests
可正常連到 Yahoo chart API、台灣證交所與鉅亨網。因此本模組一律以 requests 實作：

- 台股/台灣 ETF：三源交叉比對（TWSE MIS + 鉅亨網 Anue + Yahoo Finance）。
- 美股/美股 ETF：雙源交叉比對（Yahoo Finance + 鉅亨網 Anue）。
  每個來源都回傳 {price, prevClose, asOf}；沒有即時價時 price 為 None（不拿昨收冒充），
  由 resolve_consensus() 決定採用哪個價格（規則見該函式說明）。
- 匯率：Yahoo TWD=X 優先、Google Finance 備援；兩者都失敗才用 config.json 的
  fx.fallbackRate，並標記 isFallback。（靜態頁的瀏覽器即時刷新只有 Yahoo 一源——
  Google Finance 不在 Worker 白名單——失敗時沿用快照匯率。）
- 歷史：Yahoo chart API 日收盤價；股票分割依 splits.json 與 Yahoo events=split 自動還原；
  美股以歷史匯率換算台幣。
- 共用常數（額外五線譜清單、門檻、Beta 分桶規則）集中在 config.json，
  前端透過 window.APP_CONFIG 取得同一份設定。

所有對外 HTTP 請求都經過 _get()：共用 Session、遇 429/5xx 以指數退避重試，
並統計各來源成功/失敗次數（get_source_stats()，只有次數、沒有金額）。
"""

import copy
import datetime
import json
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SPLITS_FILE = os.path.join(BASE_DIR, "splits.json")

# 台北時間（固定 UTC+8，台灣不實施日光節約時間）
TPE = datetime.timezone(datetime.timedelta(hours=8))

# ------------------------------------------------------------------ 設定

_REQUIRED_CONFIG_PATHS = (
    "extraLohasTickers",
    "cashTickers",
    "consensus.twoSourceMaxDiff",
    "consensus.staleGapSeconds",
    "lohas.window",
    "lohas.displayWindow",
    "lohas.start",
    "betaBuckets.cashBeta",
    "betaBuckets.leveragedMin",
    "fx.fallbackRate",
    "fx.min",
    "fx.max",
    "forwardPe.min",
    "forwardPe.max",
    "forwardPe.staleDays",
    "freshness.warnMinutes",
    "freshness.alertMinutes",
    "history.start",
    "validation.historyMaxJump",
    "validation.lohasMaxJump",
    "validation.indicatorMaxAgeDays",
)


def _strip_comments(obj):
    """移除以底線開頭的鍵（_comment 等說明文字），不注入前端。"""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not str(k).startswith("_")}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return obj


def load_config(path=CONFIG_FILE):
    """讀取 config.json（必要檔案；缺檔或缺欄位直接拋錯，避免無聲套用錯誤預設值）。"""
    with open(path, "r", encoding="utf-8") as handle:
        cfg = _strip_comments(json.load(handle))
    missing = []
    for dotted in _REQUIRED_CONFIG_PATHS:
        node = cfg
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                missing.append(dotted)
                break
            node = node[part]
    if missing:
        raise ValueError("config.json 缺少設定：" + ", ".join(missing))
    return cfg


CONFIG = load_config()


def public_config():
    """注入前端的設定（window.APP_CONFIG）。只含門檻與清單，沒有任何個人資料。"""
    return copy.deepcopy(CONFIG)


# ------------------------------------------------------------------ HTTP

# 部分來源（CNN、Google Finance）會拒絕非瀏覽器的 User-Agent，因此沿用瀏覽器 UA。
# 只讀取公開頁面/端點，請求頻率由報價快取、批次查詢與排程控制（見 README「資料來源」）。
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}
_MAX_WORKERS = 6  # 並行請求上限（Yahoo 並行過多容易被限流）


def _make_session():
    session = requests.Session()
    retry = Retry(
        total=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,  # 重試用完後交給 raise_for_status() 拋錯
        respect_retry_after_header=False,  # 避免上游給極長的 Retry-After 卡住建置
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(_HEADERS)
    return session


SESSION = _make_session()

_stats_lock = threading.Lock()
_SOURCE_STATS = {}


def _record(source, outcome):
    """outcome：ok（HTTP 成功）/ fail（連線或 HTTP 錯誤）/ empty（HTTP 成功但資料不可用）。"""
    with _stats_lock:
        stats = _SOURCE_STATS.setdefault(source, {"ok": 0, "fail": 0, "empty": 0})
        stats[outcome] += 1


def get_source_stats():
    """各來源請求次數統計（只有次數，不含任何金額）。"""
    with _stats_lock:
        return {k: dict(v) for k, v in sorted(_SOURCE_STATS.items())}


def reset_source_stats():
    with _stats_lock:
        _SOURCE_STATS.clear()


def _get(url, source, timeout=10, **kwargs):
    """GET + 狀態碼檢查 + 來源統計。失敗時拋出例外，由呼叫端決定缺值行為。"""
    try:
        resp = SESSION.get(url, timeout=timeout, **kwargs)
        resp.raise_for_status()
    except Exception:
        _record(source, "fail")
        raise
    _record(source, "ok")
    return resp


# ------------------------------------------------------------------ 共用小工具

_CASH_TICKERS = frozenset(str(t).strip().upper() for t in CONFIG["cashTickers"])
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# 合理的報價時間範圍（2000-01-01 ~ 2100-01-01，epoch 秒）；用來擋掉毫秒或亂碼時間戳
_TS_MIN, _TS_MAX = 946684800, 4102444800


def is_taiwan_ticker(ticker):
    """判斷是否為台股/台灣 ETF（Yahoo 慣例：.TW / .TWO）。"""
    return str(ticker or "").strip().upper().endswith((".TW", ".TWO"))


def is_cash(ticker):
    """判斷是否為現金部位（價格恆為 1 台幣，無報價來源；清單見 config.json cashTickers）。"""
    return str(ticker or "").strip().upper() in _CASH_TICKERS


def _to_float(value):
    try:
        if value in (None, "", "-"):
            return None
        val = float(value)
        return val if math.isfinite(val) else None
    except (TypeError, ValueError):
        return None


def _to_positive_float(value):
    """僅接受大於 0 的有限浮點數，濾除 TWSE 市價單編碼 0.0000 或異常 0 值。"""
    val = _to_float(value)
    return val if (val is not None and val > 0) else None


def _to_int(value):
    val = _to_float(value)
    return int(val) if val is not None else None


def _is_valid_price(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _is_valid_ts(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and _TS_MIN <= value <= _TS_MAX
    )


def _first_positive_from_book(raw):
    """從 TWSE 五檔字串（例如 '0.0000_2230.0000_2225.0000_'）取得第一個 > 0 的價位。"""
    if not raw or raw == "-":
        return None
    for part in str(raw).split("_"):
        val = _to_positive_float(part)
        if val is not None:
            return val
    return None


def _twse_channel(ticker):
    """把代號轉成 TWSE MIS 頻道字串。

    2330.TW  -> tse_2330.tw （上市）
    6488.TWO -> otc_6488.tw （上櫃）
    """
    code = ticker.split(".")[0]
    if ticker.upper().endswith(".TWO"):
        return "otc_" + code + ".tw"
    return "tse_" + code + ".tw"


def _cnyes_symbol(ticker):
    """把代號轉成鉅亨網 Anue API 符號。

    2330.TW / 6488.TWO -> TWS:2330:STOCK / TWS:6488:STOCK
    QQQM               -> USS:QQQM:STOCK
    """
    upper = ticker.strip().upper()
    if is_taiwan_ticker(upper):
        return "TWS:" + upper.split(".")[0] + ":STOCK"
    return "USS:" + upper + ":STOCK"


def _market_date(ts, gmtoffset=0):
    """epoch 秒 → 交易所當地日期 YYYY-MM-DD（Yahoo 的 gmtoffset：台股 28800、美東 -18000）。"""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + int(gmtoffset or 0)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _date_to_ts(date_str):
    return int(time.mktime(time.strptime(date_str, "%Y-%m-%d")))


def today_tpe():
    return datetime.datetime.now(TPE).date()


def days_old(date_str, today=None):
    """YYYY-MM-DD 距今（台北日期）幾天；格式不對回傳 None。"""
    if not isinstance(date_str, str) or not _DATE_RE.fullmatch(date_str):
        return None
    try:
        date = datetime.date.fromisoformat(date_str)
    except ValueError:
        return None
    return ((today or today_tpe()) - date).days


def safe_result(future, default, label):
    try:
        return future.result()
    except Exception as error:  # 個別來源失敗不影響其他來源
        print("[" + label + "] 失敗：" + str(error))
        return default


# ---------------------------------------------------------------- Yahoo chart

_YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_YAHOO_CHART_FALLBACK = "https://query2.finance.yahoo.com/v8/finance/chart/{sym}"


def _yahoo_chart(ticker, rng=None, interval="1d", period1=None, period2=None, events=None):
    """回傳 Yahoo chart API 的 result[0]，失敗自動切換 query2 備援。"""
    params = {"interval": interval}
    if period1 is not None:
        # 指定起訖時間戳可取得每日 granularity（range=max 會被降頻成月）
        params["period1"] = int(period1)
        params["period2"] = int(period2 or time.time())
    else:
        params["range"] = rng or "5d"
    if events:
        params["events"] = events
    last_error = None
    for tmpl in (_YAHOO_CHART, _YAHOO_CHART_FALLBACK):
        try:
            resp = _get(tmpl.format(sym=ticker), "yahoo", params=params, timeout=(5, 10))
            results = (resp.json().get("chart") or {}).get("result") or []
            if results and isinstance(results[0], dict):
                return results[0]
            _record("yahoo", "empty")
            last_error = "回應沒有資料"
        except Exception as error:
            last_error = error
    print("[yahoo] chart 失敗 " + ticker + "：" + str(last_error))
    return None


def _chart_closes(result):
    quote = (((result or {}).get("indicators") or {}).get("quote") or [{}])[0] or {}
    return quote.get("close") or []


def _daily_bars(result):
    """[(交易所當地日期, 收盤價, 時間戳)]，只保留 > 0 的收盤價。"""
    meta = (result or {}).get("meta") or {}
    offset = meta.get("gmtoffset") or 0
    bars = []
    for ts, close in zip((result or {}).get("timestamp") or [], _chart_closes(result)):
        val = _to_positive_float(close)
        date_str = _market_date(ts, offset) if val is not None else None
        if date_str:
            bars.append((date_str, val, int(ts)))
    return bars


def _prev_close_from_chart(result):
    """前一交易日收盤價。

    順序：meta.previousClose → 日線倒數第二根（最後一根是今天時）或最後一根
    → meta.chartPreviousClose（最後手段）。
    注意 range=5d 時 chartPreviousClose 是「圖表區間開始前」的收盤，也就是約 5 個交易日前，
    不能優先使用。前端 core.js 的 prevCloseFromChart 實作同一套規則（golden test 約束）。
    """
    meta = (result or {}).get("meta") or {}
    prev = _to_positive_float(meta.get("previousClose"))
    if prev is not None:
        return prev
    bars = _daily_bars(result)
    if bars:
        today = _market_date(meta.get("regularMarketTime"), meta.get("gmtoffset"))
        if bars[-1][0] != today:
            return bars[-1][1]
        if len(bars) >= 2:
            return bars[-2][1]
    return _to_positive_float(meta.get("chartPreviousClose"))


def yahoo_quote_from_chart(result):
    """Yahoo chart result → {"price", "prevClose", "asOf", "name"}（前端 core.js yahooQuoteFromChart 同一套）。"""
    if not result:
        return {"price": None, "prevClose": None, "asOf": None, "name": None}
    meta = result.get("meta") or {}
    price = _to_positive_float(meta.get("regularMarketPrice"))
    as_of = _to_int(meta.get("regularMarketTime"))
    if price is None:
        bars = _daily_bars(result)
        if bars:
            price, as_of = bars[-1][1], bars[-1][2]
    return {
        "price": price,
        "prevClose": _prev_close_from_chart(result),
        "asOf": as_of,
        "name": meta.get("shortName") or meta.get("longName") or None,
    }


def _yahoo_quote_detail(ticker):
    """Yahoo 即時報價。回傳 {"price", "prevClose", "asOf", "name"}。"""
    return yahoo_quote_from_chart(_yahoo_chart(ticker, rng="5d"))


# ------------------------------------------------------------------ 股票分割

# 分割偵測窗口：Yahoo 的分割事件日與價格實際跳動日可能相差 1～2 週
# （實測 00662.TW 事件記在 2026-11-17、價格在 11-09 就已是分割後水準；
#  0052.TW 事件記在 2025-11-19、價格在 11-17 跳動）。
_SPLIT_WINDOW_BEFORE_DAYS = 21
_SPLIT_WINDOW_AFTER_DAYS = 7
_SPLIT_RATIO_TOLERANCE = 1.35  # 跳動倍數容許落在 ratio / 1.35 ~ ratio × 1.35


def load_splits(path=SPLITS_FILE):
    """讀取 splits.json → {ticker: [(date, ratio), ...]}。格式錯誤直接拋錯（CI 建置失敗）。"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        items = json.load(handle)
    if not isinstance(items, list):
        raise ValueError("splits.json 格式錯誤：最外層必須是陣列")
    out = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("splits.json 格式錯誤：" + repr(item))
        ticker = str(item.get("ticker") or "").strip().upper()
        date_str = str(item.get("date") or "")
        ratio = _to_positive_float(item.get("ratio"))
        valid_date = bool(_DATE_RE.fullmatch(date_str))
        if valid_date:
            try:
                datetime.date.fromisoformat(date_str)
            except ValueError:
                valid_date = False
        if not ticker or not valid_date or ratio is None or ratio == 1:
            raise ValueError("splits.json 格式錯誤：" + repr(item))
        out.setdefault(ticker, []).append((date_str, ratio))
    return out


SPLITS = load_splits()


def _yahoo_split_events(result):
    """Yahoo chart events=split → [(date, ratio)]；ratio = numerator / denominator。"""
    meta = (result or {}).get("meta") or {}
    events = (((result or {}).get("events") or {}).get("splits")) or {}
    out = []
    for event in events.values() if isinstance(events, dict) else []:
        num = _to_positive_float((event or {}).get("numerator"))
        den = _to_positive_float((event or {}).get("denominator"))
        date_str = _market_date((event or {}).get("date"), meta.get("gmtoffset"))
        if num and den and date_str and num != den:
            out.append((date_str, num / den))
    return out


def apply_splits(dates, closes, splits):
    """把分割前的收盤價換算成分割後的單位（除以 ratio）。

    Yahoo 有時已自動還原、有時沒有，事件日期也不一定等於價格跳動日，
    所以不依日期硬切：在 [事件日 − 21 天, 事件日 + 7 天] 內找
    「前一日收盤 / 當日收盤 ≈ ratio」的那一天，只還原那天之前的價格；
    找不到就代表資料已還原，維持不變。因此重複套用同一事件（splits.json 與
    Yahoo 事件重複）也不會重複還原。
    """
    out = [float(c) for c in closes]
    for split_date, ratio in splits or ():
        try:
            center = datetime.date.fromisoformat(split_date)
        except (TypeError, ValueError):
            continue
        lo = (center - datetime.timedelta(days=_SPLIT_WINDOW_BEFORE_DAYS)).isoformat()
        hi = (center + datetime.timedelta(days=_SPLIT_WINDOW_AFTER_DAYS)).isoformat()
        r_lo, r_hi = sorted((ratio / _SPLIT_RATIO_TOLERANCE, ratio * _SPLIT_RATIO_TOLERANCE))
        best = None
        for i in range(1, len(out)):
            if not (lo <= dates[i] <= hi) or out[i - 1] <= 0 or out[i] <= 0:
                continue
            jump = out[i - 1] / out[i]
            if r_lo <= jump <= r_hi:
                score = abs(math.log(jump / ratio))
                if best is None or score < best[0]:
                    best = (score, i)
        if best is not None:
            for j in range(best[1]):
                out[j] = out[j] / ratio
    return out


def _yahoo_history(ticker, start_ts):
    """回傳 {date_str: close_float}（交易所當地日期，已還原股票分割）。"""
    result = _yahoo_chart(
        ticker, period1=start_ts, period2=int(time.time()), interval="1d", events="split"
    )
    if not result:
        return {}
    meta = result.get("meta") or {}
    offset = meta.get("gmtoffset") or 0
    by_date = {}
    for ts, close in zip(result.get("timestamp") or [], _chart_closes(result)):
        val = _to_positive_float(close)
        if val is None or ts is None or ts < start_ts:
            continue
        date_str = _market_date(ts, offset)
        if date_str:
            by_date[date_str] = val
    if not by_date:
        return {}
    dates = sorted(by_date)
    closes = [by_date[d] for d in dates]
    splits = list(SPLITS.get(ticker.strip().upper(), ())) + _yahoo_split_events(result)
    if splits:
        closes = apply_splits(dates, closes, splits)
    return dict(zip(dates, closes))


# ------------------------------------------------------------------ 匯率

_fx_lock = threading.Lock()
_fx_cache = {"info": None, "ts": 0.0}
_FX_TTL_SECONDS = 300
_FX_FALLBACK_TTL_SECONDS = 60  # 保底值只快取 1 分鐘，盡快重試真實來源
_GOOGLE_FX_URL = "https://www.google.com/finance/quote/USD-TWD"


def _fx_plausible(rate):
    return rate is not None and CONFIG["fx"]["min"] <= rate <= CONFIG["fx"]["max"]


def _yahoo_usd_twd_info():
    detail = _yahoo_quote_detail("TWD=X")
    if _fx_plausible(detail["price"]):
        return {"value": detail["price"], "source": "yahoo", "asOf": detail["asOf"], "isFallback": False}
    return None


def _google_finance_usd_twd_info():
    try:
        resp = _get(_GOOGLE_FX_URL, "google-fx", timeout=8)
    except Exception as error:
        print("[fx] Google Finance 匯率取得失敗：" + str(error))
        return None
    for pattern in (
        r'data-last-price="([\d.]+)"',
        r'"USD / TWD"[^}]*?\[(3[0-9]\.\d+)',
        r'class="YMlKec fxKbKc">([\d.]+)',
    ):
        match = re.search(pattern, resp.text)
        if match:
            rate = _to_positive_float(match.group(1))
            if _fx_plausible(rate):
                return {"value": rate, "source": "google", "asOf": int(time.time()), "isFallback": False}
    _record("google-fx", "empty")
    print("[fx] Google Finance 頁面解析不到合理匯率")
    return None


def get_usd_twd_rate_info():
    """USD -> TWD 匯率：{"value", "source", "asOf", "isFallback"}。

    順序 Yahoo TWD=X → Google Finance → config.json fx.fallbackRate（isFallback=True，頁面標示「匯率暫缺」）。
    """
    with _fx_lock:
        now = time.time()
        info = _fx_cache["info"]
        if info is not None:
            ttl = _FX_FALLBACK_TTL_SECONDS if info["isFallback"] else _FX_TTL_SECONDS
            if now - _fx_cache["ts"] < ttl:
                return dict(info)
        info = _yahoo_usd_twd_info() or _google_finance_usd_twd_info()
        if info is None:
            print("[fx] 匯率來源皆失敗，暫以 fallbackRate 估算")
            info = {
                "value": float(CONFIG["fx"]["fallbackRate"]),
                "source": "fallback",
                "asOf": None,
                "isFallback": True,
            }
        _fx_cache["info"] = info
        _fx_cache["ts"] = now
        return dict(info)


def get_usd_twd_rate():
    """只要匯率數值時使用；需要來源/保底標記請用 get_usd_twd_rate_info()。"""
    return get_usd_twd_rate_info()["value"]


# ------------------------------------------------------------------ 報價來源

_TWSE_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_TWSE_BATCH = 20  # 每次查詢的頻道數（ex_ch 以 | 串接）
_CNYES_QUOTES = "https://ws.api.cnyes.com/ws/api/v1/quote/quotes/{syms}"


def _parse_twse_row(row):
    """TWSE MIS 一列 → {price|None, prevClose, asOf, name}。

    price：最新成交價 z → 最佳買價 b → 最佳賣價 a（都跳過市價單編碼 0.0000）；
    都沒有就是 None（尚未成交），不拿昨收 y 冒充。
    """
    price = (
        _to_positive_float(row.get("z"))
        or _first_positive_from_book(row.get("b"))
        or _first_positive_from_book(row.get("a"))
    )
    tlong = _to_float(row.get("tlong"))
    return {
        "price": price,
        "prevClose": _to_positive_float(row.get("y")),
        "asOf": int(tlong / 1000) if tlong else None,
        "name": row.get("n") or None,
    }


def twse_channel_map(tickers):
    """{TWSE 頻道: 代號}（只含台股；core.js twseChannelMap 同一套）。"""
    out = {}
    for ticker in tickers or []:
        upper = ticker.strip().upper()
        if is_taiwan_ticker(upper):
            out[_twse_channel(upper)] = upper
    return out


def _twse_quotes(tickers):
    """批次 TWSE MIS 即時報價 → {ticker: {price|None, prevClose, asOf, name}}。"""
    items = list(twse_channel_map(tickers).items())
    out = {}
    for i in range(0, len(items), _TWSE_BATCH):
        chunk = dict(items[i:i + _TWSE_BATCH])
        try:
            resp = _get(
                _TWSE_URL,
                "twse",
                params={
                    "ex_ch": "|".join(chunk),
                    "json": "1",
                    "delay": "0",
                    "_": int(time.time() * 1000),
                },
                headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"},
                timeout=8,
            )
            rows = resp.json().get("msgArray") or []
        except Exception as error:
            print("[tw] TWSE 即時報價失敗：" + str(error))
            continue
        if not rows:
            _record("twse", "empty")
        out.update(match_twse_rows(rows, chunk))
    return out


def match_twse_rows(rows, channel_map):
    """TWSE msgArray → {ticker: 解析結果}；以 ex + "_" + ch 對回查詢頻道（前端 core.js matchTwseRows 同一套）。"""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        ticker = channel_map.get(str(row.get("ex")) + "_" + str(row.get("ch")))
        if ticker:
            out[ticker] = _parse_twse_row(row)
    return out


def _parse_cnyes_row(row):
    """鉅亨網一列 → {price|None, prevClose, asOf, name}。

    欄位：6 = 最新價、21 = 昨收、200007 = 報價時間（epoch 秒）、200009 = 名稱。
    """
    return {
        "price": _to_positive_float(row.get("6")),
        "prevClose": _to_positive_float(row.get("21")),
        "asOf": _to_int(row.get("200007")),
        "name": row.get("200009") or None,
    }


def cnyes_symbol_map(tickers):
    """{鉅亨網符號: 代號}（排除現金；core.js cnyesSymbolMap 同一套）。"""
    out = {}
    for ticker in tickers or []:
        upper = ticker.strip().upper()
        if upper and not is_cash(upper):
            out[_cnyes_symbol(upper)] = upper
    return out


def _cnyes_quotes(tickers):
    """批次向鉅亨網 Anue API 抓取即時報價（台股上市/上櫃與美股）。"""
    sym_to_ticker = cnyes_symbol_map(tickers)
    if not sym_to_ticker:
        return {}
    try:
        resp = _get(_CNYES_QUOTES.format(syms=",".join(sym_to_ticker)), "cnyes", timeout=8)
        rows = resp.json().get("data") or []
    except Exception as error:
        print("[cnyes] 鉅亨網即時報價失敗：" + str(error))
        return {}
    if not rows:
        _record("cnyes", "empty")
    return match_cnyes_rows(rows, sym_to_ticker)


def match_cnyes_rows(rows, symbol_map):
    """鉅亨網 data → {ticker: 解析結果}；以欄位 "0"（或 "800013"）的符號對回代號（core.js matchCnyesRows 同一套）。"""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = row.get("0") or row.get("800013")
        ticker = symbol_map.get(symbol) if isinstance(symbol, str) else None
        if ticker:
            out[ticker] = _parse_cnyes_row(row)
    return out


# ------------------------------------------------------------------ 共識報價

def _rel_diff(p1, p2):
    return abs(p1 - p2) / max(p1, p2)


def resolve_consensus(quotes, priority, max_diff=None, stale_gap=None):
    """多源股價共識。

    quotes: {來源: {"price": float|None, "prevClose": float|None, "asOf": epoch 秒|None}}
    規則（前端 core.js 的 resolveConsensus 實作同一套，由 tests/golden/consensus.json 約束）：
      1. 只有 price > 0 的來源能投票；沒有即時價的來源不投票，也不拿昨收冒充。
      2. 至少兩個來源有 asOf 時，比最新來源舊超過 staleGapSeconds 的來源不投票。
      3. 剩 1 源 → 採用；≥ 3 源 → 中位數（任兩源都不一致時標 conflict）。
      4. 恰 2 源：差距 ≤ twoSourceMaxDiff → 依 priority；否則採 asOf 較新者並標 conflict。
      5. 沒有任何即時價 → 依 priority 取昨收並標 stale（頁面顯示「沿用昨收」）。
    回傳 {"price", "source", "conflict", "stale", "sources": {來源: 價格}, "asOf"}
    """
    cfg = CONFIG["consensus"]
    max_diff = cfg["twoSourceMaxDiff"] if max_diff is None else max_diff
    stale_gap = cfg["staleGapSeconds"] if stale_gap is None else stale_gap
    quotes = {src: q for src, q in (quotes or {}).items() if isinstance(q, dict)}
    order = [s for s in priority if s in quotes] + sorted(s for s in quotes if s not in priority)
    valid = [s for s in order if _is_valid_price(quotes[s].get("price"))]
    result = {
        "price": None,
        "source": None,
        "conflict": False,
        "stale": False,
        "sources": {s: quotes[s]["price"] for s in valid},
        "asOf": None,
    }

    def as_of(src):
        ts = quotes[src].get("asOf")
        return ts if _is_valid_ts(ts) else None

    dated = [as_of(s) for s in valid if as_of(s) is not None]
    if len(dated) >= 2:
        newest = max(dated)
        valid = [s for s in valid if as_of(s) is None or newest - as_of(s) <= stale_gap]

    def pick(src, conflict=False):
        result.update(price=quotes[src]["price"], source=src, conflict=conflict, asOf=as_of(src))
        return result

    if not valid:
        for src in order:
            prev = quotes[src].get("prevClose")
            if _is_valid_price(prev):
                result.update(price=prev, source=src + ":prevClose", stale=True)
                break
        return result
    if len(valid) == 1:
        return pick(valid[0])
    if len(valid) >= 3:
        ranked = sorted(valid, key=lambda s: quotes[s]["price"])
        prices = [quotes[s]["price"] for s in valid]
        agree = any(
            _rel_diff(prices[i], prices[j]) <= max_diff
            for i in range(len(prices))
            for j in range(i + 1, len(prices))
        )
        return pick(ranked[len(ranked) // 2], conflict=not agree)
    first, second = valid[0], valid[1]
    if _rel_diff(quotes[first]["price"], quotes[second]["price"]) <= max_diff:
        return pick(first)
    newest_src = max((first, second), key=lambda s: as_of(s) or 0)
    return pick(newest_src, conflict=True)


def _quote_meta(consensus):
    return {
        "source": consensus["source"],
        "conflict": consensus["conflict"],
        "stale": consensus["stale"],
        "sources": consensus["sources"],
        "asOf": consensus["asOf"],
    }


TW_PRIORITY = ("twse", "cnyes", "yahoo")
US_PRIORITY = ("yahoo", "cnyes")


def _tw_quote(twse, cnyes, yahoo):
    """台股三源共識（core.js twQuote 同一套）。"""
    twse, cnyes, yahoo = twse or {}, cnyes or {}, yahoo or {}
    consensus = resolve_consensus({"twse": twse, "cnyes": cnyes, "yahoo": yahoo}, priority=TW_PRIORITY)
    quote = {
        "name": twse.get("name") or cnyes.get("name") or yahoo.get("name"),
        "price": consensus["price"],
        "currency": "TWD",
    }
    quote.update(_quote_meta(consensus))
    return quote


def _us_quote(yahoo, cnyes, fx_rate):
    """美股兩源共識，再以匯率換成台幣（core.js usQuote 同一套；sources 內為美元報價）。"""
    yahoo, cnyes = yahoo or {}, cnyes or {}
    consensus = resolve_consensus({"yahoo": yahoo, "cnyes": cnyes}, priority=US_PRIORITY)
    usd = consensus["price"]
    quote = {
        "name": yahoo.get("name") or cnyes.get("name"),
        "price": round(usd * fx_rate, 2) if (usd is not None and fx_rate) else None,
        "priceUsd": usd,
        "currency": "TWD",
        "fxRate": fx_rate,
    }
    quote.update(_quote_meta(consensus))  # sources 內為美元報價
    return quote


def _cash_quote():
    return {
        "name": "台幣現金",
        "price": 1.0,
        "currency": "TWD",
        "source": "cash",
        "conflict": False,
        "stale": False,
        "sources": {},
        "asOf": None,
    }


def _fetch_quotes(tickers):
    """實際抓取：TWSE 與鉅亨網各一次批次查詢，Yahoo 與匯率並行。"""
    priced = [t for t in tickers if not is_cash(t)]
    tw = [t for t in priced if is_taiwan_ticker(t)]
    us = [t for t in priced if not is_taiwan_ticker(t)]
    out = {t: _cash_quote() for t in tickers if is_cash(t)}
    if not priced:
        return out
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        f_cnyes = pool.submit(_cnyes_quotes, priced)
        f_twse = pool.submit(_twse_quotes, tw) if tw else None
        f_fx = pool.submit(get_usd_twd_rate_info) if us else None
        f_yahoo = {t: pool.submit(_yahoo_quote_detail, t) for t in priced}
        cnyes = safe_result(f_cnyes, {}, "cnyes")
        twse = safe_result(f_twse, {}, "twse") if f_twse else {}
        fx = safe_result(f_fx, None, "fx") if f_fx else None
        yahoo = {t: safe_result(f, {}, "yahoo " + t) for t, f in f_yahoo.items()}
    fx_rate = (fx or {}).get("value")
    for t in tw:
        out[t] = _tw_quote(twse.get(t), cnyes.get(t), yahoo.get(t))
    for t in us:
        out[t] = _us_quote(yahoo.get(t), cnyes.get(t), fx_rate)
    return out


_quote_lock = threading.Lock()
_quote_cache = {}  # ticker -> (抓取時間, quote)
_QUOTE_TTL_SECONDS = 20  # Flask 模式 30 秒輪詢 + 多個端點同時查價時共用


def get_quotes(tickers):
    """多檔即時報價（台幣計價；美股另附 priceUsd）。20 秒內重複查詢直接用快取。"""
    unique = list(dict.fromkeys(str(t).strip().upper() for t in tickers if t and str(t).strip()))
    now = time.time()
    out = {}
    need = []
    with _quote_lock:
        for ticker in unique:
            hit = _quote_cache.get(ticker)
            if hit and now - hit[0] < _QUOTE_TTL_SECONDS:
                out[ticker] = copy.deepcopy(hit[1])
            else:
                need.append(ticker)
    if need:
        fresh = _fetch_quotes(need)
        with _quote_lock:
            for ticker, quote in fresh.items():
                if quote.get("price") is not None:  # 失敗的報價不快取，下次直接重試
                    _quote_cache[ticker] = (time.time(), copy.deepcopy(quote))
        out.update(fresh)
    return {t: out[t] for t in unique if t in out}


def get_quote(ticker):
    """單檔即時報價，價格一律回傳台幣。"""
    ticker = str(ticker).strip().upper()
    return get_quotes([ticker]).get(ticker) or {"name": None, "price": None, "currency": "TWD"}


def clear_caches():
    """測試用：清除報價與匯率快取。"""
    with _quote_lock:
        _quote_cache.clear()
    with _fx_lock:
        _fx_cache["info"] = None
        _fx_cache["ts"] = 0.0


# ------------------------------------------------------------------ 歷史回溯

_BACKFILL_REPORT_MIN_DAYS = 7  # 起始日只差幾天（假日差異）不列入「上市前回補」提示


def _extract_shares_map(holdings):
    shares = {}
    for holding in holdings or []:
        qty = _to_float(holding.get("shares"))
        if qty:
            key = holding["ticker"].strip().upper()
            # 同 ticker 可能出現多筆（例如美股 QQQM + 台股複委託 QQQM），
            # 對「總市值走勢」而言報價來源相同，股數應合併加總，不能覆蓋。
            shares[key] = shares.get(key, 0.0) + qty
    return shares


def _align_series(hist, dates):
    """把 {date: value} 對齊到 dates：取「該日或之前最近一筆」，開頭缺值以第一筆回補。全缺回傳 None。"""
    keys = sorted(hist)
    out = {}
    j = 0
    last = None
    for d in dates:
        while j < len(keys) and keys[j] <= d:
            last = hist[keys[j]]
            j += 1
        out[d] = last
    first = next((v for v in out.values() if v is not None), None)
    if first is None:
        return None
    for d in dates:
        if out[d] is not None:
            break
        out[d] = first
    return out


def _fetch_histories(tickers, start_ts):
    if not tickers:
        return {}
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(tickers))) as pool:
        futures = {t: pool.submit(_yahoo_history, t, start_ts) for t in tickers}
        return {t: safe_result(f, {}, "history " + t) for t, f in futures.items()}


def get_history(holdings, start=None, holdings_2025=None):
    """歷史回溯：以「目前持股固定不變」為假設，往回推算每日總市值（台幣）。

    holdings:      [{"ticker": ..., "shares": ...}, ...]
    holdings_2025: 2025portfolio.json 持倉清單（選擇性，同步計算 2025 總資產曲線）
    回傳：
      dates / totalValue / totalValue2025
      holdings2025:   [{"ticker", "shares"}] 供前端即時刷新 2025 曲線最後一點
      backfilled:     [{"ticker", "firstDate"}] 上市（或 Yahoo 有資料）前以首日價格回補的持股：
                      視同持有等值現金、價值不變，避免上市日出現假跳升（頁面會註明）
      missing:        完全抓不到歷史價格的持股（未計入曲線；validate_static.py 會擋下部署）
      fxApproximated: 匯率歷史抓取失敗、改以目前匯率換算美股部位時為 True（頁面會註明）
    """
    shares = _extract_shares_map(holdings)
    shares_2025 = _extract_shares_map(holdings_2025)
    if not shares:
        return _empty_history()

    start_ts = _date_to_ts(start or CONFIG["history"]["start"])
    priced = sorted(t for t in set(shares) | set(shares_2025) if not is_cash(t))
    need_fx = any(not is_taiwan_ticker(t) for t in priced)
    fetched = _fetch_histories(priced + (["TWD=X"] if need_fx else []), start_ts)
    fx_hist = fetched.pop("TWD=X", {}) if need_fx else {}

    close_frames = {t: h for t, h in fetched.items() if h}
    missing = sorted(t for t in priced if t not in close_frames)
    for ticker in missing:
        print("[history] 無歷史資料：" + ticker)

    all_dates = sorted({d for h in close_frames.values() for d in h})
    if not all_dates:
        empty = _empty_history()
        empty["missing"] = missing
        return empty

    first_day = datetime.date.fromisoformat(all_dates[0])
    combined = {}
    backfilled = []
    for ticker, hist in close_frames.items():
        combined[ticker] = _align_series(hist, all_dates)
        first_date = min(hist)
        if (datetime.date.fromisoformat(first_date) - first_day).days > _BACKFILL_REPORT_MIN_DAYS:
            backfilled.append({"ticker": ticker, "firstDate": first_date})
    backfilled.sort(key=lambda item: (item["firstDate"], item["ticker"]))

    fx_aligned = _align_series(fx_hist, all_dates) if (need_fx and fx_hist) else None
    fx_approximated = False
    if need_fx and fx_aligned is None:
        rate = get_usd_twd_rate()
        fx_aligned = {d: rate for d in all_dates}
        fx_approximated = True
        print("[history] 匯率歷史取得失敗，美股部位以目前匯率近似")

    def day_value(share_map, d):
        total = 0.0
        for ticker, qty in share_map.items():
            if is_cash(ticker):
                total += float(qty)
                continue
            series = combined.get(ticker)
            if series is None:
                continue  # 列於 missing
            price = series[d]
            if not is_taiwan_ticker(ticker):
                price *= fx_aligned[d]
            total += price * qty
        return total

    dates, totals, totals_2025 = [], [], []
    for d in all_dates:
        day_total = day_value(shares, d)
        if day_total <= 0:
            continue
        dates.append(d)
        totals.append(round(day_total, 2))
        if shares_2025:
            totals_2025.append(round(day_value(shares_2025, d), 2))

    if not dates:
        return _empty_history()
    return {
        "dates": dates,
        "totalValue": totals,
        "totalValue2025": totals_2025,
        "holdings2025": [{"ticker": k, "shares": v} for k, v in shares_2025.items()],
        "backfilled": backfilled,
        "missing": missing,
        "fxApproximated": fx_approximated,
    }


def _empty_history():
    return {
        "dates": [],
        "totalValue": [],
        "totalValue2025": [],
        "holdings2025": [],
        "backfilled": [],
        "missing": [],
        "fxApproximated": False,
    }


# ------------------------------------------------------------ 樂活五線譜

def _compensated_sum(values):
    """Neumaier 補償加總：與 CPython 3.12+ 內建 sum() 對浮點數的演算法相同（core.js compensatedSum 同一套）。

    明確寫出來是為了讓 CI 的 Python 3.11（內建 sum 是逐項相加）、本機 3.12+ 與瀏覽器重算的五線譜逐位元一致。
    """
    total = 0.0
    comp = 0.0
    for x in values:
        t = total + x
        if abs(total) >= abs(x):
            comp += (total - t) + x
        else:
            comp += (x - t) + total
        total = t
    if comp and math.isfinite(comp):
        total += comp
    return total


def compute_lohas(dates, closes, window=None, display_window=None):
    """樂活五線譜：對最近 window 個交易日收盤價做線性回歸（中軸 TL），
    以殘差的母體標準差 SD 畫 ±1SD、±2SD、-3SD、-4SD 通道，並延伸到最近 display_window 日。

    前端 core.js 的 computeLohas 實作同一套演算法（tests/golden/lohas.json 約束），
    靜態版只傳日期與收盤價，由前端重算通道線。
    回傳 {"chartData": [...], "n": 回歸樣本數, "window", "insufficient": n < window}
    """
    window = int(window or CONFIG["lohas"]["window"])
    display_window = int(display_window or CONFIG["lohas"]["displayWindow"])
    pairs = list(zip(dates, closes))[-display_window:]
    if not pairs:
        return {"chartData": [], "n": 0, "window": window, "insufficient": True}
    shown = [float(c) for _, c in pairs]
    reg = shown[-window:]
    n = len(reg)
    # 平方一律寫成 d * d（不用 ** 2）：libm 的 pow 不保證正確捨入，乘法才與 JS 逐位元一致
    x_mean = (n - 1) / 2.0
    y_mean = _compensated_sum(reg) / n
    num = _compensated_sum((i - x_mean) * (y - y_mean) for i, y in enumerate(reg))
    den = _compensated_sum((i - x_mean) * (i - x_mean) for i in range(n))
    slope = num / den if den else 0.0
    intercept = y_mean - slope * x_mean
    residuals = [y - (slope * i + intercept) for i, y in enumerate(reg)]
    r_mean = _compensated_sum(residuals) / n
    sd = math.sqrt(_compensated_sum((r - r_mean) * (r - r_mean) for r in residuals) / n)

    offset = len(shown) - n
    rows = []
    for i, (date_str, close) in enumerate(pairs):
        tl = slope * (i - offset) + intercept
        rows.append(
            {
                "date": date_str,
                "close": round(float(close), 2),
                "tl": round(tl, 2),
                "p1sd": round(tl + sd, 2),
                "p2sd": round(tl + 2 * sd, 2),
                "m1sd": round(tl - sd, 2),
                "m2sd": round(tl - 2 * sd, 2),
                "m3sd": round(tl - 3 * sd, 2),
                "m4sd": round(tl - 4 * sd, 2),
            }
        )
    return {"chartData": rows, "n": n, "window": window, "insufficient": n < window}


def get_lohas(ticker, start=None, compact=False):
    """單檔樂活五線譜。

    compact=False（Flask /api/lohas）：回傳完整 chartData。
    compact=True（靜態建置）：只回傳 dates / closes，通道線由前端 computeLohas 重算，
    頁面 payload 少約 7 成。兩種格式都帶 n / window / insufficient（樣本不足時頁面會標示）。
    收盤價先四捨五入到小數 2 位再回歸，兩種格式與前端重算的輸入完全相同。
    """
    ticker = ticker.strip().upper()
    if is_cash(ticker):
        return {"error": "現金無五線譜資料", "chartData": []}

    hist = _yahoo_history(ticker, _date_to_ts(start or CONFIG["lohas"]["start"]))
    if not hist:
        return {"error": "找不到 " + ticker + " 的歷史數據", "chartData": []}

    items = sorted(hist.items())
    lohas = compute_lohas([d for d, _ in items], [round(c, 2) for _, c in items])
    rows = lohas["chartData"]
    is_tw = is_taiwan_ticker(ticker)
    data = {
        "ticker": ticker,
        "currencySymbol": "NT$" if is_tw else "$",
        "lastDate": rows[-1]["date"] if rows else None,
        "sentimentDescription": (
            "統計學價格軌道與 CNN 美股市場情緒參考" if is_tw else "統計學價格軌道與美股即時情緒指標"
        ),
        "n": lohas["n"],
        "window": lohas["window"],
        "insufficient": lohas["insufficient"],
    }
    if compact:
        data["dates"] = [r["date"] for r in rows]
        data["closes"] = [r["close"] for r in rows]
    else:
        data["chartData"] = rows
    return data


# ------------------------------------------------------------ CNN Fear & Greed

_CNN_FG_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


def fear_greed_unavailable(message):
    """缺值一律用 null（不再用 50 冒充「中性」），頁面顯示灰色「資料暫缺」且不畫指針。"""
    return {
        "score": None,
        "rating": None,
        "timestamp": None,
        "previousClose": None,
        "previous1Week": None,
        "previous1Month": None,
        "previous1Year": None,
        "error": message,
    }


def _round1(value):
    return round(value, 1) if value is not None else None


def parse_fear_greed(payload):
    """CNN graphdata JSON → 頁面使用的格式；分數缺值或超出 0–100 時拋出 ValueError。"""
    fg = (payload or {}).get("fear_and_greed") or {}
    score = _to_float(fg.get("score"))
    if score is None or not (0 <= score <= 100):
        raise ValueError("score 缺值或超出 0–100")
    return {
        "score": round(score, 1),
        "rating": str(fg.get("rating") or "").upper() or None,
        "timestamp": fg.get("timestamp"),
        "previousClose": _round1(_to_float(fg.get("previous_close"))),
        "previous1Week": _round1(_to_float(fg.get("previous_1_week"))),
        "previous1Month": _round1(_to_float(fg.get("previous_1_month"))),
        "previous1Year": _round1(_to_float(fg.get("previous_1_year"))),
        "error": None,
    }


def get_fear_greed():
    """CNN Fear & Greed 即時分數與歷史比較；失敗回傳 score=None 與錯誤原因。"""
    try:
        resp = _get(_CNN_FG_URL, "cnn", timeout=10)
    except Exception as error:
        print("[fg] CNN Fear & Greed 取得失敗：" + str(error))
        return fear_greed_unavailable("CNN 無回應")
    try:
        return parse_fear_greed(resp.json())
    except Exception as error:
        _record("cnn", "empty")
        print("[fg] CNN Fear & Greed 資料格式異常：" + str(error))
        return fear_greed_unavailable("CNN 資料格式異常")


# ------------------------------------------------------------ 市場指標 (VIX & Nasdaq100 Forward PE)

_DOLLARLIQUIDITY_URL = "https://dollarliquidity.com/en/valuation/nasdaq-100-forward-pe"
_DOLLARLIQUIDITY_OVERVIEW_URL = "https://dollarliquidity.com/en/valuation"


def get_vix():
    """CBOE VIX 波動率指數最新值與前一交易日收盤（Yahoo Finance Chart API）。"""
    result = _yahoo_chart("%5EVIX", rng="5d")
    if not result:
        return vix_unavailable()
    return vix_from_chart(result)


def vix_from_chart(result):
    """VIX chart result → {"value", "previousValue", "date"}（前端 core.js vixFromChart 同一套）。"""
    meta = (result or {}).get("meta") or {}
    bars = _daily_bars(result)
    price = _to_positive_float(meta.get("regularMarketPrice"))
    if price is None and bars:
        price = bars[-1][1]
    date_str = _market_date(meta.get("regularMarketTime"), meta.get("gmtoffset")) if meta.get(
        "regularMarketTime"
    ) else (bars[-1][0] if bars else None)
    prev = _prev_close_from_chart(result)
    return {
        "value": round(price, 2) if price is not None else None,
        "previousValue": round(prev, 2) if prev is not None else None,
        "date": date_str,
    }


def _pe_plausible(value):
    return value is not None and CONFIG["forwardPe"]["min"] <= value <= CONFIG["forwardPe"]["max"]


_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
)}


def _english_date(text):
    """'Nov 21, 2026' → '2026-11-21'（不依賴系統 locale；格式不對回傳 None）。"""
    match = re.fullmatch(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", (text or "").strip())
    if not match or match.group(1) not in _MONTHS:
        return None
    try:
        return datetime.date(int(match.group(3)), _MONTHS[match.group(1)], int(match.group(2))).isoformat()
    except ValueError:
        return None


def _parse_dollarliquidity_overview(html_text):
    """從 DollarLiquidity /en/valuation 總覽頁（較輕量）解析 Nasdaq-100 Forward P/E。

    change 是網站在數值旁顯示的變化值，網站沒有說明比較基準；
    previousValue = value − change 只是推算（previousValueDerived=True），頁面會如實標示。

    網站是 React 伺服器端輸出，相鄰文字節點之間會插入 <!-- -->（例如 "+<!-- -->1.68"）：
    先整段移除註解、再把標籤換成空白；正負號與數字分在不同元素時中間會有空白，同樣接受。
    """
    plain = re.sub(r"<!--.*?-->", "", html_text, flags=re.S)
    plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", plain))
    match = re.search(
        r"Nasdaq-100 Forward P/E\s+(\d+(?:\.\d+)?)\s*[×x].{0,400}?"
        r"As of\s+([A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*([+−-]?)\s*(\d+(?:\.\d+)?)",
        plain,
    )
    if not match:
        return None
    val = _to_float(match.group(1))
    chg = _to_float(match.group(3).replace("−", "-") + match.group(4))
    date_str = _english_date(match.group(2))
    if val is None or chg is None:
        return None
    val, chg = round(val, 2), round(chg, 2)
    return {
        "value": val,
        "date": date_str,
        "previousValue": round(val - chg, 2),
        "previousDate": None,
        "previousValueDerived": True,
        "change": chg,
    }


def _parse_dollarliquidity_detail(text):
    """明細頁備援解析：Today 數值 + historyPoints 倒數第二筆當前值。"""
    prev_val = None
    prev_date = None
    hp_idx = text.find("historyPoints")
    if hp_idx > 0:
        snippet = text[max(0, hp_idx - 1200):hp_idx]
        pairs = re.findall(
            r'\\?"date\\?"\s*:\s*\\?"(\d{4}-\d{2}-\d{2})\\?"\s*,\s*\\?"pe\\?"\s*:\s*([\d.]+)',
            snippet,
        )
        if len(pairs) >= 2:
            prev_date, prev_pe_str = pairs[-2]
            prev_val = _to_float(prev_pe_str)
            if prev_val is None:
                prev_date = None
            else:
                prev_val = round(prev_val, 2)

    val = None
    date_str = None
    match = re.search(
        r"Forward P/E Ratio Today:\s*([\d.]+)\s*[×x]?\s*\((\d{4}-\d{2}-\d{2})\)", text, re.I
    )
    if match:
        val, date_str = _to_float(match.group(1)), match.group(2)
    else:
        match_val = re.search(
            r'"name"\s*:\s*"Forward P/E"\s*,\s*"unitText"\s*:\s*"ratio"\s*,\s*"value"\s*:\s*([\d.]+)',
            text,
        )
        if match_val:
            val = _to_float(match_val.group(1))
            date_match = re.search(r'"latestDate"\s*:\s*"(\d{4}-\d{2}-\d{2})"', text)
            date_str = date_match.group(1) if date_match else None
    if val is None:
        return None
    val = round(val, 2)
    return {
        "value": val,
        "date": date_str,
        "previousValue": prev_val,
        "previousDate": prev_date,
        "previousValueDerived": False,
        "change": round(val - prev_val, 2) if prev_val is not None else None,
    }


_last_good_pe = {"info": None}  # 解析失敗時沿用上次成功值（Flask 長時間執行時有用）


def _finish_pe(info, is_fallback=False, error=None):
    out = dict(info)
    out.update(url=_DOLLARLIQUIDITY_URL, source="DollarLiquidity", isFallback=is_fallback, error=error)
    age = days_old(out.get("date"))
    out["stale"] = out.get("value") is not None and (age is None or age > CONFIG["forwardPe"]["staleDays"])
    return out


def get_nasdaq100_forward_pe_dollarliquidity():
    """從 DollarLiquidity.com 取得 Nasdaq-100 Forward P/E Ratio。

    先抓較輕量的 /en/valuation 總覽頁（帶時間戳參數避開 Cloudflare CDN 6 小時邊緣快取），
    版面變動或數值不合理（超出 config.json forwardPe.min–max）就退回明細頁解析。
    都失敗時沿用上次成功值（isFallback=True）；資料日期超過 staleDays 天標 stale（頁面顯示「資料可能過期」）。
    """
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
    parsed = None
    for url, parser, label in (
        (_DOLLARLIQUIDITY_OVERVIEW_URL, _parse_dollarliquidity_overview, "總覽頁"),
        (_DOLLARLIQUIDITY_URL, _parse_dollarliquidity_detail, "明細頁"),
    ):
        try:
            resp = _get(url, "dollarliquidity", params={"_": int(time.time())}, headers=headers, timeout=15)
        except Exception as error:
            print("[pe] DollarLiquidity " + label + "取得失敗：" + str(error))
            continue
        candidate = parser(resp.text)
        if candidate and _pe_plausible(candidate["value"]):
            parsed = candidate
            break
        _record("dollarliquidity", "empty")
        print("[pe] DollarLiquidity " + label + "解析不到合理的 Forward P/E")

    if parsed is not None:
        _last_good_pe["info"] = dict(parsed)
        return _finish_pe(parsed)
    if _last_good_pe["info"] is not None:
        return _finish_pe(_last_good_pe["info"], is_fallback=True, error="本次解析失敗，沿用上次成功值")
    return pe_unavailable("DollarLiquidity 解析失敗")


def pe_unavailable(message):
    """Forward P/E 缺值（value 為 None，附 error）。"""
    empty = {
        "value": None,
        "date": None,
        "previousValue": None,
        "previousDate": None,
        "previousValueDerived": False,
        "change": None,
    }
    return _finish_pe(empty, error=message)


def vix_unavailable():
    return {"value": None, "previousValue": None, "date": None}


def empty_market_indicators(message="市場指標取得失敗"):
    return {"vix": vix_unavailable(), "nasdaq100ForwardPe": {"dollarliquidity": pe_unavailable(message)}}


def get_market_indicators():
    """並行抓取 VIX 與 DollarLiquidity 的 Nasdaq 100 Forward P/E Ratio，縮短等待時間。"""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_vix = pool.submit(get_vix)
        f_pe = pool.submit(get_nasdaq100_forward_pe_dollarliquidity)
        return {
            "vix": safe_result(f_vix, vix_unavailable(), "vix"),
            "nasdaq100ForwardPe": {
                "dollarliquidity": safe_result(f_pe, pe_unavailable("DollarLiquidity 取得失敗"), "pe"),
            },
        }

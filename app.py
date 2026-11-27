"""Portfolio 後端：Flask API + 頁面服務（本機模式）。

啟動：
    pip install -r requirements-dev.txt
    python app.py              # 需要互動除錯器時：FLASK_DEBUG=1 python app.py
然後開瀏覽器： http://127.0.0.1:5000

持股存於 portfolio.json（本機檔案），透過 /api/holdings 讀寫。
只服務本機：Host 必須是 127.0.0.1:5000 / localhost:5000（擋 DNS rebinding），
寫入請求必須是同源的 application/json（擋跨站請求偽造）。
"""

import json
import math
import os
import re
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

try:
    from flask import Flask, Response, jsonify, request, send_from_directory
except ImportError:  # CI 靜態建置只裝 requests；build_static.py 只用到 build_* / load_* / render_page
    Flask = None
    Response = jsonify = request = send_from_directory = None

import portfolio_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _default_data_dir():
    """資料目錄順序：PORTFOLIO_DATA_DIR → 相鄰 ../portfolio_private_data → BASE_DIR。"""
    env_dir = os.environ.get("PORTFOLIO_DATA_DIR")
    if env_dir:
        return os.path.abspath(env_dir)
    if not os.path.exists(os.path.join(BASE_DIR, "portfolio.json")):
        sibling = os.path.abspath(os.path.join(BASE_DIR, "..", "portfolio_private_data"))
        if os.path.isdir(sibling):
            return sibling
    return BASE_DIR


DATA_DIR = _default_data_dir()
PORTFOLIO_FILE = os.path.join(DATA_DIR, "portfolio.json")
PORTFOLIO_2025_FILE = os.path.join(DATA_DIR, "2025portfolio.json")
DEBT_FILE = os.path.join(DATA_DIR, "debt.json")
TEMPLATE_FILE = os.path.join(BASE_DIR, "portfolio.html")

# portfolio.html 內唯一的注入點：本機模式換成 APP_CONFIG，靜態建置換成 APP_CONFIG + STATIC_DATA
STATIC_MARKER = "<!--STATIC_DATA-->"

TICKER_RE = re.compile(r"[A-Z0-9.\-]{1,15}")
NAME_MAX_LEN = 100
MARKETS = (None, "TW", "US")
LOCAL_HOSTS = {"127.0.0.1:5000", "localhost:5000"}
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ALLOWED_FETCH_SITES = {None, "same-origin", "none"}  # None：非瀏覽器客戶端（curl、測試）
_LOHAS_WORKERS = 4


class HoldingsFileError(Exception):
    """持股檔存在但無法解析或格式不符。API 回 500 並拒絕寫入，避免用空清單覆寫原檔。"""


class _DummyApp:
    """沒安裝 Flask 時的替身：讓 build_static.py 可以 import 本模組。"""

    def route(self, *_args, **_kwargs):
        return lambda fn: fn

    def before_request(self, fn):
        return fn

    def errorhandler(self, *_args, **_kwargs):
        return lambda fn: fn


app = Flask(__name__, static_folder=None) if Flask is not None else _DummyApp()


# ------------------------------------------------------------------ 持股檔

def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _holding_problem(item):
    """回傳持股項目的格式問題描述；沒問題回傳 None。"""
    if not isinstance(item, dict):
        return "不是物件"
    ticker = item.get("ticker")
    if not isinstance(ticker, str) or not TICKER_RE.fullmatch(ticker.strip().upper()):
        return "的 ticker 格式不正確"
    if not (_finite_number(item.get("shares")) and item["shares"] >= 0):
        return "的 shares 必須為 ≥ 0 的有限數字"
    if item.get("beta") is not None and not _finite_number(item["beta"]):
        return "的 beta 必須為有限數字或留空"
    market = item.get("market")
    if market is not None and (not isinstance(market, str) or market.strip().upper() not in MARKETS):
        return "的 market 只能是 TW、US 或留空"
    if item.get("name") is not None and not isinstance(item["name"], str):
        return "的 name 必須為字串"
    return None


def _read_holdings_file(path):
    """檔案不存在 → []；存在但讀不到、JSON 錯誤或格式不符 → HoldingsFileError。"""
    label = os.path.basename(path)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise HoldingsFileError(label + " 讀取失敗：" + str(error)) from error
    if not isinstance(data, list):
        raise HoldingsFileError(label + " 格式錯誤：最外層必須是陣列")
    for index, item in enumerate(data):
        problem = _holding_problem(item)
        if problem:
            raise HoldingsFileError(label + " 第 " + str(index + 1) + " 筆" + problem)
    return data


def load_holdings(data_dir=None):
    path = os.path.join(data_dir, "portfolio.json") if data_dir else PORTFOLIO_FILE
    return _read_holdings_file(path)


def load_2025_holdings(data_dir=None):
    path = os.path.join(data_dir, "2025portfolio.json") if data_dir else PORTFOLIO_2025_FILE
    return _read_holdings_file(path)


def load_debt(path=None):
    """讀取借貸負債 JSON（給 build_static.py 內嵌與加密用）；檔案不存在回傳 None。"""
    target = path or DEBT_FILE
    if not os.path.exists(target):
        return None

    def reject_constant(name):
        raise HoldingsFileError("debt.json 讀取失敗：含有 " + name)

    try:
        with open(target, "r", encoding="utf-8-sig") as handle:
            data = json.loads(handle.read(), parse_constant=reject_constant)
    except (OSError, ValueError) as error:
        raise HoldingsFileError("debt.json 讀取失敗：" + str(error)) from error
    if not isinstance(data, list):
        raise HoldingsFileError("debt.json 格式錯誤：最外層必須是陣列")
    return data


_holdings_lock = threading.Lock()  # Flask 開發伺服器是多執行緒；讀-改-寫要序列化


def save_holdings(holdings):
    """原子寫入：先寫同目錄暫存檔再 os.replace，不會留下寫到一半的 portfolio.json。

    allow_nan=False：NaN / Infinity 直接拋錯，不會寫出不合 JSON 標準的檔案。
    """
    payload = json.dumps(holdings, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    # 暫存檔放在目標檔同一目錄：os.replace 只有在同一個檔案系統內才是原子操作
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PORTFOLIO_FILE), prefix=".portfolio-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.exists(PORTFOLIO_FILE):
            shutil.copymode(PORTFOLIO_FILE, tmp)
        os.replace(tmp, PORTFOLIO_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _norm_market(value):
    return str(value or "").strip().upper() or None


def _same_holding(item, ticker, market):
    # 同 ticker + 同 market（例如美股 QQQM vs 台股複委託 QQQM）才視為同一筆
    return item["ticker"].strip().upper() == ticker and _norm_market(item.get("market")) == market


# ------------------------------------------------------------------ 頁面

def js_json(obj):
    """可安全內嵌在 <script> 的 JSON。

    allow_nan=False：NaN / Infinity 直接拋錯（建置失敗，不發布壞資料）；
    '<' 轉成 \\u003c：資料中的 </script> 或 <!-- 無法提早結束 script 區塊。
    """
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


def render_page(static_data=None):
    """把 portfolio.html 的注入標記換成設定（與靜態資料）。標記必須恰好出現 1 次。"""
    with open(TEMPLATE_FILE, "r", encoding="utf-8") as handle:
        html = handle.read()
    count = html.count(STATIC_MARKER)
    if count != 1:
        raise RuntimeError(
            "portfolio.html 的注入標記 " + STATIC_MARKER + " 應恰好 1 個，實際 " + str(count) + " 個"
        )
    script = "<script>window.APP_CONFIG=" + js_json(portfolio_data.public_config()) + ";"
    if static_data is not None:
        script += "window.STATIC_DATA=" + js_json(static_data) + ";"
    script += "</script>"
    return html.replace(STATIC_MARKER, script)


# ------------------------------------------------------------------ 請求防護

def _error(message, status):
    return jsonify({"error": message}), status


@app.before_request
def _guard_local_requests():
    """只服務本機：Host 檢查擋 DNS rebinding；Sec-Fetch-Site 檢查擋跨站寫入。"""
    if (request.host or "").lower() not in LOCAL_HOSTS:
        return _error("只接受 127.0.0.1:5000 或 localhost:5000 的請求", 400)
    if request.method in _UNSAFE_METHODS and request.headers.get("Sec-Fetch-Site") not in _ALLOWED_FETCH_SITES:
        return _error("拒絕跨站寫入請求", 403)
    return None


@app.errorhandler(HoldingsFileError)
def _holdings_file_error(error):
    print("[portfolio] " + str(error))
    return _error(str(error), 500)


def _valid_ticker(raw):
    # 只接受字串：不把 JSON 的 true / 1234 轉成 "TRUE" / "1234"（與讀檔驗證 _holding_problem 一致）
    if not isinstance(raw, str):
        return None
    ticker = raw.strip().upper()
    return ticker if TICKER_RE.fullmatch(ticker) else None


# ------------------------------------------------------------------ 路由

@app.route("/")
def index():
    return Response(render_page(), mimetype="text/html")


@app.route("/core.js")
def core_js():
    """前後端共用的純函式（共識選價、五線譜回歸、Beta 分桶、還款期數）。"""
    return send_from_directory(BASE_DIR, "core.js")


@app.route("/debt.json")
def debt_json():
    """借貸負債資料（本機模式優先讀取 BASE_DIR，其次讀取 DATA_DIR）。"""
    if os.path.exists(os.path.join(BASE_DIR, "debt.json")):
        return send_from_directory(BASE_DIR, "debt.json")
    return send_from_directory(os.path.dirname(DEBT_FILE), os.path.basename(DEBT_FILE))


@app.route("/api/holdings", methods=["GET"])
def get_holdings():
    return jsonify(load_holdings())


def _parse_shares(value):
    if isinstance(value, bool):
        return None
    try:
        shares = float(value)
    except (TypeError, ValueError):
        return None
    return shares if (math.isfinite(shares) and shares >= 0) else None


@app.route("/api/holdings", methods=["POST"])
def add_holding():
    if not request.is_json:
        return _error("Content-Type 必須為 application/json", 400)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _error("請求內容必須是 JSON 物件", 400)
    ticker = _valid_ticker(body.get("ticker"))
    if ticker is None:
        return _error("ticker 格式不正確（限英數字、點與連字號，1–15 字）", 400)
    shares = _parse_shares(body.get("shares"))
    if shares is None:
        return _error("shares 必須為 ≥ 0 的有限數字", 400)
    name = body.get("name")
    if name is not None and not isinstance(name, str):
        return _error("name 必須為字串", 400)
    name = (name or "").strip()
    if len(name) > NAME_MAX_LEN:
        return _error("name 最多 " + str(NAME_MAX_LEN) + " 字", 400)
    market = body.get("market")
    if market is not None and not isinstance(market, str):
        return _error("market 只能是 TW、US 或留空", 400)
    market = _norm_market(market)
    if market not in MARKETS:
        return _error("market 只能是 TW、US 或留空", 400)

    with _holdings_lock:
        holdings = load_holdings()
        for item in holdings:
            if _same_holding(item, ticker, market):
                item["shares"] = shares
                if name:
                    item["name"] = name
                break
        else:
            new_item = {"ticker": ticker, "name": name, "shares": shares}
            if market:
                new_item["market"] = market
            holdings.append(new_item)
        save_holdings(holdings)
    return jsonify(holdings)


@app.route("/api/holdings/<ticker>", methods=["DELETE"])
def delete_holding(ticker):
    ticker = _valid_ticker(ticker)
    if ticker is None:
        return _error("ticker 格式不正確", 400)
    market = _norm_market(request.args.get("market"))
    with _holdings_lock:
        holdings = load_holdings()
        kept = [h for h in holdings if not _same_holding(h, ticker, market)]
        if len(kept) == len(holdings):
            return _error("找不到這筆持股：" + ticker + ("（" + market + "）" if market else ""), 404)
        save_holdings(kept)
    return jsonify(kept)


@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    """回傳目前持股的即時報價、每檔台幣總額與占比。"""
    return jsonify(build_portfolio(load_holdings()))


def beta_bucket(is_cash, beta, cfg=None):
    """Beta 分桶（規則見 config.json betaBuckets；前端 core.js betaBucket 實作同一套）。

    現金或 Beta == cashBeta → cash；未填 Beta → unclassified；Beta >= leveragedMin → leveraged；其餘 → index。
    注意這是依 Beta 數值分類，不是依「是否為槓桿型 ETF」：個股手動填 Beta 2 也會歸入 leveraged（見 README）。
    """
    cfg = cfg or portfolio_data.CONFIG["betaBuckets"]
    if is_cash or (beta is not None and beta == cfg["cashBeta"]):
        return "cash"
    if beta is None:
        return "unclassified"
    if beta >= cfg["leveragedMin"]:
        return "leveraged"
    return "index"


def summarize_positions(positions, fx_rate, bucket_cfg=None):
    """持股彙總（總額、占比、市場小計、Beta 分桶、加權 Beta）。會就地寫入每筆 position["weight"] 並依市值排序。

    前端即時刷新後用 core.js summarizePositions 重算，規則相同（tests/golden/portfolio_summary.json 固定兩邊結果）。
    position 需要 ticker / value / isCash / isTaiwan / beta / shares；value 為 None 代表缺價。

    缺值不冒充：缺價持股列在 missingPrice（不計入總資產）；未填 Beta 的非現金持股歸入 unclassified、
    列在 betaUnknown，也不計入加權 Beta；沒有任何已知 Beta 時 portfolioBeta 為 None（頁面顯示「—」）。
    """
    total = 0.0
    missing_price = []
    for position in positions:
        value = position["value"]
        if value is None and (position.get("shares") or 0) > 0 and not position["isCash"]:
            missing_price.append(position["ticker"])
        if value:
            total += value

    beta_value_sum = 0.0
    beta_weight_sum = 0.0
    beta_unknown = []
    breakdown = {"us": 0.0, "tw": 0.0, "cash": 0.0}
    buckets = {"index": 0.0, "leveraged": 0.0, "cash": 0.0, "unclassified": 0.0}
    for position in positions:
        value = position["value"]
        position["weight"] = round(value / total * 100, 2) if (value and total > 0) else 0.0
        if not value:
            continue
        if position["isCash"]:
            breakdown["cash"] += value
        elif position["isTaiwan"]:
            breakdown["tw"] += value
        else:
            breakdown["us"] += value
        buckets[beta_bucket(position["isCash"], position["beta"], bucket_cfg)] += value
        beta = 0.0 if (position["isCash"] and position["beta"] is None) else position["beta"]
        if beta is None:
            beta_unknown.append(position["ticker"])
        else:
            beta_value_sum += value * beta
            beta_weight_sum += value

    positions.sort(key=lambda p: p["value"] or 0, reverse=True)
    return {
        "positions": positions,
        "totalValue": round(total, 2),
        "totalValueUsd": round(total / fx_rate, 2) if fx_rate else None,
        "fxRate": fx_rate,
        "portfolioBeta": round(beta_value_sum / beta_weight_sum, 4) if beta_weight_sum > 0 else None,
        "breakdown": {k: round(v, 2) for k, v in breakdown.items()},
        "breakdownUsd": {k: round(v / fx_rate, 2) for k, v in breakdown.items()} if fx_rate else None,
        "betaAllocation": {k: round(v / total * 100, 2) if total > 0 else 0.0 for k, v in buckets.items()},
        "missingPrice": missing_price,
        "betaUnknown": beta_unknown,
    }


def build_portfolio(holdings):
    """組出持股快照（報價、台幣總額、占比、Beta、加權 Beta、資產分類小計）。

    Flask 端點與靜態建置腳本共用。
    holding 可帶 "market": "TW" 來強制歸類為台股資產（例如台股複委託買的美股 ETF，
    報價仍沿用該 ticker 原本的美股/台股報價來源，只是統計分類不同）。
    彙總規則見 summarize_positions。
    """
    quotes = portfolio_data.get_quotes([h["ticker"] for h in holdings]) if holdings else {}
    fx = portfolio_data.get_usd_twd_rate_info()

    positions = []
    for holding in holdings:
        ticker = holding["ticker"].strip().upper()
        quote = quotes.get(ticker) or {}
        price = quote.get("price")
        shares = float(holding.get("shares") or 0)
        beta = holding.get("beta")
        market_override = _norm_market(holding.get("market"))
        # market 只影響資產分類（美股/台股統計），不影響報價來源
        is_taiwan = (market_override == "TW") if market_override else portfolio_data.is_taiwan_ticker(ticker)
        positions.append(
            {
                "ticker": ticker,
                "name": holding.get("name") or quote.get("name") or ticker,
                "shares": shares,
                "price": price,
                "priceUsd": quote.get("priceUsd"),
                "fxRate": quote.get("fxRate"),
                "value": round(price * shares, 2) if price is not None else None,
                "beta": float(beta) if beta is not None else None,
                "isTaiwan": is_taiwan,
                "isCash": portfolio_data.is_cash(ticker),
                "market": market_override,
                "source": quote.get("source"),
                "conflict": bool(quote.get("conflict")),
                "stale": bool(quote.get("stale")),
                "sources": quote.get("sources") or {},
                "asOf": quote.get("asOf"),
            }
        )

    summary = summarize_positions(positions, fx["value"])
    summary["fx"] = fx
    return summary


def _valid_start(raw):
    """YYYY-MM-DD、2000-01-01 至今天（台北日期）。"""
    age = portfolio_data.days_old(raw)
    return age is not None and age >= 0 and raw >= "2000-01-01"


@app.route("/api/history", methods=["GET"])
def get_history():
    """歷史回溯（假設目前持股固定不變，並同步回傳 2025portfolio.json 總資產曲線）。"""
    start = request.args.get("start") or portfolio_data.CONFIG["history"]["start"]
    if not _valid_start(start):
        return _error("start 必須為 YYYY-MM-DD（2000-01-01 至今天）", 400)
    holdings = load_holdings()
    return jsonify(portfolio_data.get_history(holdings, start=start, holdings_2025=load_2025_holdings()))


@app.route("/api/quote/<ticker>", methods=["GET"])
def get_single_quote(ticker):
    """單檔查價，用於新增前預覽名稱與現價。"""
    ticker = _valid_ticker(ticker)
    if ticker is None:
        return _error("ticker 格式不正確", 400)
    return jsonify(portfolio_data.get_quote(ticker))


@app.route("/api/lohas/<ticker>", methods=["GET"])
def get_lohas(ticker):
    """樂活五線譜資料（完整 chartData）。"""
    ticker = _valid_ticker(ticker)
    if ticker is None:
        return _error("ticker 格式不正確", 400)
    return jsonify(portfolio_data.get_lohas(ticker))


@app.route("/api/feargreed", methods=["GET"])
def get_feargreed():
    """CNN Fear & Greed 即時分數（失敗時 score 為 null，附 error）。"""
    return jsonify(portfolio_data.get_fear_greed())


@app.route("/api/market-indicators", methods=["GET"])
def get_market_indicators():
    """VIX 指數與 Nasdaq 100 Forward P/E Ratio（DollarLiquidity）。"""
    return jsonify(portfolio_data.get_market_indicators())


def lohas_tickers(holdings):
    """五線譜選單：每檔持股（排除現金、同 ticker 去重）+ config.json 的額外觀察商品。"""
    held = {h["ticker"].strip().upper() for h in holdings if not portfolio_data.is_cash(h["ticker"])}
    return sorted(held | {t.strip().upper() for t in portfolio_data.CONFIG["extraLohasTickers"]})


def build_lohas_bundle(holdings):
    """靜態建置用：並行算好每檔五線譜（精簡格式：只含日期與收盤價）+ Fear&Greed + 市場指標。"""
    tickers = lohas_tickers(holdings)
    with ThreadPoolExecutor(max_workers=_LOHAS_WORKERS) as pool:
        f_fg = pool.submit(portfolio_data.get_fear_greed)
        f_mkt = pool.submit(portfolio_data.get_market_indicators)
        f_charts = {t: pool.submit(portfolio_data.get_lohas, t, None, True) for t in tickers}
        charts = {}
        for ticker, future in f_charts.items():
            data = portfolio_data.safe_result(future, {}, "lohas " + ticker)
            if data.get("dates"):
                charts[ticker] = data
        fear_greed = portfolio_data.safe_result(
            f_fg, portfolio_data.fear_greed_unavailable("CNN 取得失敗"), "fg"
        )
        indicators = portfolio_data.safe_result(
            f_mkt, portfolio_data.empty_market_indicators(), "market-indicators"
        )
    return {
        "tickers": list(charts),
        "charts": charts,
        "failed": [t for t in tickers if t not in charts],
        "fearGreed": fear_greed,
        "marketIndicators": indicators,
    }


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG") == "1"
    print("Portfolio 儀表板啟動： http://127.0.0.1:5000" + ("（debug 模式）" if debug else ""))
    app.run(host="127.0.0.1", port=5000, debug=debug)

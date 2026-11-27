"""Flask 本機模式：請求防護、輸入驗證、持股檔讀寫（原子寫入、壞檔不覆寫）、頁面注入（全部虛構資料）。"""

import json
import os
import stat

import pytest

pytest.importorskip("flask")

import app as app_module  # noqa: E402
import portfolio_data as pdata  # noqa: E402

BASE = "http://127.0.0.1:5000"
JSON = "application/json"


@pytest.fixture
def files(tmp_path, monkeypatch):
    portfolio = tmp_path / "portfolio.json"
    portfolio_2025 = tmp_path / "2025portfolio.json"
    monkeypatch.setattr(app_module, "PORTFOLIO_FILE", str(portfolio))
    monkeypatch.setattr(app_module, "PORTFOLIO_2025_FILE", str(portfolio_2025))
    return portfolio, portfolio_2025


@pytest.fixture
def client(files):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def get(client, path, **kwargs):
    return client.get(path, base_url=BASE, **kwargs)


def post(client, body, raw=None, **kwargs):
    data = raw if raw is not None else json.dumps(body)
    return client.post("/api/holdings", base_url=BASE, data=data, content_type=kwargs.pop("content_type", JSON),
                       **kwargs)


def delete(client, path, **kwargs):
    return client.delete(path, base_url=BASE, **kwargs)


def write(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def tmp_leftovers(directory):
    return [name for name in os.listdir(directory) if name.startswith(".portfolio-")]


# ------------------------------------------------------------------ 請求防護

@pytest.mark.parametrize("base_url", ["http://evil.example:5000", "http://127.0.0.1:8080", "http://localhost"])
def test_foreign_host_is_rejected(client, base_url):
    resp = client.get("/api/holdings", base_url=base_url)
    assert resp.status_code == 400 and "127.0.0.1:5000" in resp.get_json()["error"]


def test_localhost_is_accepted(client):
    assert client.get("/api/holdings", base_url="http://localhost:5000").status_code == 200


@pytest.mark.parametrize("site, status", [("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)])
def test_cross_site_writes_are_rejected(client, site, status):
    resp = post(client, {"ticker": "FAKE", "shares": 1}, headers={"Sec-Fetch-Site": site})
    assert resp.status_code == status


def test_cross_site_reads_are_allowed(client):
    assert get(client, "/api/holdings", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


# ------------------------------------------------------------------ 輸入驗證

@pytest.mark.parametrize("body, raw, content_type, message", [
    ({"ticker": "FAKE", "shares": 1}, None, "text/plain", "Content-Type"),
    ([1, 2], None, JSON, "JSON 物件"),
    (None, "{broken", JSON, "JSON 物件"),
    ({"ticker": "<script>", "shares": 1}, None, JSON, "ticker"),
    ({"ticker": "", "shares": 1}, None, JSON, "ticker"),
    ({"ticker": "A" * 16, "shares": 1}, None, JSON, "ticker"),
    ({"ticker": "AB CD", "shares": 1}, None, JSON, "ticker"),
    ({"ticker": 1234, "shares": 1}, None, JSON, "ticker"),
    ({"ticker": True, "shares": 1}, None, JSON, "ticker"),
    ({"ticker": "FAKE", "shares": -1}, None, JSON, "shares"),
    ({"ticker": "FAKE", "shares": "abc"}, None, JSON, "shares"),
    ({"ticker": "FAKE", "shares": True}, None, JSON, "shares"),
    ({"ticker": "FAKE"}, None, JSON, "shares"),
    (None, '{"ticker": "FAKE", "shares": NaN}', JSON, "shares"),
    (None, '{"ticker": "FAKE", "shares": 1e400}', JSON, "shares"),
    ({"ticker": "FAKE", "shares": 1, "name": 5}, None, JSON, "name"),
    ({"ticker": "FAKE", "shares": 1, "name": "x" * 101}, None, JSON, "name"),
    ({"ticker": "FAKE", "shares": 1, "market": "JP"}, None, JSON, "market"),
    ({"ticker": "FAKE", "shares": 1, "market": 5}, None, JSON, "market"),
])
def test_invalid_post_is_rejected(client, files, body, raw, content_type, message):
    resp = post(client, body, raw=raw, content_type=content_type)
    assert resp.status_code == 400
    assert message in resp.get_json()["error"]
    assert not files[0].exists()


# ------------------------------------------------------------------ 新增 / 更新 / 刪除

def test_add_update_and_delete(client, files):
    portfolio = files[0]
    assert post(client, {"ticker": " fake ", "shares": "2.5", "name": " 假公司 "}).get_json() == [
        {"ticker": "FAKE", "name": "假公司", "shares": 2.5}]
    post(client, {"ticker": "FAKE", "shares": 3})  # 同代號同市場 → 更新股數、保留名稱
    post(client, {"ticker": "fake", "shares": 4, "market": "tw"})  # 不同市場 → 另一筆
    saved = json.loads(portfolio.read_text(encoding="utf-8"))
    assert saved == [{"ticker": "FAKE", "name": "假公司", "shares": 3.0},
                     {"ticker": "FAKE", "name": "", "shares": 4.0, "market": "TW"}]

    assert delete(client, "/api/holdings/FAKE?market=TW").get_json() == [
        {"ticker": "FAKE", "name": "假公司", "shares": 3.0}]
    resp = delete(client, "/api/holdings/FAKE?market=US")
    assert resp.status_code == 404 and "找不到" in resp.get_json()["error"]
    assert delete(client, "/api/holdings/fake").get_json() == []
    assert delete(client, "/api/holdings/%3Cx%3E").status_code == 400
    assert tmp_leftovers(portfolio.parent) == []


@pytest.mark.parametrize("content, message", [
    ("{not json", "讀取失敗"),
    ({"ticker": "FAKE"}, "最外層必須是陣列"),
    ([{"ticker": "FAKE", "shares": "abc"}], "第 1 筆的 shares"),
    ([{"ticker": "FAKE", "shares": 1}, {"ticker": "FAKE", "shares": 1, "market": "JP"}], "第 2 筆的 market"),
    ([{"ticker": "FAKE", "shares": 1, "beta": "high"}], "beta"),
    (["FAKE"], "不是物件"),
])
def test_corrupt_holdings_file_is_never_overwritten(client, files, content, message):
    portfolio = files[0]
    write(portfolio, content)
    before = portfolio.read_bytes()
    for resp in (get(client, "/api/holdings"), post(client, {"ticker": "NEW", "shares": 1}),
                 delete(client, "/api/holdings/FAKE"), get(client, "/api/portfolio")):
        assert resp.status_code == 500
        assert message in resp.get_json()["error"]
    assert portfolio.read_bytes() == before


# ------------------------------------------------------------------ 原子寫入

def test_save_preserves_mode_and_leaves_no_temp_file(files):
    portfolio = files[0]
    write(portfolio, [])
    os.chmod(portfolio, 0o600)
    app_module.save_holdings([{"ticker": "FAKE", "name": "", "shares": 1.0}])
    assert stat.S_IMODE(os.stat(portfolio).st_mode) == 0o600
    assert json.loads(portfolio.read_text(encoding="utf-8"))[0]["ticker"] == "FAKE"
    assert tmp_leftovers(portfolio.parent) == []


def test_save_rejects_nan_without_touching_file(files):
    portfolio = files[0]
    write(portfolio, [{"ticker": "FAKE", "shares": 1}])
    before = portfolio.read_bytes()
    with pytest.raises(ValueError):
        app_module.save_holdings([{"ticker": "FAKE", "shares": float("nan")}])
    assert portfolio.read_bytes() == before and tmp_leftovers(portfolio.parent) == []


def test_failed_replace_cleans_up_temp_file(files, monkeypatch):
    portfolio = files[0]
    write(portfolio, [{"ticker": "FAKE", "shares": 1}])
    before = portfolio.read_bytes()

    def broken_replace(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(app_module.os, "replace", broken_replace)
    with pytest.raises(OSError):
        app_module.save_holdings([{"ticker": "OTHER", "shares": 2}])
    assert portfolio.read_bytes() == before and tmp_leftovers(portfolio.parent) == []


# ------------------------------------------------------------------ 其他 API

@pytest.mark.parametrize("start", ["2026-13-01", "abc", "1999-12-31", "2999-01-01", "2026-1-1"])
def test_history_rejects_bad_start(client, start):
    resp = get(client, "/api/history?start=" + start)
    assert resp.status_code == 400 and "start" in resp.get_json()["error"]


def test_history_passes_start_and_2025_holdings(client, files, monkeypatch):
    write(files[0], [{"ticker": "FAKE", "shares": 1}])
    write(files[1], [{"ticker": "OLD", "shares": 2}])
    seen = {}

    def fake_history(holdings, start=None, holdings_2025=None):
        seen.update(holdings=holdings, start=start, holdings_2025=holdings_2025)
        return pdata._empty_history()

    monkeypatch.setattr(pdata, "get_history", fake_history)
    assert get(client, "/api/history?start=2024-01-02").status_code == 200
    assert seen == {"holdings": [{"ticker": "FAKE", "shares": 1}], "start": "2024-01-02",
                    "holdings_2025": [{"ticker": "OLD", "shares": 2}]}


@pytest.mark.parametrize("path", ["/api/quote/%3Cscript%3E", "/api/lohas/AAAAAAAAAAAAAAAA", "/api/quote/a%20b"])
def test_bad_ticker_in_path_is_rejected(client, path):
    assert get(client, path).status_code == 400


def test_quote_and_lohas_normalize_ticker(client, monkeypatch):
    monkeypatch.setattr(pdata, "get_quote", lambda t: {"ticker": t})
    monkeypatch.setattr(pdata, "get_lohas", lambda t: {"ticker": t})
    assert get(client, "/api/quote/fake.tw").get_json() == {"ticker": "FAKE.TW"}
    assert get(client, "/api/lohas/fake").get_json() == {"ticker": "FAKE"}


def test_feargreed_and_indicators_endpoints(client, monkeypatch):
    monkeypatch.setattr(pdata, "get_fear_greed", lambda: pdata.fear_greed_unavailable("測試"))
    monkeypatch.setattr(pdata, "get_market_indicators", lambda: pdata.empty_market_indicators("測試"))
    assert get(client, "/api/feargreed").get_json()["error"] == "測試"
    assert get(client, "/api/market-indicators").get_json()["vix"]["value"] is None


# ------------------------------------------------------------------ 頁面

def test_index_injects_config_only(client):
    resp = get(client, "/")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and resp.mimetype == "text/html"
    assert html.count("window.APP_CONFIG=") == 1
    assert "window.STATIC_DATA=" not in html and app_module.STATIC_MARKER not in html
    assert html.index("window.APP_CONFIG=") < html.index('<script src="core.js">')


def test_render_page_escapes_static_data():
    html = app_module.render_page({"name": "</script><script>alert(1)</script><!--", "n": 1})
    assert "</script><script>alert(1)" not in html and "<!--\"" not in html
    payload = html.split("window.STATIC_DATA=", 1)[1].split(";</script>", 1)[0]
    assert json.loads(payload)["name"].startswith("</script>")


@pytest.mark.parametrize("markers", [0, 2])
def test_render_page_requires_exactly_one_marker(tmp_path, monkeypatch, markers):
    template = tmp_path / "portfolio.html"
    template.write_text("<html>" + app_module.STATIC_MARKER * markers + "</html>", encoding="utf-8")
    monkeypatch.setattr(app_module, "TEMPLATE_FILE", str(template))
    with pytest.raises(RuntimeError, match="應恰好 1 個"):
        app_module.render_page()


def test_template_invariants():
    with open(app_module.TEMPLATE_FILE, encoding="utf-8") as handle:
        html = handle.read()
    assert html.count(app_module.STATIC_MARKER) == 1
    assert "window.STATIC_DATA=" not in html
    assert 'integrity="sha384-' in html  # Plotly CDN 有 SRI


def test_js_json_rejects_nan():
    with pytest.raises(ValueError):
        app_module.js_json({"x": float("nan")})
    assert app_module.js_json({"a": "<b>"}) == '{"a":"\\u003cb>"}'


def test_static_files_are_served_from_base_dir(client, tmp_path, monkeypatch):
    (tmp_path / "core.js").write_text("// fake core", encoding="utf-8")
    (tmp_path / "debt.json").write_text('[{"bank": "測試銀行", "principal": 1000}]', encoding="utf-8")
    monkeypatch.setattr(app_module, "BASE_DIR", str(tmp_path))
    assert get(client, "/core.js").get_data(as_text=True) == "// fake core"
    assert get(client, "/debt.json").get_json() == [{"bank": "測試銀行", "principal": 1000}]
    assert get(client, "/portfolio.json").status_code == 404  # 只開放這兩個靜態檔


# ------------------------------------------------------------------ 持股彙總

@pytest.fixture
def quotes(monkeypatch, fixed_config):
    data = {
        "9901.TW": {"name": "假股", "price": 100.0, "source": "twse", "sources": {"twse": 100.0}, "asOf": 1},
        "FAKE": {"name": None, "price": 300.0, "priceUsd": 10.0, "fxRate": 30.0, "source": "yahoo", "conflict": True},
        "NOPRICE": {"name": None, "price": None},
        "CASH": {"name": "台幣現金", "price": 1.0, "source": "cash"},
    }
    monkeypatch.setattr(pdata, "get_quotes", lambda tickers: {t.strip().upper(): data[t.strip().upper()] for t in tickers})
    monkeypatch.setattr(pdata, "get_usd_twd_rate_info", lambda: {
        "value": 30.0, "source": "yahoo", "asOf": None, "isFallback": False})
    return data


def test_build_portfolio(quotes):
    out = app_module.build_portfolio([
        {"ticker": "9901.TW", "shares": 10, "beta": 1.0},
        {"ticker": "FAKE", "shares": 2, "beta": 2.5, "market": "TW", "name": "複委託"},
        {"ticker": "NOPRICE", "shares": 3},
        {"ticker": "CASH", "shares": 400},
    ])
    by_ticker = {p["ticker"]: p for p in out["positions"]}
    assert [p["ticker"] for p in out["positions"]] == ["9901.TW", "FAKE", "CASH", "NOPRICE"]
    assert out["totalValue"] == 2000.0
    assert out["missingPrice"] == ["NOPRICE"] and out["betaUnknown"] == []
    assert by_ticker["FAKE"]["isTaiwan"] is True and by_ticker["FAKE"]["name"] == "複委託"
    assert by_ticker["FAKE"]["conflict"] is True and by_ticker["9901.TW"]["name"] == "假股"
    assert out["breakdown"] == {"us": 0.0, "tw": 1600.0, "cash": 400.0}
    assert out["betaAllocation"] == {"index": 50.0, "leveraged": 30.0, "cash": 20.0, "unclassified": 0.0}
    assert out["portfolioBeta"] == round((1000 * 1.0 + 600 * 2.5) / 2000, 4)
    assert out["fx"]["value"] == 30.0 and out["totalValueUsd"] == round(2000 / 30, 2)


def test_build_portfolio_unknown_beta_is_not_guessed(quotes):
    out = app_module.build_portfolio([{"ticker": "9901.TW", "shares": 1}])
    assert out["portfolioBeta"] is None and out["betaUnknown"] == ["9901.TW"]
    assert out["betaAllocation"]["unclassified"] == 100.0


def test_build_portfolio_empty(quotes):
    out = app_module.build_portfolio([])
    assert out["totalValue"] == 0.0 and out["positions"] == [] and out["portfolioBeta"] is None


def test_lohas_tickers(monkeypatch, fixed_config):
    monkeypatch.setitem(pdata.CONFIG, "extraLohasTickers", ["0050.TW", " qqq "])
    holdings = [{"ticker": "fake"}, {"ticker": "FAKE", "market": "TW"}, {"ticker": "cash"}, {"ticker": "0050.tw"}]
    assert app_module.lohas_tickers(holdings) == ["0050.TW", "FAKE", "QQQ"]


def test_build_lohas_bundle_reports_failures(monkeypatch, fixed_config):
    monkeypatch.setitem(pdata.CONFIG, "extraLohasTickers", [])

    def fake_lohas(ticker, start=None, compact=False):
        assert compact is True
        if ticker == "BOOM":
            raise RuntimeError("boom")
        if ticker == "EMPTY":
            return {"error": "找不到 EMPTY 的歷史數據", "chartData": []}
        return {"ticker": ticker, "dates": ["2026-11-20"], "closes": [1.0]}

    def broken_indicators():
        raise RuntimeError("down")

    monkeypatch.setattr(pdata, "get_lohas", fake_lohas)
    monkeypatch.setattr(pdata, "get_fear_greed", lambda: pdata.fear_greed_unavailable("x"))
    monkeypatch.setattr(pdata, "get_market_indicators", broken_indicators)
    out = app_module.build_lohas_bundle([{"ticker": t} for t in ("OK", "BOOM", "EMPTY")])
    assert out["tickers"] == ["OK"] and out["failed"] == ["BOOM", "EMPTY"]
    assert out["marketIndicators"]["vix"]["value"] is None
    assert out["fearGreed"]["score"] is None

"""部署前資料品質與加密驗證閘門：檢查 build_static.py / crypto_static.py 產出的 HTML。

用法：
    python validate_static.py dist/index.html --debt _private_data/debt.json
    python validate_static.py dist/index.html --encrypted --no-leak-from _private_data
    python validate_static.py dist/index.html --strict

明文模式檢查項目（門檻見 config.json validation）：
  ERROR   window.STATIC_DATA 恰好注入 1 次、可解析、不含 NaN / Infinity
  ERROR   generatedAt 帶台北時區（+08:00）
  ERROR   股數 > 0 的非現金持股都有 > 0 的價格；匯率不是保底值
  ERROR   歷史曲線有資料、沒有抓不到歷史的持股、單日變動 < historyMaxJump
  ERROR   五線譜收盤價單日變動 < lohasMaxJump（抓未還原的股票分割）
  ERROR   --debt：最外層是陣列；每筆的 bank、principal、monthlyPayment、dueDay、maturityDate（與有填的 asOfDate）格式正確
  WARNING VIX / Forward P/E / Fear & Greed 資料超過 indicatorMaxAgeDays 天或缺值；P/E 沿用上次成功值
  WARNING 報價來源不一致、沿用昨收、未填 Beta、匯率歷史近似、五線譜取得失敗
  WARNING --debt：借貸沒有 asOfDate（剩餘金額不會依還款日攤銷）

加密模式檢查項目（--encrypted）：
  ERROR   window.STATIC_DATA 不可出現（0 次）
  ERROR   window.APP_CONFIG 恰好注入 1 次且可解析
  ERROR   window.ENCRYPTED_DATA 恰好注入 1 次，且符合 AES-256-GCM + PBKDF2-SHA256 ≥ 600,000 次規格
  ERROR   --no-leak-from：私有資料中的銀行名稱、大額本金/月付/股數不得以明文出現在 HTML 中

只輸出代號、日期與檢查結果，絕不輸出任何金額、股數、銀行名稱或總資產變動幅度
（GitHub Actions 的 log 是公開的）。
"""

import argparse
import json
import math
import os
import re
import sys

import crypto_static
import portfolio_data

STATIC_PREFIX = "window.STATIC_DATA="
APP_CONFIG_PREFIX = "window.APP_CONFIG="


class ValidationError(Exception):
    pass


def _extract_json_prefix(html, prefix, label):
    count = html.count(prefix)
    if count != 1:
        raise ValidationError(prefix.rstrip("=") + " 應恰好出現 1 次，實際 " + str(count) + " 次")

    def reject_constant(name):
        raise ValidationError(label + " 含有 " + name)

    decoder = json.JSONDecoder(parse_constant=reject_constant)
    start = html.index(prefix) + len(prefix)
    try:
        data, _end = decoder.raw_decode(html, start)
    except ValueError as error:
        raise ValidationError(label + " 無法解析：" + str(error)) from error
    if not isinstance(data, dict):
        raise ValidationError(label + " 必須是物件")
    return data


def extract_static_data(html):
    """取出 window.STATIC_DATA 的 JSON（必須恰好一次；NaN / Infinity 視為錯誤）。"""
    return _extract_json_prefix(html, STATIC_PREFIX, "STATIC_DATA")


def max_jump(values):
    """每個數值相對「前一個正數」的最大變動 → (比例, 該點索引)；資料不足回傳 (0.0, None)。

    缺值（None、非數字）直接跳過、不中斷比較，所以缺值前後的跳動照樣抓得到；
    掉到 0 或負數算 100% 以上的變動（不能拿來當下一點的基準）。
    """
    best = (0.0, None)
    prev = None
    for i, val in enumerate(values or []):
        if not isinstance(val, (int, float)) or isinstance(val, bool) or not math.isfinite(val):
            continue
        if prev is not None:
            jump = abs(val / prev - 1)
            if jump > best[0]:
                best = (jump, i)
        if val > 0:
            prev = val
    return best


def _lohas_closes(chart):
    if isinstance(chart.get("closes"), list):
        return chart.get("dates") or [], chart["closes"]
    rows = chart.get("chartData") or []
    return [r.get("date") for r in rows], [r.get("close") for r in rows]


def _check_portfolio(portfolio, errors, warnings):
    positions = portfolio.get("positions")
    if not isinstance(positions, list) or not positions:
        errors.append("portfolio.positions 是空的")
        return
    for pos in positions:
        ticker = str(pos.get("ticker"))
        shares = pos.get("shares") or 0
        price = pos.get("price")
        if portfolio_data.is_cash(ticker) or pos.get("isCash"):
            continue
        if shares > 0 and not (isinstance(price, (int, float)) and price > 0):
            errors.append(ticker + " 沒有有效價格")
        if pos.get("conflict"):
            warnings.append(ticker + " 報價來源不一致（" + str(pos.get("source")) + "）")
        if pos.get("stale"):
            warnings.append(ticker + " 沒有即時價，沿用昨收")
    fx = portfolio.get("fx") or {}
    if fx.get("isFallback"):
        errors.append("匯率來源皆失敗，使用保底匯率")
    if portfolio.get("betaUnknown"):
        warnings.append("未填 Beta：" + ", ".join(portfolio["betaUnknown"]))


def _check_history(history, cfg, errors, warnings):
    dates = history.get("dates") or []
    if not dates:
        errors.append("歷史曲線沒有資料")
        return
    if history.get("missing"):
        errors.append("抓不到歷史價格：" + ", ".join(history["missing"]))
    if history.get("fxApproximated"):
        warnings.append("匯率歷史取得失敗，美股部位以目前匯率近似")
    for key, label in (("totalValue", "目前持股"), ("totalValue2025", "2025 持股")):
        jump, idx = max_jump(history.get(key))
        if idx is not None and jump >= cfg["historyMaxJump"]:
            day = dates[idx] if idx < len(dates) else "?"
            errors.append(
                label + "總資產曲線在 " + day + " 單日變動超過 "
                + format(cfg["historyMaxJump"], ".0%") + "（可能是缺價、分割或回補問題）"
            )


def _check_lohas(lohas, cfg, errors, warnings):
    charts = lohas.get("charts") or {}
    if not charts:
        errors.append("五線譜沒有任何商品資料")
    for ticker, chart in sorted(charts.items()):
        dates, closes = _lohas_closes(chart)
        jump, idx = max_jump(closes)
        if idx is not None and jump >= cfg["lohasMaxJump"]:
            day = dates[idx] if idx < len(dates) else "?"
            errors.append(
                ticker + " 五線譜收盤價在 " + str(day) + " 單日變動 " + format(jump, ".1%")
                + "（疑似未還原的股票分割，請檢查 splits.json）"
            )
    if lohas.get("failed"):
        warnings.append("五線譜取得失敗：" + ", ".join(lohas["failed"]))


def _check_indicators(lohas, cfg, warnings, today=None):
    max_age = cfg["indicatorMaxAgeDays"]

    def check(label, date_str, value):
        if value is None:
            warnings.append(label + " 資料暫缺")
            return
        age = portfolio_data.days_old(str(date_str or "")[:10], today=today)
        if age is None or age > max_age:
            warnings.append(label + " 資料日期 " + str(date_str) + " 超過 " + str(max_age) + " 天")

    indicators = lohas.get("marketIndicators") or {}
    vix = indicators.get("vix") or {}
    check("VIX", vix.get("date"), vix.get("value"))
    pe = (indicators.get("nasdaq100ForwardPe") or {}).get("dollarliquidity") or {}
    check("Nasdaq 100 Forward P/E", pe.get("date"), pe.get("value"))
    if pe.get("isFallback"):
        warnings.append("Nasdaq 100 Forward P/E 本次解析失敗，沿用上次成功值")
    fg = lohas.get("fearGreed") or {}
    check("CNN Fear & Greed", fg.get("timestamp"), fg.get("score"))


def validate(data, today=None):
    """回傳 (errors, warnings)，兩者都是字串清單。"""
    cfg = portfolio_data.CONFIG["validation"]
    errors, warnings = [], []
    generated = data.get("generatedAt")
    if not (isinstance(generated, str) and generated.endswith("+08:00")):
        errors.append("generatedAt 沒有台北時區（+08:00）：" + str(generated))
    _check_portfolio(data.get("portfolio") or {}, errors, warnings)
    _check_history(data.get("history") or {}, cfg, errors, warnings)
    lohas = data.get("lohas") or {}
    _check_lohas(lohas, cfg, errors, warnings)
    _check_indicators(lohas, cfg, warnings, today=today)
    return errors, warnings


# ------------------------------------------------------------------ debt.json

def _non_negative_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _valid_date(value):
    """YYYY-MM-DD 且是真實存在的日期（與 core.js parseLocalDate 同規則）。"""
    return portfolio_data.days_old(value) is not None


def validate_debt(items):
    """檢查已解析的 debt.json → (errors, warnings)。

    前端（core.js）遇到 dueDay 不是 1–31 的整數（Number.isInteger，所以 15.0 可以、"15" 不行）
    或日期格式不對時，會把剩餘期數算成 0；金額不是數字時會顯示 NaN——這些都在部署前擋下。
    """
    if not isinstance(items, list):
        return ["debt.json 最外層必須是陣列（沒有借貸時填 []）"], []
    errors, warnings = [], []
    for number, loan in enumerate(items, 1):
        where = "debt.json 第 " + str(number) + " 筆"
        if not isinstance(loan, dict):
            errors.append(where + "不是物件")
            continue
        bank = loan.get("bank")
        if not (isinstance(bank, str) and bank.strip()):
            errors.append(where + "：bank 必須是非空字串")
        for key in ("principal", "monthlyPayment"):
            if not _non_negative_number(loan.get(key)):
                errors.append(where + "：" + key + " 必須是 ≥ 0 的數字")
        due_day = loan.get("dueDay")
        if not (_non_negative_number(due_day) and due_day == int(due_day) and 1 <= due_day <= 31):
            errors.append(where + "：dueDay 必須是 1–31 的整數")
        if not _valid_date(loan.get("maturityDate")):
            errors.append(where + "：maturityDate 必須是有效的 YYYY-MM-DD 日期")
        if loan.get("asOfDate") is None:
            warnings.append(where + "沒有 asOfDate，借貸剩餘金額不會依還款日攤銷")
        elif not _valid_date(loan["asOfDate"]):
            errors.append(where + "：asOfDate 必須是有效的 YYYY-MM-DD 日期")
    return errors, warnings


def validate_debt_file(path):
    """讀取並檢查 debt.json → (errors, warnings)；讀不到、無法解析或含 NaN / Infinity 都是 ERROR。"""
    def reject_constant(name):
        raise ValueError("含有 " + name)

    try:
        # utf-8-sig：和瀏覽器的 fetch().json() 一樣接受開頭的 BOM
        with open(path, "r", encoding="utf-8-sig") as handle:
            items = json.loads(handle.read(), parse_constant=reject_constant)
    except OSError as error:
        return ["讀不到 debt.json：" + str(error)], []
    except ValueError as error:
        return ["debt.json 無法解析：" + str(error)], []
    return validate_debt(items)


# ------------------------------------------------------------------ 加密與明文外洩檢查

def _numeric_leak_tokens(value):
    """針對 ≥ 10000 的敏感數值產生可能的明文表示（避免把 0/1/180 等常見常數誤報）。"""
    if not _non_negative_number(value) or value < 10000:
        return []
    tokens = {format(value, "g")}
    if value == int(value):
        iv = int(value)
        tokens.add(str(iv))
        tokens.add(format(iv, ","))
    else:
        tokens.add(str(value))
    return sorted(tokens)


def _template_text():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.html")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""
    return ""


def _strip_encrypted_envelope(html):
    """移除已驗證的 window.ENCRYPTED_DATA 密文區塊，避免 Base64 亂數偶發碰撞數字 token。"""
    prefix = crypto_static.ENCRYPTED_PREFIX
    if html.count(prefix) != 1:
        return html
    try:
        _obj, start, end = crypto_static._extract_json_assignment(html, prefix)
    except crypto_static.CryptoError:
        return html
    return html[:start] + html[end:]


def check_no_plaintext_leaks(html, data_dir):
    """掃描加密後 HTML（密文外）是否意外包含 data_dir 私有 JSON 中的銀行名稱或大額數值（絕不印出機密值）。"""
    errors = []
    scanned = _strip_encrypted_envelope(html)
    template = _template_text()

    def leaked_number(value):
        for tok in _numeric_leak_tokens(value):
            if tok in template:
                continue
            if re.search(r"(?<!\d)" + re.escape(tok) + r"(?!\d)", scanned):
                return True
        return False

    debt_path = os.path.join(data_dir, "debt.json")
    if os.path.exists(debt_path):
        try:
            with open(debt_path, "r", encoding="utf-8-sig") as handle:
                loans = json.load(handle)
        except (OSError, ValueError) as error:
            errors.append("無法讀取 " + debt_path + " 進行外洩檢查：" + str(error))
            loans = []
        if isinstance(loans, list):
            for idx, loan in enumerate(loans, 1):
                if not isinstance(loan, dict):
                    continue
                bank = loan.get("bank")
                if isinstance(bank, str) and len(bank.strip()) >= 2 and bank.strip() in scanned:
                    errors.append(f"偵測到明文外洩（debt.json 第 {idx} 筆欄位 bank）")
                for field in ("principal", "monthlyPayment"):
                    if leaked_number(loan.get(field)):
                        errors.append(f"偵測到明文外洩（debt.json 第 {idx} 筆欄位 {field}）")

    for filename in ("portfolio.json", "2025portfolio.json"):
        path = os.path.join(data_dir, filename)
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8-sig") as handle:
                holdings = json.load(handle)
        except (OSError, ValueError) as error:
            errors.append("無法讀取 " + path + " 進行外洩檢查：" + str(error))
            continue
        if isinstance(holdings, list):
            for idx, item in enumerate(holdings, 1):
                if not isinstance(item, dict):
                    continue
                if leaked_number(item.get("shares")):
                    errors.append(f"偵測到明文外洩（{filename} 第 {idx} 筆欄位 shares）")
    return errors


def validate_encrypted_html(html, no_leak_dir=None):
    """檢查已加密靜態頁：不含明文 STATIC_DATA、含有效 APP_CONFIG 與 ENCRYPTED_DATA，且無明文外洩。"""
    errors = []
    if STATIC_PREFIX in html:
        errors.append("已加密頁面不得包含明文 " + STATIC_PREFIX)
    try:
        _extract_json_prefix(html, APP_CONFIG_PREFIX, "APP_CONFIG")
    except ValidationError as error:
        errors.append(str(error))
    try:
        crypto_static.extract_encrypted_envelope(html)
    except crypto_static.CryptoError as error:
        errors.append(str(error))
    if no_leak_dir:
        errors.extend(check_no_plaintext_leaks(html, no_leak_dir))
    return errors, []


def main(argv=None):
    parser = argparse.ArgumentParser(description="部署前檢查 index.html 內嵌資料（或已加密頁面）與 debt.json")
    parser.add_argument("html", help="build_static.py / crypto_static.py 產出的 HTML")
    parser.add_argument("--debt", metavar="DEBT_JSON", help="一併檢查借貸負債檔（例如 debt.json）")
    parser.add_argument("--encrypted", action="store_true", help="檢查已加密 HTML（驗證 ENCRYPTED_DATA 且不含明文 STATIC_DATA）")
    parser.add_argument("--no-leak-from", metavar="DATA_DIR", help="搭配 --encrypted：檢查私有資料目錄中的敏感值未以明文出現")
    parser.add_argument("--strict", action="store_true", help="WARNING 也視為錯誤")
    args = parser.parse_args(argv)

    with open(args.html, "r", encoding="utf-8") as handle:
        html = handle.read()
    if args.encrypted:
        errors, warnings = validate_encrypted_html(html, no_leak_dir=args.no_leak_from)
    else:
        try:
            errors, warnings = validate(extract_static_data(html))
        except ValidationError as error:
            errors, warnings = [str(error)], []
        if args.debt:
            debt_errors, debt_warnings = validate_debt_file(args.debt)
            errors, warnings = errors + debt_errors, warnings + debt_warnings
    if args.strict:
        errors, warnings = errors + warnings, []
    for message in warnings:
        print("::warning::" + message)
    for message in errors:
        print("::error::" + message)
    if errors:
        print("資料品質檢查失敗：" + str(len(errors)) + " 項錯誤、" + str(len(warnings)) + " 項警告")
        return 1
    print("資料品質檢查通過（" + str(len(warnings)) + " 項警告）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

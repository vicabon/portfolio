"""部署前資料品質閘門：每條規則都要能觸發；log 只能出現代號、日期與檢查結果（全部虛構資料）。"""

import datetime
import json
import re

import pytest

import portfolio_data as pdata
import validate_static as vs
from fakes import FAKE_PRICE, FAKE_SHARES, FAKE_TOTALS, static_data

TODAY = datetime.date(2026, 11, 20)
PE = ("lohas", "marketIndicators", "nasdaq100ForwardPe", "dollarliquidity")
VIX = ("lohas", "marketIndicators", "vix")


@pytest.fixture(autouse=True)
def thresholds(monkeypatch, fixed_config):
    """門檻固定，不受 config.json 調整影響。"""
    monkeypatch.setitem(pdata.CONFIG, "validation",
                        {"historyMaxJump": 0.15, "lohasMaxJump": 0.45, "indicatorMaxAgeDays": 4})


def check(data):
    return vs.validate(data, today=TODAY)


def with_value(path, value):
    data = static_data(TODAY)
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


def page(payload_text):
    return "<html><script>window.STATIC_DATA=" + payload_text + ";</script></html>"


def test_good_data_passes():
    assert check(static_data(TODAY)) == ([], [])


@pytest.mark.parametrize("path, value, kind, message", [
    (("generatedAt",), "2026-11-20T06:17:00+00:00", "error", "generatedAt 沒有台北時區"),
    (("generatedAt",), None, "error", "generatedAt 沒有台北時區"),
    (("portfolio", "positions"), [], "error", "portfolio.positions 是空的"),
    (("portfolio", "positions", 0, "price"), None, "error", "9901.TW 沒有有效價格"),
    (("portfolio", "positions", 0, "price"), 0, "error", "9901.TW 沒有有效價格"),
    (("portfolio", "positions", 1, "price"), "300", "error", "FAKE 沒有有效價格"),
    (("portfolio", "positions", 0, "conflict"), True, "warning", "9901.TW 報價來源不一致（twse）"),
    (("portfolio", "positions", 0, "stale"), True, "warning", "9901.TW 沒有即時價，沿用昨收"),
    (("portfolio", "fx", "isFallback"), True, "error", "保底匯率"),
    (("portfolio", "betaUnknown"), ["9901.TW"], "warning", "未填 Beta：9901.TW"),
    (("history", "dates"), [], "error", "歷史曲線沒有資料"),
    (("history", "missing"), ["GONE"], "error", "抓不到歷史價格：GONE"),
    (("history", "fxApproximated"), True, "warning", "匯率歷史取得失敗"),
    (("history", "totalValue"), [FAKE_TOTALS[0], FAKE_TOTALS[0] * 1.2, FAKE_TOTALS[0] * 1.2],
     "error", "目前持股總資產曲線在 2026-11-19 單日變動超過 15%"),
    (("history", "totalValue2025"), [1000.0, 1000.0, 0.0], "error", "2025 持股總資產曲線在 2026-11-20"),
    (("lohas", "charts"), {}, "error", "五線譜沒有任何商品資料"),
    (("lohas", "charts", "9901.TW", "closes"), [100.0, 50.0, 50.5],
     "error", "9901.TW 五線譜收盤價在 2026-11-19 單日變動 50.0%"),
    (("lohas", "charts", "9901.TW", "closes"), [100.0, None, 40.0],
     "error", "9901.TW 五線譜收盤價在 2026-11-20 單日變動 60.0%"),
    (("lohas", "charts", "FAKE", "chartData", 2, "close"), 2.5, "error", "FAKE 五線譜收盤價在 2026-11-20"),
    (("lohas", "failed"), ["GONE"], "warning", "五線譜取得失敗：GONE"),
    (VIX + ("value",), None, "warning", "VIX 資料暫缺"),
    (VIX + ("date",), "2026-11-15", "warning", "VIX 資料日期 2026-11-15 超過 4 天"),
    (VIX + ("date",), "11/20/2026", "warning", "VIX 資料日期 11/20/2026"),
    (VIX + ("date",), None, "warning", "VIX 資料日期 None"),
    (PE + ("value",), None, "warning", "Nasdaq 100 Forward P/E 資料暫缺"),
    (PE + ("isFallback",), True, "warning", "沿用上次成功值"),
    (("lohas", "fearGreed", "timestamp"), "2026-11-01T00:00:00+00:00", "warning", "CNN Fear & Greed 資料日期"),
    (("lohas", "fearGreed", "score"), None, "warning", "CNN Fear & Greed 資料暫缺"),
    (("lohas", "marketIndicators"), None, "warning", "VIX 資料暫缺"),
])
def test_each_rule_fires(path, value, kind, message):
    errors, warnings = check(with_value(path, value))
    hits, other = (errors, warnings) if kind == "error" else (warnings, errors)
    assert any(message in m for m in hits), hits
    assert other == []


@pytest.mark.parametrize("ratio, fails", [(1.14, False), (1.16, True), (0.86, False), (0.84, True)])
def test_history_jump_threshold(ratio, fails):
    errors, _ = check(with_value(("history", "totalValue"), [100.0, 100.0 * ratio, 100.0 * ratio]))
    assert bool(errors) is fails


@pytest.mark.parametrize("age, warns", [(0, False), (4, False), (5, True)])
def test_indicator_age_threshold(age, warns):
    day = (TODAY - datetime.timedelta(days=age)).isoformat()
    _, warnings = check(with_value(VIX + ("date",), day))
    assert bool(warnings) is warns


def test_cash_and_zero_share_positions_need_no_price():
    data = static_data(TODAY)
    positions = data["portfolio"]["positions"]
    positions[2]["price"] = None  # isCash
    positions.append({"ticker": "SOLD", "shares": 0.0, "price": None, "isCash": False})
    positions.append({"ticker": "twd", "shares": 5.0, "price": None})  # 代號本身就是現金
    assert check(data) == ([], [])


def test_missing_sections_are_errors_not_crashes():
    errors, warnings = check({"generatedAt": "2026-11-20T14:00:00+08:00"})
    assert errors == ["portfolio.positions 是空的", "歷史曲線沒有資料", "五線譜沒有任何商品資料"]
    assert len(warnings) == 3  # 三個指標都缺


# ------------------------------------------------------------------ 解析

def test_extract_round_trip():
    data = static_data(TODAY)
    assert vs.extract_static_data(page(json.dumps(data))) == data


@pytest.mark.parametrize("html, message", [
    ("<html></html>", "應恰好出現 1 次，實際 0 次"),
    (page("{}") + page("{}"), "實際 2 次"),
    (page('{"a": NaN}'), "含有 NaN"),
    (page('{"a": [Infinity]}'), "含有 Infinity"),
    (page('{"a": -Infinity}'), "含有 -Infinity"),
    (page("[1, 2]"), "必須是物件"),
    (page("{broken"), "無法解析"),
])
def test_extract_rejects_bad_payload(html, message):
    with pytest.raises(vs.ValidationError, match=re.escape(message)):
        vs.extract_static_data(html)


@pytest.mark.parametrize("values, expected", [
    ([], (0.0, None)),
    (None, (0.0, None)),
    ([100.0], (0.0, None)),
    ([100.0, 120.0, 114.0], (pytest.approx(0.2), 1)),
    ([100.0, 110.0, 120.0, 130.0], (pytest.approx(0.1), 1)),  # 和前一點比，不是和第一點比
    ([100.0, None, 40.0], (pytest.approx(0.6), 2)),  # 缺值不中斷比較
    ([100.0, 0.0, 100.0], (pytest.approx(1.0), 1)),  # 掉到 0 算 100% 變動
    ([100.0, "x", True, 101.0], (pytest.approx(0.01), 3)),
    ([0.0, -5.0, 10.0], (0.0, None)),  # 沒有正數基準
])
def test_max_jump(values, expected):
    assert vs.max_jump(values) == expected


# ------------------------------------------------------------------ 命令列

@pytest.fixture
def frozen_today(monkeypatch):
    monkeypatch.setattr(pdata, "today_tpe", lambda: TODAY)


def write_page(tmp_path, data):
    path = tmp_path / "index.html"
    path.write_text(page(json.dumps(data)), encoding="utf-8")
    return str(path)


def test_main_passes_good_page(tmp_path, capsys, frozen_today):
    assert vs.main([write_page(tmp_path, static_data(TODAY))]) == 0
    assert "資料品質檢查通過（0 項警告）" in capsys.readouterr().out


def test_warnings_fail_only_in_strict_mode(tmp_path, capsys, frozen_today):
    data = static_data(TODAY)
    data["lohas"]["failed"] = ["GONE"]
    path = write_page(tmp_path, data)
    assert vs.main([path]) == 0
    assert "::warning::五線譜取得失敗：GONE" in capsys.readouterr().out
    assert vs.main([path, "--strict"]) == 1
    assert "::error::五線譜取得失敗：GONE" in capsys.readouterr().out


def test_failure_output_has_no_amounts(tmp_path, capsys, frozen_today):
    data = static_data(TODAY)
    data["history"]["totalValue"][1] = FAKE_TOTALS[0] * 2
    data["portfolio"]["positions"][0].update(price=None, conflict=True)
    assert vs.main([write_page(tmp_path, data)]) == 1
    out = capsys.readouterr().out
    assert out.count("::error::") == 2 and out.count("::warning::") == 1
    assert "資料品質檢查失敗：2 項錯誤、1 項警告" in out
    for secret in (FAKE_TOTALS[0], FAKE_TOTALS[0] * 2, FAKE_SHARES, FAKE_PRICE):
        assert format(secret, "g") not in out and format(secret, ",.0f") not in out


def test_unparseable_page_fails(tmp_path, capsys):
    path = tmp_path / "index.html"
    path.write_text("<html>no data</html>", encoding="utf-8")
    assert vs.main([str(path)]) == 1
    assert "::error::window.STATIC_DATA 應恰好出現 1 次" in capsys.readouterr().out


# ------------------------------------------------------------------ debt.json（--debt）

FAKE_LOAN = {"bank": "測試銀行", "principal": 123456, "dueDay": 15, "monthlyPayment": 7890,
             "maturityDate": "2030-06-15", "asOfDate": "2026-01-01"}
DROP = object()


def one_loan(key=None, value=None):
    loan = dict(FAKE_LOAN)
    if key is not None:
        if value is DROP:
            del loan[key]
        else:
            loan[key] = value
    return [loan]


@pytest.mark.parametrize("key, value", [
    (None, None),
    ("principal", 0),        # 已還清、還沒從檔案移除
    ("principal", 1234.5),
    ("dueDay", 31.0),        # core.js 的 Number.isInteger(31.0) 也是 true
    ("maturityDate", "2024-02-29"),
])
def test_debt_good_loans_pass(key, value):
    assert vs.validate_debt(one_loan(key, value)) == ([], [])


@pytest.mark.parametrize("key, value, message", [
    ("bank", DROP, "bank 必須是非空字串"),
    ("bank", "  ", "bank 必須是非空字串"),
    ("bank", 123, "bank 必須是非空字串"),
    ("principal", DROP, "principal 必須是 ≥ 0 的數字"),
    ("principal", "123456", "principal 必須是 ≥ 0 的數字"),   # 字串（前端會算出 NaN 或靠隱性轉型）
    ("principal", "1,234,567", "principal 必須是 ≥ 0 的數字"),
    ("principal", -1, "principal 必須是 ≥ 0 的數字"),
    ("principal", True, "principal 必須是 ≥ 0 的數字"),
    ("monthlyPayment", None, "monthlyPayment 必須是 ≥ 0 的數字"),
    ("dueDay", 0, "dueDay 必須是 1–31 的整數"),
    ("dueDay", 32, "dueDay 必須是 1–31 的整數"),
    ("dueDay", 15.5, "dueDay 必須是 1–31 的整數"),
    ("dueDay", "15", "dueDay 必須是 1–31 的整數"),
    ("dueDay", True, "dueDay 必須是 1–31 的整數"),
    ("dueDay", DROP, "dueDay 必須是 1–31 的整數"),
    ("maturityDate", DROP, "maturityDate 必須是有效的 YYYY-MM-DD 日期"),
    ("maturityDate", "2030/06/15", "maturityDate 必須是有效的 YYYY-MM-DD 日期"),
    ("maturityDate", "2030-02-30", "maturityDate 必須是有效的 YYYY-MM-DD 日期"),
    ("maturityDate", "2030-6-15", "maturityDate 必須是有效的 YYYY-MM-DD 日期"),
    ("maturityDate", "２０３０-０６-１５", "maturityDate 必須是有效的 YYYY-MM-DD 日期"),  # 全形數字，core.js 也不接受
    ("asOfDate", "2026-13-01", "asOfDate 必須是有效的 YYYY-MM-DD 日期"),
    ("asOfDate", "", "asOfDate 必須是有效的 YYYY-MM-DD 日期"),
    ("asOfDate", 20260101, "asOfDate 必須是有效的 YYYY-MM-DD 日期"),
])
def test_debt_each_field_rule_fires(key, value, message):
    errors, warnings = vs.validate_debt(one_loan(key, value))
    assert errors == ["debt.json 第 1 筆：" + message]
    assert warnings == []


@pytest.mark.parametrize("value", [DROP, None])
def test_debt_missing_as_of_date_only_warns(value):
    assert vs.validate_debt(one_loan("asOfDate", value)) == (
        [], ["debt.json 第 1 筆沒有 asOfDate，借貸剩餘金額不會依還款日攤銷"])


@pytest.mark.parametrize("items, errors", [
    ([], []),
    ({"loans": []}, ["debt.json 最外層必須是陣列（沒有借貸時填 []）"]),
    (FAKE_LOAN, ["debt.json 最外層必須是陣列（沒有借貸時填 []）"]),
    (None, ["debt.json 最外層必須是陣列（沒有借貸時填 []）"]),
    ([FAKE_LOAN, "x"], ["debt.json 第 2 筆不是物件"]),
    ([FAKE_LOAN, dict(FAKE_LOAN, dueDay=0)], ["debt.json 第 2 筆：dueDay 必須是 1–31 的整數"]),
])
def test_debt_top_level_and_numbering(items, errors):
    assert vs.validate_debt(items) == (errors, [])


def write_debt(tmp_path, text, encoding="utf-8"):
    path = tmp_path / "debt.json"
    path.write_text(text, encoding=encoding)
    return str(path)


def test_main_checks_debt_only_when_asked(tmp_path, capsys, frozen_today):
    page_path = write_page(tmp_path, static_data(TODAY))
    write_debt(tmp_path, '{"loans": []}')  # 格式錯誤，但沒帶 --debt 就不讀
    assert vs.main([page_path]) == 0
    assert vs.main([page_path, "--debt", write_debt(tmp_path, json.dumps([FAKE_LOAN]))]) == 0
    assert "資料品質檢查通過（0 項警告）" in capsys.readouterr().out


def test_main_debt_failure_output_has_no_bank_or_amounts(tmp_path, capsys, frozen_today):
    bad = [dict(FAKE_LOAN, dueDay=32), dict(FAKE_LOAN, asOfDate=None)]
    debt_path = write_debt(tmp_path, json.dumps(bad, ensure_ascii=False))
    assert vs.main([write_page(tmp_path, static_data(TODAY)), "--debt", debt_path]) == 1
    out = capsys.readouterr().out
    assert "::error::debt.json 第 1 筆：dueDay 必須是 1–31 的整數" in out
    assert "::warning::debt.json 第 2 筆沒有 asOfDate" in out
    assert "資料品質檢查失敗：1 項錯誤、1 項警告" in out
    for secret in ("測試銀行", "123456", "123,456", "7890", "7,890"):
        assert secret not in out


def test_main_debt_warning_fails_in_strict_mode(tmp_path, capsys, frozen_today):
    debt_path = write_debt(tmp_path, json.dumps(one_loan("asOfDate", DROP)))
    page_path = write_page(tmp_path, static_data(TODAY))
    assert vs.main([page_path, "--debt", debt_path]) == 0
    assert vs.main([page_path, "--debt", debt_path, "--strict"]) == 1
    assert "::error::debt.json 第 1 筆沒有 asOfDate" in capsys.readouterr().out


@pytest.mark.parametrize("text, message", [
    ('[{"bank": "x", "principal": NaN}]', "debt.json 無法解析：含有 NaN"),
    ("[Infinity]", "debt.json 無法解析：含有 Infinity"),
    ("[{broken", "debt.json 無法解析："),
    ("", "debt.json 無法解析："),
])
def test_debt_file_parse_errors(tmp_path, text, message):
    errors, warnings = vs.validate_debt_file(write_debt(tmp_path, text))
    assert len(errors) == 1 and errors[0].startswith(message), errors
    assert warnings == []


def test_debt_file_missing_and_bom(tmp_path):
    errors, _ = vs.validate_debt_file(str(tmp_path / "missing.json"))
    assert len(errors) == 1 and errors[0].startswith("讀不到 debt.json：")
    bom_path = write_debt(tmp_path, json.dumps([FAKE_LOAN]), encoding="utf-8-sig")
    assert vs.validate_debt_file(bom_path) == ([], [])

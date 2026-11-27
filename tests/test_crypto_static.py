"""方案 B 靜態加密、無明文外洩閘門、私有資料目錄建置與 Web Crypto 跨語言互通測試（全部虛構資料）。"""

import datetime
import json
import os
import shutil
import subprocess

import pytest

import app as app_module
import build_static
import crypto_static
import portfolio_data as pdata
import validate_static
from fakes import FAKE_TOTALS, static_data

TODAY = datetime.date(2026, 11, 20)
TEST_PASS = "Correct-Horse-Battery-Staple-2026!"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_VERIFY = os.path.join(ROOT, "tests", "js", "verify_decrypt.js")


def _sample_private_dir(tmp_path):
    data_dir = tmp_path / "private_data"
    data_dir.mkdir()
    (data_dir / "portfolio.json").write_text(json.dumps([
        {"ticker": "9901.TW", "name": "測試持股", "shares": 887766, "beta": 1.0},
        {"ticker": "TWD-CASH", "name": "台幣現金", "shares": 5432100, "beta": 0.0},
    ], ensure_ascii=False), encoding="utf-8")
    (data_dir / "2025portfolio.json").write_text(json.dumps([
        {"ticker": "9901.TW", "name": "測試持股", "shares": 776655, "beta": 1.0},
    ], ensure_ascii=False), encoding="utf-8")
    (data_dir / "debt.json").write_text(json.dumps([
        {
            "bank": "測試機密銀行",
            "principal": 3456789,
            "dueDay": 15,
            "monthlyPayment": 61234,
            "maturityDate": "2031-06-15",
            "asOfDate": "2026-01-01",
        }
    ], ensure_ascii=False), encoding="utf-8")
    return data_dir


def test_encrypt_decrypt_round_trip_and_random_nonce():
    payload = static_data(TODAY)
    env1 = crypto_static.encrypt_payload(payload, TEST_PASS)
    env2 = crypto_static.encrypt_payload(payload, TEST_PASS)
    assert env1["salt"] != env2["salt"]
    assert env1["iv"] != env2["iv"]
    assert env1["ct"] != env2["ct"]
    assert crypto_static.decrypt_payload(env1, TEST_PASS) == payload
    assert crypto_static.decrypt_payload(env2, TEST_PASS) == payload


def test_wrong_passphrase_and_tampered_ciphertext_fail():
    env = crypto_static.encrypt_payload({"ok": True}, TEST_PASS)
    with pytest.raises(crypto_static.CryptoError, match="解密失敗"):
        crypto_static.decrypt_payload(env, "Wrong-Passphrase-123!")

    tampered = dict(env, aad="wrong-aad")
    with pytest.raises(crypto_static.CryptoError, match="解密失敗"):
        crypto_static.decrypt_payload(tampered, TEST_PASS)


def test_weak_parameters_rejected():
    with pytest.raises(crypto_static.CryptoError, match="密碼長度"):
        crypto_static.encrypt_payload({"a": 1}, "short")
    with pytest.raises(crypto_static.CryptoError, match="疊代次數"):
        crypto_static.encrypt_payload({"a": 1}, TEST_PASS, iterations=1000)


def test_encrypt_html_and_encrypted_validation_gate(tmp_path, monkeypatch, capsys, fixed_config):
    data_dir = _sample_private_dir(tmp_path)
    fake_bundle = static_data(TODAY)
    fake_bundle["debt"] = json.loads((data_dir / "debt.json").read_text(encoding="utf-8"))

    monkeypatch.setattr(app_module, "build_portfolio", lambda _h: fake_bundle["portfolio"])
    monkeypatch.setattr(app_module, "build_lohas_bundle", lambda _h: fake_bundle["lohas"])
    monkeypatch.setattr(pdata, "get_history", lambda _h, holdings_2025=None: fake_bundle["history"])

    out_html = tmp_path / "dist" / "index.html"
    build_static.main([
        "--data-dir", str(data_dir),
        "--debt", str(data_dir / "debt.json"),
        "--output", str(out_html),
    ])

    # build_static.py stdout 絕不能印出總資產金額
    build_log = capsys.readouterr().out
    assert format(FAKE_TOTALS[-1], ",.0f") not in build_log
    assert "測試機密銀行" not in build_log
    assert "3,456,789" not in build_log

    # 加密前：明文 HTML 包含內嵌 debt 並通過明文品質閘門
    plain_html = out_html.read_text(encoding="utf-8")
    parsed_plain = validate_static.extract_static_data(plain_html)
    assert parsed_plain["debt"][0]["bank"] == "測試機密銀行"
    assert validate_static.main([str(out_html), "--debt", str(data_dir / "debt.json")]) == 0

    # 加密後：不再包含 window.STATIC_DATA=、通過加密與無外洩閘門與 CLI verify
    monkeypatch.setenv("SITE_PASSPHRASE", TEST_PASS)
    assert crypto_static.main(["encrypt", str(out_html), "--passphrase-env", "SITE_PASSPHRASE"]) == 0
    assert crypto_static.main(["verify", str(out_html), "--passphrase-env", "SITE_PASSPHRASE"]) == 0
    assert validate_static.main([
        str(out_html), "--encrypted", "--no-leak-from", str(data_dir)
    ]) == 0

    enc_html = out_html.read_text(encoding="utf-8")
    assert "window.STATIC_DATA=" not in enc_html
    assert enc_html.count("window.ENCRYPTED_DATA=") == 1
    assert enc_html.count("window.APP_CONFIG=") == 1
    for secret in ("測試機密銀行", "3456789", "3,456,789", "61234", "887766", "5432100", TEST_PASS):
        assert secret not in enc_html

    # 確認沒有任何 log 洩漏密碼或機密值
    all_out = capsys.readouterr().out
    for secret in ("測試機密銀行", "3456789", TEST_PASS):
        assert secret not in all_out


def test_leak_detector_catches_plaintext_bank_and_amounts(tmp_path):
    data_dir = _sample_private_dir(tmp_path)
    base_html = app_module.render_page(static_data(TODAY))
    enc_html = crypto_static.encrypt_html(base_html, TEST_PASS)
    assert validate_static.validate_encrypted_html(enc_html, no_leak_dir=str(data_dir)) == ([], [])

    # 刻意注入機密銀行名與本金 → 應被抓到且錯誤訊息不回吐機密值
    leaked_html = enc_html + "<!-- 測試機密銀行 3456789 5432100 -->"
    errors, _ = validate_static.validate_encrypted_html(leaked_html, no_leak_dir=str(data_dir))
    assert len(errors) == 3
    joined = "\n".join(errors)
    assert "debt.json 第 1 筆欄位 bank" in joined
    assert "debt.json 第 1 筆欄位 principal" in joined
    assert "portfolio.json 第 2 筆欄位 shares" in joined
    for secret in ("測試機密銀行", "3456789", "5432100"):
        assert secret not in joined


def test_webcrypto_node_decrypts_python_ciphertext(tmp_path):
    """跨語言驗證：Python crypto_static.py 加密的 HTML 必須可由 Node Web Crypto API（同瀏覽器）正確解密。"""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("CI 必須安裝 node 才能驗證 Web Crypto 跨語言解密相容性")
        pytest.skip("本機沒有 node（CI 會執行這項測試）")

    payload = static_data(TODAY)
    payload["debt"] = [{"bank": "測試銀行", "principal": 123456}]
    plain_html = app_module.render_page(payload)
    enc_html = crypto_static.encrypt_html(plain_html, TEST_PASS)
    html_file = tmp_path / "encrypted.html"
    html_file.write_text(enc_html, encoding="utf-8")

    env = dict(os.environ, SITE_PASSPHRASE=TEST_PASS)
    proc = subprocess.run(
        [node, JS_VERIFY, str(html_file)],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == payload

"""靜態頁面資料加密工具：以 PBKDF2-HMAC-SHA256 + AES-256-GCM 加密 window.STATIC_DATA。

用途（方案 B：Private Data Repo + 公開 GitHub Pages + 前端密碼鎖）：
  1. build_static.py 從私有資料倉儲建置含明文 window.STATIC_DATA 的 dist/index.html。
  2. validate_static.py 在加密前執行資料品質閘門。
  3. 本模組把 window.STATIC_DATA=...; 替換為 window.ENCRYPTED_DATA=...;，
     讓瀏覽器在客戶端透過 Web Crypto API（crypto.subtle）離線解密。

安全設計：
  - KDF：PBKDF2-HMAC-SHA256，預設 600,000 次疊代（OWASP 建議值），每次建置產生全新 16-byte 隨機 salt。
  - AEAD：AES-256-GCM，每次建置產生全新 12-byte 隨機 nonce（IV），綁定 AAD（portfolio-static-v1）。
  - 任何命令列輸出絕不印出密碼、解密後資料或金額。

用法：
    python crypto_static.py encrypt dist/index.html --passphrase-env SITE_PASSPHRASE
    python crypto_static.py verify  dist/index.html --passphrase-env SITE_PASSPHRASE
"""

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

STATIC_PREFIX = "window.STATIC_DATA="
ENCRYPTED_PREFIX = "window.ENCRYPTED_DATA="

ENVELOPE_VERSION = 1
CIPHER_ALG = "AES-256-GCM"
KDF_ALG = "PBKDF2-SHA256"
DEFAULT_ITERATIONS = 600_000
MIN_ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12
KEY_BYTES = 32
DEFAULT_AAD = "portfolio-static-v1"


class CryptoError(Exception):
    """加密、解密或信封格式錯誤。"""


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(text: str, label: str) -> bytes:
    if not isinstance(text, str) or not text:
        raise CryptoError("加密信封欄位 " + label + " 必須是非空字串")
    try:
        return base64.b64decode(text, validate=True)
    except ValueError as error:
        raise CryptoError("加密信封欄位 " + label + " 不是合法的 Base64") from error


def _js_json(obj) -> str:
    """安全內嵌於 <script> 的緊湊 JSON（跳脫 '<' 避免提前關閉 script 標籤）。"""
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).replace(
        "<", "\\u003c"
    )


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise CryptoError("解密密碼不可為空")
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, dklen=KEY_BYTES)


def encrypt_payload(data, passphrase: str, iterations: int = DEFAULT_ITERATIONS, aad: str = DEFAULT_AAD) -> dict:
    """把 JSON 可序列化物件加密成與瀏覽器 Web Crypto API 相容的信封 dict。"""
    if iterations < MIN_ITERATIONS:
        raise CryptoError("PBKDF2 疊代次數不可低於 " + str(MIN_ITERATIONS))
    if not isinstance(passphrase, str) or len(passphrase) < 8:
        raise CryptoError("密碼長度至少需 8 個字元")

    plaintext = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    key = _derive_key(passphrase, salt, iterations)
    ciphertext = AESGCM(key).encrypt(iv, plaintext, aad.encode("utf-8"))

    return {
        "v": ENVELOPE_VERSION,
        "alg": CIPHER_ALG,
        "kdf": KDF_ALG,
        "iter": iterations,
        "salt": _b64encode(salt),
        "iv": _b64encode(iv),
        "aad": aad,
        "ct": _b64encode(ciphertext),
    }


def validate_envelope(envelope) -> dict:
    """檢查加密信封結構與參數強度；不合格拋出 CryptoError。"""
    if not isinstance(envelope, dict):
        raise CryptoError("ENCRYPTED_DATA 必須是物件")
    if envelope.get("v") != ENVELOPE_VERSION:
        raise CryptoError("不支援的加密信封版本：" + str(envelope.get("v")))
    if envelope.get("alg") != CIPHER_ALG:
        raise CryptoError("不支援的加密演算法：" + str(envelope.get("alg")))
    if envelope.get("kdf") != KDF_ALG:
        raise CryptoError("不支援的金鑰衍生演算法：" + str(envelope.get("kdf")))
    iterations = envelope.get("iter")
    if not isinstance(iterations, int) or isinstance(iterations, bool) or iterations < MIN_ITERATIONS:
        raise CryptoError("PBKDF2 疊代次數不足（至少 " + str(MIN_ITERATIONS) + "）")
    aad = envelope.get("aad")
    if not isinstance(aad, str) or not aad:
        raise CryptoError("加密信封缺少有效的 aad")
    salt = _b64decode(envelope.get("salt"), "salt")
    if len(salt) < SALT_BYTES:
        raise CryptoError("salt 長度不足（至少 " + str(SALT_BYTES) + " bytes）")
    iv = _b64decode(envelope.get("iv"), "iv")
    if len(iv) != IV_BYTES:
        raise CryptoError("AES-GCM iv 必須為 " + str(IV_BYTES) + " bytes")
    ct = _b64decode(envelope.get("ct"), "ct")
    if len(ct) <= 16:  # 至少包含 16-byte GCM tag + 1-byte payload
        raise CryptoError("密文長度異常")
    return envelope


def decrypt_payload(envelope: dict, passphrase: str):
    """驗證並解密信封 → 原始 Python 物件；密碼錯誤或密文遭竄改拋出 CryptoError。"""
    validate_envelope(envelope)
    salt = _b64decode(envelope["salt"], "salt")
    iv = _b64decode(envelope["iv"], "iv")
    ct = _b64decode(envelope["ct"], "ct")
    aad = envelope["aad"].encode("utf-8")
    key = _derive_key(passphrase, salt, envelope["iter"])
    try:
        plaintext = AESGCM(key).decrypt(iv, ct, aad)
    except InvalidTag as error:
        raise CryptoError("解密失敗（密碼錯誤或密文遭竄改）") from error
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise CryptoError("解密後的資料不是有效的 JSON") from error


def _extract_json_assignment(html: str, prefix: str):
    """定位並解碼 `<prefix><json>` → (parsed_obj, start_idx, end_idx)。"""
    count = html.count(prefix)
    if count != 1:
        raise CryptoError(prefix + " 應恰好出現 1 次，實際 " + str(count) + " 次")

    def reject_constant(name):
        raise CryptoError(prefix + " 含有 " + name)

    decoder = json.JSONDecoder(parse_constant=reject_constant)
    assign_start = html.index(prefix)
    json_start = assign_start + len(prefix)
    try:
        obj, json_end = decoder.raw_decode(html, json_start)
    except ValueError as error:
        raise CryptoError(prefix + " 無法解析：" + str(error)) from error
    return obj, assign_start, json_end


def extract_encrypted_envelope(html: str) -> dict:
    """從加密後的 HTML 取出並驗證 window.ENCRYPTED_DATA 信封。"""
    obj, _start, _end = _extract_json_assignment(html, ENCRYPTED_PREFIX)
    return validate_envelope(obj)


def encrypt_html(html: str, passphrase: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """將 HTML 中的明文 window.STATIC_DATA=... 替換成加密的 window.ENCRYPTED_DATA=...。"""
    if ENCRYPTED_PREFIX in html:
        raise CryptoError("HTML 已包含 " + ENCRYPTED_PREFIX + "，拒絕重複加密")
    static_data, assign_start, json_end = _extract_json_assignment(html, STATIC_PREFIX)
    if not isinstance(static_data, dict):
        raise CryptoError("STATIC_DATA 必須是物件")

    envelope = encrypt_payload(static_data, passphrase, iterations=iterations)
    encrypted_assignment = ENCRYPTED_PREFIX + _js_json(envelope)
    out = html[:assign_start] + encrypted_assignment + html[json_end:]

    if STATIC_PREFIX in out:
        raise CryptoError("加密後仍殘留 " + STATIC_PREFIX)
    if out.count(ENCRYPTED_PREFIX) != 1:
        raise CryptoError("加密後 " + ENCRYPTED_PREFIX + " 應恰好出現 1 次")
    return out


def _atomic_write(path: str, content: str) -> None:
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".crypto-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_passphrase(env_name: str) -> str:
    value = os.environ.get(env_name, "")
    if not value:
        raise CryptoError("環境變數 " + env_name + " 未設定或為空")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="靜態頁 window.STATIC_DATA 的 AES-256-GCM 加密與驗證工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_enc = sub.add_parser("encrypt", help="將明文靜態 HTML 的 STATIC_DATA 加密為 ENCRYPTED_DATA")
    p_enc.add_argument("html", help="輸入 HTML 路徑")
    p_enc.add_argument("--output", help="輸出 HTML 路徑（預設覆寫原檔）")
    p_enc.add_argument("--passphrase-env", default="SITE_PASSPHRASE", help="讀取密碼的環境變數名稱")
    p_enc.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS, help="PBKDF2 疊代次數")

    p_ver = sub.add_parser("verify", help="在記憶體中解密驗證 HTML（不輸出任何個人資料）")
    p_ver.add_argument("html", help="已加密 HTML 路徑")
    p_ver.add_argument("--passphrase-env", default="SITE_PASSPHRASE", help="讀取密碼的環境變數名稱")

    args = parser.parse_args(argv)
    try:
        passphrase = _read_passphrase(args.passphrase_env)
        with open(args.html, "r", encoding="utf-8") as handle:
            html = handle.read()
        if args.cmd == "encrypt":
            encrypted_html = encrypt_html(html, passphrase, iterations=args.iterations)
            _atomic_write(args.output or args.html, encrypted_html)
            print(f"已完成加密（{CIPHER_ALG}，{KDF_ALG} {args.iterations:,} 次疊代）")
        else:
            if STATIC_PREFIX in html:
                raise CryptoError("偵測到未加密的 " + STATIC_PREFIX)
            envelope = extract_encrypted_envelope(html)
            data = decrypt_payload(envelope, passphrase)
            if not isinstance(data, dict) or not data.get("generatedAt"):
                raise CryptoError("解密後缺少必要欄位 generatedAt")
            print(f"加密驗證通過（{envelope['alg']}，{envelope['kdf']} {envelope['iter']:,} 次疊代）")
    except (OSError, CryptoError) as error:
        print("::error::" + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

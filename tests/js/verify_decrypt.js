"use strict";
// 驗證瀏覽器端 Web Crypto API（crypto.subtle）能正確解密 crypto_static.py 產生的 window.ENCRYPTED_DATA。
// 演算法與參數與 portfolio.html 的 decryptEnvelope 完全一致。
// 用法：SITE_PASSPHRASE=... node tests/js/verify_decrypt.js <encrypted_html_path>

const fs = require("fs");
const { webcrypto } = require("crypto");

function b64ToBytes(b64) {
    return new Uint8Array(Buffer.from(b64, "base64"));
}

async function decryptEnvelope(env, passphrase) {
    if (!env || env.v !== 1 || env.alg !== "AES-256-GCM" || env.kdf !== "PBKDF2-SHA256") {
        throw new Error("Unsupported envelope format");
    }
    const enc = new TextEncoder();
    const keyMaterial = await webcrypto.subtle.importKey(
        "raw", enc.encode(passphrase), { name: "PBKDF2" }, false, ["deriveKey"]
    );
    const aesKey = await webcrypto.subtle.deriveKey(
        { name: "PBKDF2", salt: b64ToBytes(env.salt), iterations: Number(env.iter), hash: "SHA-256" },
        keyMaterial,
        { name: "AES-GCM", length: 256 },
        false,
        ["decrypt"]
    );
    const plainBuf = await webcrypto.subtle.decrypt(
        { name: "AES-GCM", iv: b64ToBytes(env.iv), additionalData: enc.encode(String(env.aad || "")) },
        aesKey,
        b64ToBytes(env.ct)
    );
    return JSON.parse(new TextDecoder().decode(plainBuf));
}

async function main() {
    const htmlPath = process.argv[2];
    const passphrase = process.env.SITE_PASSPHRASE || "";
    if (!htmlPath || !passphrase) {
        console.error("Usage: SITE_PASSPHRASE=... node verify_decrypt.js <html_path>");
        process.exit(2);
    }
    const html = fs.readFileSync(htmlPath, "utf8");
    const prefix = "window.ENCRYPTED_DATA=";
    const idx = html.indexOf(prefix);
    if (idx < 0) throw new Error("Missing window.ENCRYPTED_DATA=");
    const after = html.slice(idx + prefix.length);
    const endIdx = after.indexOf(";</script>");
    if (endIdx < 0) throw new Error("Missing closing script tag after ENCRYPTED_DATA");
    const env = JSON.parse(after.slice(0, endIdx));
    const data = await decryptEnvelope(env, passphrase);
    process.stdout.write(JSON.stringify(data));
}

main().catch(err => {
    console.error(err && err.message ? err.message : String(err));
    process.exit(1);
});

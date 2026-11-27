"""靜態建置腳本：把即時報價、歷史回溯、樂活五線譜、市場指標與借貸資料烘焙進靜態 HTML。

GitHub Pages 只服務靜態檔案、前端又受 CORS 限制無法直接抓多數報價來源，
因此在 CI（或本機）先跑這支腳本抓好資料，注入 portfolio.html 產生靜態頁面；
公開部署前再由 validate_static.py 檢查資料品質，並由 crypto_static.py 加密。

用法：
    python build_static.py                                                   # 讀取預設資料目錄，產生 ./index.html
    python build_static.py --data-dir _private_data --output dist/index.html # 從私有資料目錄建置並內嵌 debt.json
    python build_static.py --output /tmp/test.html                           # 輸出到其他位置（本機測試用）

注意：本腳本的 stdout 會在公開 GitHub Actions log 中顯示，因此只印代號、日期、
來源統計與狀態，絕不印出總資產金額、股數或借貸銀行與金額。
"""

import argparse
import datetime
import os

import app  # 重用 load_holdings / load_2025_holdings / load_debt / build_* / render_page
import portfolio_data

DEFAULT_OUTPUT = os.path.join(app.BASE_DIR, "index.html")
STATIC_PREFIX = "window.STATIC_DATA="


def build_html(static_data):
    """注入資料並確認注入點恰好一個（模板改版找不到標記時直接失敗，不產出空頁面）。"""
    html = app.render_page(static_data)
    count = html.count(STATIC_PREFIX)
    if count != 1:
        raise SystemExit("window.STATIC_DATA 應恰好注入 1 次，實際 " + str(count) + " 次，停止建置")
    return html


def _print_quote_notes(portfolio):
    """只印代號與來源，絕不印金額。"""
    positions = portfolio["positions"]
    notes = (
        ("缺價未計入", portfolio.get("missingPrice") or []),
        ("來源不一致", [p["ticker"] for p in positions if p.get("conflict")]),
        ("沿用昨收", [p["ticker"] for p in positions if p.get("stale")]),
        ("未填 Beta", portfolio.get("betaUnknown") or []),
    )
    for label, tickers in notes:
        if tickers:
            print("  " + label + "：" + ", ".join(tickers))
    fx = portfolio.get("fx") or {}
    print("  匯率來源：" + str(fx.get("source")) + ("（保底值）" if fx.get("isFallback") else ""))


def _load_debt_for_build(args):
    if args.debt:
        if not os.path.exists(args.debt):
            raise SystemExit("找不到指定的借貸檔：" + args.debt)
        return app.load_debt(args.debt)
    if args.data_dir:
        candidate = os.path.join(args.data_dir, "debt.json")
        if os.path.exists(candidate):
            return app.load_debt(candidate)
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description="產生 GitHub Pages 用的靜態 index.html")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="輸出路徑（預設 repo 根目錄 index.html）")
    parser.add_argument("--data-dir", help="私有資料目錄（含 portfolio.json、2025portfolio.json、debt.json）")
    parser.add_argument("--debt", help="指定 debt.json 路徑（會一併內嵌至 STATIC_DATA 供後續加密）")
    args = parser.parse_args(argv)

    holdings = app.load_holdings(data_dir=args.data_dir) if args.data_dir else app.load_holdings()
    if not holdings:
        raise SystemExit("portfolio.json 沒有任何持股，無法建置。")
    holdings_2025 = (
        app.load_2025_holdings(data_dir=args.data_dir) if args.data_dir else app.load_2025_holdings()
    )
    debt_items = _load_debt_for_build(args)

    portfolio_data.reset_source_stats()
    print(f"抓取 {len(holdings)} 檔即時報價…")
    portfolio = app.build_portfolio(holdings)

    print("計算歷史回溯…")
    history = portfolio_data.get_history(holdings, holdings_2025=holdings_2025)

    print("計算樂活五線譜 + Fear & Greed + 市場指標…")
    lohas = app.build_lohas_bundle(holdings)

    generated_at = datetime.datetime.now(portfolio_data.TPE).isoformat(timespec="seconds")
    static_data = {
        "portfolio": portfolio,
        "history": history,
        "lohas": lohas,
        "generatedAt": generated_at,
    }
    if debt_items is not None:
        static_data["debt"] = debt_items
    html = build_html(static_data)

    output = os.path.abspath(args.output)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as handle:
        handle.write(html)

    print(f"已產生 {output}")
    beta = portfolio.get("portfolioBeta")
    print("  加權 Beta：" + (format(beta, ".4f") if beta is not None else "—（沒有任何持股填 Beta）"))
    print("  內嵌借貸資料：" + (f"是（{len(debt_items)} 筆）" if debt_items is not None else "否"))
    print(f"  歷史區間：{history['dates'][0]} ~ {history['dates'][-1]}"
          if history["dates"] else "  （無歷史資料）")
    print(f"  資料時間：{generated_at}")
    _print_quote_notes(portfolio)
    if history.get("missing"):
        print("  無歷史資料：" + ", ".join(history["missing"]))
    if history.get("backfilled"):
        print("  上市前以首日價格回補：" + ", ".join(
            item["ticker"] + "（" + item["firstDate"] + " 起）" for item in history["backfilled"]
        ))
    if history.get("fxApproximated"):
        print("  匯率歷史取得失敗，美股部位以目前匯率近似")
    if lohas.get("failed"):
        print("  五線譜取得失敗：" + ", ".join(lohas["failed"]))
    print("  來源統計（成功/失敗/無資料）：")
    for source, stats in portfolio_data.get_source_stats().items():
        print(f"    {source}: {stats['ok']}/{stats['fail']}/{stats['empty']}")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# 常用操作一鍵完成（不需要另外裝 make）。用法：scripts/dev.sh <指令>
#
#   install   安裝本機 Flask 與測試需要的套件（requirements-dev.txt）
#   test      跑離線測試（Python + core.js golden；本機沒有 node 時 JS 部分會略過）
#   test-js   用 node 在 UTC / 台北 / 紐約三個時區跑 core.js golden
#   golden    重新產生 tests/golden/*.json（只在「刻意改規則」時用，產生後要檢查 git diff）
#   check     push 前檢查：測試 → 用真實資料建置到暫存檔 → 資料品質閘門（不動 repo 的 index.html）
#   run       啟動本機 Flask 模式（http://127.0.0.1:5000）
#   pull      git pull --ff-only
#   publish   check 通過後列出變更、經你確認才 commit（只含已追蹤檔案）並 push
#             push 到 main 會觸發公開部署；新檔案請先自行 git add 並確認內容。
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON="${PYTHON:-python3}"

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

cmd="${1:-help}"
[ $# -gt 0 ] && shift

case "$cmd" in
    install)
        "$PYTHON" -m pip install -r requirements-dev.txt
        ;;
    test)
        "$PYTHON" -m pytest -p no:cacheprovider "$@"
        ;;
    test-js)
        command -v node >/dev/null || { echo "找不到 node" >&2; exit 1; }
        for tz in UTC Asia/Taipei America/New_York; do
            TZ="$tz" node tests/js/run_golden.js
        done
        ;;
    golden)
        "$PYTHON" tests/make_golden.py
        git --no-pager diff --stat -- tests/golden || true
        ;;
    check)
        "$PYTHON" -m pytest -p no:cacheprovider -q
        data_dir="${PORTFOLIO_DATA_DIR:-../portfolio_private_data}"
        [ -f "$data_dir/portfolio.json" ] || { echo "找不到 $data_dir/portfolio.json（請設定 PORTFOLIO_DATA_DIR 或將 portfolio_private_data 放在相鄰目錄）" >&2; exit 1; }
        tmp="$(mktemp -d)"
        trap 'rm -rf "$tmp"' EXIT
        "$PYTHON" build_static.py --data-dir "$data_dir" --debt "$data_dir/debt.json" --output "$tmp/index.html"
        "$PYTHON" validate_static.py "$tmp/index.html" --debt "$data_dir/debt.json"
        ;;
    run)
        "$PYTHON" app.py
        ;;
    pull)
        git pull --ff-only
        ;;
    publish)
        message="${1:-}"
        [ -n "$message" ] || { echo '用法：scripts/dev.sh publish "commit 訊息"' >&2; exit 1; }
        [ -t 0 ] || { echo "publish 需要在互動式終端機執行（要人工確認變更）" >&2; exit 1; }
        "$0" check
        echo
        git status --short
        untracked="$(git ls-files --others --exclude-standard)"
        if [ -n "$untracked" ]; then
            echo
            echo "以下未追蹤的新檔案「不會」被 commit（需要的話先自行 git add 並確認內容）："
            echo "$untracked" | sed 's/^/  /'
        fi
        echo
        git --no-pager diff --stat HEAD
        echo
        read -r -p "確認以上已追蹤檔案的變更要 commit 並 push（會觸發公開部署）？輸入 yes 繼續：" answer
        [ "$answer" = "yes" ] || { echo "已取消"; exit 1; }
        git commit -a -m "$message"
        git push
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        echo "未知指令：$cmd" >&2
        usage >&2
        exit 1
        ;;
esac

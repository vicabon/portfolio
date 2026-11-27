#!/usr/bin/env bash
# 由外部排程觸發 deploy.yml（workflow_dispatch），補足 GitHub 內建排程「盡力而為」常被丟棄的問題
# （11/03–11/23 實測每小時排程只執行 14.3%，見 README「排程與監控」）。
#
# 需求：
#   - GitHub CLI（gh）
#   - fine-grained personal access token：Repository access 只選這個 repo，
#     權限只開 Actions: Read and write（不要給 Contents 寫入或其他 repo）。
#     token 放在 GH_TOKEN 環境變數，或放進檔案（chmod 600）並用 GH_TOKEN_FILE 指定；不要寫進 repo。
#
# crontab 範例（每小時第 7 分；與 workflow 內建排程的第 17 分錯開）：
#   7 * * * *  GH_TOKEN_FILE=$HOME/.config/portfolio/gh_token /path/to/repo/scripts/trigger_deploy.sh >> $HOME/.cache/portfolio-trigger.log 2>&1
#
# repo 預設由這個目錄的 git remote 推得；要指定其他 repo 時設 PORTFOLIO_REPO=<owner>/<repo>。
set -euo pipefail

cd "$(dirname "$0")/.."

if [ -z "${GH_TOKEN:-}" ] && [ -n "${GH_TOKEN_FILE:-}" ]; then
    GH_TOKEN="$(cat "$GH_TOKEN_FILE")"
    export GH_TOKEN
fi

repo_args=()
if [ -n "${PORTFOLIO_REPO:-}" ]; then
    repo_args=(--repo "$PORTFOLIO_REPO")
fi

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) 觸發 deploy.yml"
gh workflow run deploy.yml ${repo_args[@]+"${repo_args[@]}"} --ref main

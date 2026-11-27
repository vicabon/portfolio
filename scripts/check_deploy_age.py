"""監控：最後一次成功部署（deploy.yml）超過門檻就回報，可選擇自動開 GitHub Issue 通知。

用法：
    python scripts/check_deploy_age.py                 # 預設門檻 120 分鐘
    python scripts/check_deploy_age.py --max-age 90
    python scripts/check_deploy_age.py --issue         # 超過門檻時開 Issue（已有同標題的未關閉 Issue 就不重開）

結束碼：0 正常、1 超過門檻、2 查不到紀錄或 gh 失敗。
需求：GitHub CLI（gh）。token 權限：Actions: Read（--issue 另需 Issues: Read and write）。
token 放在 GH_TOKEN，或放進檔案（chmod 600）並用 GH_TOKEN_FILE 指定（與 scripts/trigger_deploy.sh 相同）。
gh 一律在 repo 根目錄執行（cron 的工作目錄通常是家目錄），所以不加 --repo 也能由 git remote 推得 repo。
建議和 scripts/trigger_deploy.sh 放在同一台常開機器的 crontab，例如每小時第 47 分執行一次。
只輸出時間與分鐘數，不讀取也不輸出任何持股資料。
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

ISSUE_TITLE = "靜態頁資料超過 {hours} 小時未更新"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gh_env():
    """GH_TOKEN 沒設、但有 GH_TOKEN_FILE 時，從檔案讀 token（讀不到會拋 OSError，由 main 回報）。"""
    env = dict(os.environ)
    path = env.get("GH_TOKEN_FILE")
    if not env.get("GH_TOKEN") and path:
        with open(path, encoding="utf-8") as handle:
            env["GH_TOKEN"] = handle.read().strip()
    return env


def _gh(args):
    proc = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60,
                          cwd=REPO_ROOT, env=_gh_env())
    if proc.returncode != 0:
        raise RuntimeError("gh " + " ".join(args[:2]) + " 失敗：" + proc.stderr.strip()[:200])
    return proc.stdout


def last_success_time(repo=None):
    """最後一次成功的 deploy.yml 執行時間（UTC aware datetime）；沒有紀錄回傳 None。"""
    args = ["run", "list", "--workflow", "deploy.yml", "--status", "success", "--limit", "1",
            "--json", "updatedAt"] + (["--repo", repo] if repo else [])
    runs = json.loads(_gh(args) or "[]")
    if not runs:
        return None
    return datetime.datetime.fromisoformat(runs[0]["updatedAt"].replace("Z", "+00:00"))


def age_minutes(then, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return int((now - then).total_seconds() // 60)


def open_issue_once(title, body, repo=None):
    """同標題的 Issue 還開著就不重複開；回傳是否新開。"""
    repo_args = ["--repo", repo] if repo else []
    existing = json.loads(_gh(["issue", "list", "--state", "open", "--search", title + " in:title",
                               "--json", "title"] + repo_args) or "[]")
    if any(item.get("title") == title for item in existing):
        return False
    _gh(["issue", "create", "--title", title, "--body", body] + repo_args)
    return True


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description="檢查最後一次成功部署距今多久")
    parser.add_argument("--max-age", type=int, default=120, help="門檻（分鐘），預設 120")
    parser.add_argument("--issue", action="store_true", help="超過門檻時開 GitHub Issue")
    parser.add_argument("--repo", help="<owner>/<repo>；預設由目前目錄的 git remote 推得")
    args = parser.parse_args(argv)

    try:
        then = last_success_time(args.repo)
    except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("ERROR：" + str(error), file=sys.stderr)
        return 2
    if then is None:
        print("ERROR：找不到任何成功的 deploy.yml 執行紀錄", file=sys.stderr)
        return 2

    age = age_minutes(then, now)
    if age <= args.max_age:
        print("OK：最後成功部署在 " + str(age) + " 分鐘前")
        return 0

    message = ("最後一次成功部署在 " + then.strftime("%Y-%m-%d %H:%M UTC") + "（" + str(age)
               + " 分鐘前），超過門檻 " + str(args.max_age) + " 分鐘。")
    print("ALERT：" + message, file=sys.stderr)
    if args.issue:
        title = ISSUE_TITLE.format(hours=format(round(args.max_age / 60, 1), "g"))  # 120 → "2"、90 → "1.5"
        body = message + "\n\n請檢查 Actions 的 deploy.yml 執行紀錄（排程是否被丟棄、資料品質閘門是否擋下）與外部觸發排程（scripts/trigger_deploy.sh）。"
        try:
            print("已開 Issue：" + title if open_issue_once(title, body, args.repo) else "已有未關閉的同名 Issue，不重複開")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            print("ERROR：開 Issue 失敗：" + str(error), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

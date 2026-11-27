"""部署新鮮度監控（scripts/check_deploy_age.py）：gh 一律換成假的，不連網。"""

import datetime
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import check_deploy_age as monitor  # noqa: E402

NOW = datetime.datetime(2026, 11, 24, 8, 0, tzinfo=datetime.timezone.utc)


@pytest.fixture
def gh(monkeypatch):
    """state["runs"]：gh run list 的回應；state["issues"]：未關閉的 Issue；state["calls"]：呼叫紀錄。"""
    state = {"runs": [], "issues": [], "calls": [], "fail": None, "tokens": []}
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN_FILE", raising=False)

    def fake_run(cmd, capture_output, text, timeout, cwd, env):
        assert cmd[0] == "gh" and capture_output and text and timeout
        assert cwd == monitor.REPO_ROOT  # cron 的工作目錄是家目錄，gh 要在 repo 裡才推得出 repo
        args = cmd[1:]
        state["calls"].append(args)
        state["tokens"].append(env.get("GH_TOKEN"))
        if state["fail"] and args[0] == state["fail"]:
            return subprocess.CompletedProcess(cmd, 1, "", "HTTP 401: Bad credentials")
        if args[:2] == ["run", "list"]:
            out = state["runs"]
        elif args[:2] == ["issue", "list"]:
            out = state["issues"]
        elif args[:2] == ["issue", "create"]:
            state["issues"].append({"title": args[args.index("--title") + 1]})
            out = ""
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(cmd, 0, out if isinstance(out, str) else json.dumps(out), "")

    monkeypatch.setattr(monitor.subprocess, "run", fake_run)
    return state


def minutes_ago(n):
    return (NOW - datetime.timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("age, code", [(0, 0), (120, 0), (121, 1), (60 * 7, 1)])
def test_threshold(gh, capsys, age, code):
    gh["runs"] = [{"updatedAt": minutes_ago(age)}]
    assert monitor.main([], now=NOW) == code
    out = capsys.readouterr()
    assert (str(age) + " 分鐘前") in (out.out + out.err)


def test_custom_threshold_and_repo(gh):
    gh["runs"] = [{"updatedAt": minutes_ago(100)}]
    assert monitor.main(["--max-age", "90", "--repo", "owner/repo"], now=NOW) == 1
    assert gh["calls"][0][-2:] == ["--repo", "owner/repo"]
    assert "--status" in gh["calls"][0] and "success" in gh["calls"][0]


def test_issue_is_opened_once(gh, capsys):
    gh["runs"] = [{"updatedAt": minutes_ago(300)}]
    assert monitor.main(["--issue"], now=NOW) == 1
    assert monitor.main(["--issue"], now=NOW) == 1
    creates = [c for c in gh["calls"] if c[:2] == ["issue", "create"]]
    assert len(creates) == 1
    assert creates[0][creates[0].index("--title") + 1] == "靜態頁資料超過 2 小時未更新"
    assert "不重複開" in capsys.readouterr().out


def test_no_issue_when_fresh(gh):
    gh["runs"] = [{"updatedAt": minutes_ago(5)}]
    assert monitor.main(["--issue"], now=NOW) == 0
    assert all(c[0] != "issue" for c in gh["calls"])


@pytest.mark.parametrize("runs", [[], ""])
def test_no_successful_run(gh, capsys, runs):
    gh["runs"] = runs
    assert monitor.main([], now=NOW) == 2
    assert "找不到" in capsys.readouterr().err


def test_gh_failure(gh, capsys):
    gh["fail"] = "run"
    assert monitor.main([], now=NOW) == 2
    assert "Bad credentials" in capsys.readouterr().err


def test_token_file(gh, tmp_path, monkeypatch):
    token = tmp_path / "gh_token"
    token.write_text("fake-token-value\n", encoding="utf-8")
    monkeypatch.setenv("GH_TOKEN_FILE", str(token))
    gh["runs"] = [{"updatedAt": minutes_ago(5)}]
    assert monitor.main([], now=NOW) == 0
    assert gh["tokens"] == ["fake-token-value"]


def test_existing_gh_token_wins_over_file(gh, monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "from-env")
    monkeypatch.setenv("GH_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    gh["runs"] = [{"updatedAt": minutes_ago(5)}]
    assert monitor.main([], now=NOW) == 0
    assert gh["tokens"] == ["from-env"]


def test_unreadable_token_file(gh, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GH_TOKEN_FILE", str(tmp_path / "missing"))
    assert monitor.main([], now=NOW) == 2
    assert gh["calls"] == [] and "ERROR" in capsys.readouterr().err


def test_issue_failure_still_reports_alert(gh, capsys):
    gh["runs"] = [{"updatedAt": minutes_ago(300)}]
    gh["fail"] = "issue"
    assert monitor.main(["--issue"], now=NOW) == 1
    err = capsys.readouterr().err
    assert "ALERT" in err and "開 Issue 失敗" in err

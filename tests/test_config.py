"""config.json 載入：註解鍵不外流、缺欄位直接拋錯、注入前端的是複本。"""

import json

import pytest

import portfolio_data


def _write(tmp_path, cfg):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_repo_config_has_every_required_key():
    cfg = portfolio_data.load_config()
    assert cfg["fx"]["min"] < cfg["fx"]["fallbackRate"] < cfg["fx"]["max"]
    assert cfg["forwardPe"]["min"] < cfg["forwardPe"]["max"]
    assert 0 < cfg["freshness"]["warnMinutes"] < cfg["freshness"]["alertMinutes"]
    assert cfg["lohas"]["window"] > 1 and cfg["lohas"]["displayWindow"] > 0
    assert 0 < cfg["consensus"]["twoSourceMaxDiff"] < 1
    assert portfolio_data.days_old(cfg["history"]["start"]) is not None
    assert portfolio_data.days_old(cfg["lohas"]["start"]) is not None


def test_comment_keys_are_stripped(tmp_path):
    cfg = portfolio_data.load_config()
    raw = json.loads(json.dumps(cfg))
    raw["_comment"] = "說明"
    raw["fx"]["_comment"] = "說明"
    raw["extraLohasTickers"] = [{"_note": "x", "keep": 1}]
    loaded = portfolio_data.load_config(_write(tmp_path, raw))
    assert "_comment" not in loaded and "_comment" not in loaded["fx"]
    assert loaded["extraLohasTickers"] == [{"keep": 1}]


def test_public_config_has_no_comment_text():
    text = json.dumps(portfolio_data.public_config(), ensure_ascii=False)
    assert "_comment" not in text


@pytest.mark.parametrize("dotted", ["fx.min", "consensus.staleGapSeconds", "cashTickers", "validation.lohasMaxJump"])
def test_missing_key_raises(tmp_path, dotted):
    raw = json.loads(json.dumps(portfolio_data.load_config()))
    node = raw
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    del node[parts[-1]]
    with pytest.raises(ValueError, match="config.json 缺少設定"):
        portfolio_data.load_config(_write(tmp_path, raw))


def test_section_that_is_not_an_object_counts_as_missing(tmp_path):
    raw = json.loads(json.dumps(portfolio_data.load_config()))
    raw["fx"] = 32
    with pytest.raises(ValueError, match="fx.min"):
        portfolio_data.load_config(_write(tmp_path, raw))


def test_public_config_is_a_copy():
    cfg = portfolio_data.public_config()
    cfg["fx"]["min"] = -1
    cfg["cashTickers"].append("X")
    assert portfolio_data.CONFIG["fx"]["min"] != -1
    assert "X" not in portfolio_data.CONFIG["cashTickers"]

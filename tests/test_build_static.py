"""靜態建置：注入點檢查、輸出路徑、產出的頁面能通過資料品質閘門（資料來源全部換成虛構資料）。"""

import datetime

import pytest

pytest.importorskip("flask")

import app  # noqa: E402
import build_static  # noqa: E402
import portfolio_data as pdata  # noqa: E402
import validate_static  # noqa: E402
from fakes import static_data  # noqa: E402

TODAY = datetime.date(2026, 11, 20)


@pytest.fixture(autouse=True)
def default_output(tmp_path, monkeypatch):
    """測試絕不寫到 repo 的 index.html。"""
    path = tmp_path / "default-index.html"
    monkeypatch.setattr(build_static, "DEFAULT_OUTPUT", str(path))
    return path


@pytest.fixture
def pipeline(monkeypatch, fixed_config):
    data = static_data(TODAY)
    seen = {}

    def history(holdings, holdings_2025=None):
        seen["history"] = (holdings, holdings_2025)
        return data["history"]

    monkeypatch.setattr(app, "load_holdings", lambda: [{"ticker": "9901.TW", "shares": 1.0}])
    monkeypatch.setattr(app, "load_2025_holdings", lambda: [{"ticker": "OLD", "shares": 2.0}])
    monkeypatch.setattr(app, "build_portfolio", lambda holdings: data["portfolio"])
    monkeypatch.setattr(app, "build_lohas_bundle", lambda holdings: data["lohas"])
    monkeypatch.setattr(pdata, "get_history", history)
    return data, seen


@pytest.mark.parametrize("count", [0, 2])
def test_build_html_requires_single_injection(monkeypatch, count):
    monkeypatch.setattr(app, "render_page", lambda _data: "<html>" + build_static.STATIC_PREFIX * count + "</html>")
    with pytest.raises(SystemExit, match="應恰好注入 1 次"):
        build_static.build_html({})


def test_output_round_trips_and_passes_validation(tmp_path, pipeline, default_output):
    data, seen = pipeline
    output = tmp_path / "out.html"
    build_static.main(["--output", str(output)])
    assert not default_output.exists()

    parsed = validate_static.extract_static_data(output.read_text(encoding="utf-8"))
    for key in ("portfolio", "history", "lohas"):
        assert parsed[key] == data[key]
    assert parsed["generatedAt"].endswith("+08:00")
    assert seen["history"] == ([{"ticker": "9901.TW", "shares": 1.0}], [{"ticker": "OLD", "shares": 2.0}])
    assert validate_static.validate(parsed, today=TODAY) == ([], [])


def test_default_output_path(pipeline, default_output):
    build_static.main([])
    html = default_output.read_text(encoding="utf-8")
    assert html.count("window.STATIC_DATA=") == 1 and html.count("window.APP_CONFIG=") == 1


def test_no_holdings_stops_before_fetching(monkeypatch, tmp_path):
    monkeypatch.setattr(app, "load_holdings", lambda: [])

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("沒有持股時不應抓資料")

    monkeypatch.setattr(app, "build_portfolio", must_not_run)
    with pytest.raises(SystemExit, match="沒有任何持股"):
        build_static.main(["--output", str(tmp_path / "x.html")])
    assert not (tmp_path / "x.html").exists()

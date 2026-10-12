import pytest

from groww_pulse.__main__ import build_parser, main, parse_iso_week


@pytest.mark.parametrize(
    "week",
    [
        "2026-W54",
        "2026-W00",
        "2025-W53",
        "2026-w41",
        "202641",
        "not-a-week",
    ],
)
def test_rejects_non_iso_week(week: str, capsys: pytest.CaptureFixture[str]):
    with pytest.raises(ValueError, match="invalid ISO week"):
        parse_iso_week(week)
    assert main(["publish", "--week", week]) == 2
    assert "invalid ISO week" in capsys.readouterr().err


def test_parse_iso_week_returns_monday():
    assert parse_iso_week("2026-W41").isoformat() == "2026-10-05"


def test_serve_binds_localhost_unless_a_host_is_passed():
    local = build_parser().parse_args(["serve"])
    public = build_parser().parse_args(["serve", "--host", "0.0.0.0", "--port", "8080"])

    assert local.host == "127.0.0.1"
    assert local.port == 8000
    assert public.host == "0.0.0.0"
    assert public.port == 8080

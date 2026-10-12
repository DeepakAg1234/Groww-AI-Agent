import os
from pathlib import Path

import pytest

from groww_pulse.config import load_config, load_env_file

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yaml"


def test_default_config_loads_groww_play_app():
    config = load_config(CONFIG)
    assert config.product_name == "Groww"
    assert config.window_weeks == 10
    assert config.apps[0].store == "play"
    assert config.apps[0].app_id == "com.nextbillion.groww"
    assert config.max_words == 280


@pytest.mark.parametrize("window_weeks", [7, 13, 0])
def test_window_weeks_outside_range_fails(tmp_path: Path, window_weeks: int):
    path = tmp_path / "config.yaml"
    text = CONFIG.read_text(encoding="utf-8")
    path.write_text(
        text.replace("window_weeks: 10", f"window_weeks: {window_weeks}", 1),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="window_weeks"):
        load_config(path)


def test_env_file_fills_missing_keys_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "# comment",
                "ANTHROPIC_API_KEY=from-file",
                'PULSE_MCP_TOKEN="from-file"',
                "ALREADY_SET=from-file",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ALREADY_SET", "from-env")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PULSE_MCP_TOKEN", raising=False)

    load_env_file(env_file)

    assert os.environ["ANTHROPIC_API_KEY"] == "from-file"
    assert os.environ["PULSE_MCP_TOKEN"] == "from-file"
    assert os.environ["ALREADY_SET"] == "from-env"


def test_window_weeks_bounds_load(tmp_path: Path):
    text = CONFIG.read_text(encoding="utf-8")
    for window_weeks in (8, 12):
        path = tmp_path / f"config-{window_weeks}.yaml"
        path.write_text(
            text.replace("window_weeks: 10", f"window_weeks: {window_weeks}", 1),
            encoding="utf-8",
        )
        assert load_config(path).window_weeks == window_weeks

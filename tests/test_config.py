

def test_no_start_times_must_be_in_order():
    import pytest as _pt

    from conftest import make_cfg as _mk
    from questersboard.config import ConfigError
    with _pt.raises(ConfigError, match="no_start_cancel_minutes"):
        _mk(scheduling={"no_start_ask_minutes": 90, "no_start_cancel_minutes": 60})
    assert _mk(scheduling={"no_start_ask_minutes": 0, "no_start_cancel_minutes": 30})

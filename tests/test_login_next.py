"""After login people only ever go back to a page inside the member area (no open redirect)."""
import pytest

from questersboard.web.routes_member import _local_next

MP = "/wow/quester"


@pytest.mark.parametrize("value,expected", [
    ("/wow/quester/events/abc123", "/wow/quester/events/abc123"),
    ("/wow/quester/me?tab=characters", "/wow/quester/me?tab=characters"),
    ("/wow/quester/", "/wow/quester/"),
    ("", "/wow/quester/"),
    ("https://evil.example/", "/wow/quester/"),
    ("//evil.example/wow/quester/", "/wow/quester/"),
    ("/wow/quester//evil.example", "/wow/quester/"),
    ("/wow/quester/../../admin", "/wow/quester/"),
    ("/wow/quester/\\\\evil.example", "/wow/quester/"),
    ("/wow/quester/x\r\nLocation: https://evil.example", "/wow/quester/"),
    ("/wow/questerevil", "/wow/quester/"),
    (None, "/wow/quester/"),
])
def test_local_next(value, expected):
    assert _local_next(value, MP) == expected

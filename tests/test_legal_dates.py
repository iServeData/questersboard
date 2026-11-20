"""The privacy and terms pages carry their own Last updated date, kept with the page."""
from questersboard.web import legal_dates


def test_each_legal_page_change_comes_with_a_new_date():
    for page, (_date, sha) in legal_dates.PAGES.items():
        assert legal_dates.fingerprint(page) == sha, (
            f"{page}.html changed: set its Last updated date in web/legal_dates.py and the fingerprint "
            f"from `python -m questersboard.web.legal_dates`")


def test_the_later_of_the_page_and_config_dates_is_shown():
    code = legal_dates.PAGES["privacy"][0]
    assert legal_dates.legal_date("privacy", "") == code
    assert legal_dates.legal_date("privacy", "2000-01-01") == code
    assert legal_dates.legal_date("privacy", "2999-12-31") == "2999-12-31"

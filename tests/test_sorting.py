"""Every dropdown is in alphanumerical order (A to Z ignoring case, numbers in number order)."""
from __future__ import annotations

import re
from pathlib import Path

from questersboard.sorting import alpha, alpha_filter, natural_key

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "questersboard" / "web" / "templates"


def test_natural_order():
    assert alpha(["Level 10", "level 9", "Arena", "2 hours", "10 hours"]) == ["2 hours", "10 hours", "Arena", "level 9", "Level 10"]
    assert natural_key("Tank") == natural_key("tank")
    assert alpha_filter({"b": "Zul", "a": "Mara", "c": "dun"}) == [("c", "dun"), ("a", "Mara"), ("b", "Zul")]
    assert [x["n"] for x in alpha_filter([{"n": "b"}, {"n": "A"}], "n")] == ["A", "b"]
    assert alpha_filter([(1, "Zed"), (2, "Abe")], 1) == [(2, "Abe"), (1, "Zed")]
    assert alpha_filter(["x2", "x10", "x1"], lambda s: s) == ["x1", "x2", "x10"]


def test_every_template_loop_in_a_select_is_sorted():
    """A {% for %} that writes <option>s inside a <select> goes through |alpha, so a new dropdown can't
    slip in unsorted. Fixed lists written out by hand are checked by eye; datalists aren't dropdowns."""
    bad = []
    for f in TEMPLATES.glob("*.html"):
        for m in re.finditer(r"<select\b.*?</select>", f.read_text(), re.S):
            for loop in re.findall(r"\{%-?\s*for [^%]*%\}", m.group(0)):
                if "|alpha" not in loop and " in (\"" not in loop and "(false, true)" not in loop \
                        and "[(60," not in loop and " in ((" not in loop and "zs if" not in loop and "groups" not in loop:
                    bad.append(f"{f.name}: {loop}")
    assert not bad, bad

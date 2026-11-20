"""Alphanumerical order for every dropdown (web forms and Discord menus): A to Z ignoring case, with
numbers in number order ("Level 9" before "Level 10"). Choices like Any, None or Not set stay first;
they are written outside the sorted list."""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

_PARTS = re.compile(r"(\d+)")


def natural_key(text: Any) -> tuple:
    s = str(text or "").casefold().strip()
    return tuple((0, int(x), "") if x.isdigit() else (1, 0, x) for x in _PARTS.split(s) if x)


def alpha(items: Iterable, key: Callable[[Any], Any] | None = None) -> list:
    """Sorted copy of items by natural_key(key(item)) (key defaults to the item itself)."""
    return sorted(items, key=lambda it: natural_key(key(it) if key else it))


def alpha_filter(items, by: Any = None) -> list:
    """Jinja filter. dict: its (key, value) pairs ordered by the value, or by=attribute name of the value
    ("name" for editions). list: by an index (1 for (value, label) pairs), an attribute, a function
    (ruleset_label) or the item."""
    if isinstance(items, dict):
        pairs = list(items.items())
        if by is None:
            return alpha(pairs, lambda kv: kv[1])
        return alpha(pairs, lambda kv: _get(kv[1], by))
    if by is None:
        return alpha(items)
    return alpha(items, lambda it: _get(it, by))


def _get(it: Any, by: Any) -> Any:
    if callable(by):
        return by(it)
    if isinstance(by, int):
        return it[by]
    if isinstance(it, dict):
        return it.get(by)
    return getattr(it, by, "")

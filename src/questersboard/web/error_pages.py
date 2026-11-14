"""Themed error pages: every error QuestersBoard answers itself (404, 429, 500 and the rest) gets the
community site's error layout, with the code on a notice pinned to the quest board.

code: (short name, notice title, heading, text, notice line). Codes not listed use the 4xx or 5xx entry.
"""

from __future__ import annotations

PAGES: dict[int, tuple[str, str, str, str, str]] = {
    400: ("Bad request", "Garbled scroll", "That request didn't make sense.",
          "Your browser sent something QuestersBoard couldn't read. Reload the page and try again.",
          "Fix: reload the page"),
    401: ("Login needed", "Unknown adventurer", "Log in first.",
          "This part of QuestersBoard is for community members. Log in with Discord to carry on.",
          "Requires: Discord login"),
    403: ("Forbidden", "Members only", "This notice isn't posted for you.",
          "You don't have the role this needs. If you think you should, ask an officer in Discord.",
          "Requires: the right role"),
    404: ("Not found", "Quest not found", "No notice on the board for that.",
          "The page, event or story you're after isn't here. It may have been taken down, or the link has a typo.",
          "Reward: one working link"),
    405: ("Method not allowed", "Wrong way in", "That's not how this page is used.",
          "This address only works from the buttons on QuestersBoard's own pages. Start again from the board.",
          "Try: the page's own buttons"),
    408: ("Request timeout", "Took too long", "The request timed out.",
          "Your browser took too long to send the request. Try once more.",
          "Try: once more"),
    409: ("Conflict", "Already claimed", "Someone changed this first.",
          "This was changed while you were looking at it. Reload the page, check it, and try again.",
          "Fix: reload and check"),
    410: ("Gone", "Quest retired", "This was taken off the board.",
          "What used to be here was removed on purpose and isn't coming back.",
          "Status: gone for good"),
    413: ("Too large", "Pack's too heavy", "That file is too big.",
          "The file or form you sent is over the size QuestersBoard accepts. Try a smaller one.",
          "Limit: shown on the form"),
    414: ("Address too long", "Scroll too long", "That link is too long.",
          "The address is longer than QuestersBoard accepts. Start again from the board.",
          "Fix: start from the board"),
    415: ("Unsupported file", "Wrong kind of file", "That file type isn't accepted.",
          "QuestersBoard takes PNG, JPEG and WebP pictures. Save it as one of those and try again.",
          "Accepted: PNG, JPEG, WebP"),
    422: ("Unprocessable", "Couldn't read that", "Something in the form didn't fit.",
          "One of the values wasn't what QuestersBoard expected. Go back, check the form and try again.",
          "Fix: check the form"),
    429: ("Too many requests", "Slow your mount", "Too many requests.",
          "You're moving faster than the board can keep up. Wait a minute, then try again.",
          "Cooldown: about 60 seconds"),
    431: ("Headers too large", "Too much baggage", "The browser sent too much.",
          "Your browser sent more than QuestersBoard accepts, usually old cookies. Clear this site's cookies "
          "and try again.", "Fix: clear cookies for the site"),
    500: ("Server error", "The board fell over", "Something broke on our side.",
          "It's not you. Try again in a few minutes. If it keeps happening, let an officer know in Discord.",
          "Bounty: report it in Discord"),
    501: ("Not implemented", "Not on the board yet", "QuestersBoard can't do that.",
          "That isn't something QuestersBoard does (yet). Start again from the board.",
          "Status: not built"),
    502: ("Bad gateway", "Courier lost", "A service didn't answer properly.",
          "Something QuestersBoard relies on sent back a bad reply. Try again in a few minutes.",
          "Try: in a few minutes"),
    503: ("Service unavailable", "Closed for maintenance", "The board is being repainted.",
          "QuestersBoard is unavailable for a moment, usually for an update. Try again in a few minutes.",
          "Back: shortly"),
    504: ("Gateway timeout", "Courier's late", "A service took too long to answer.",
          "Something QuestersBoard relies on didn't answer in time. Try again in a few minutes.",
          "Try: in a few minutes"),
}
GENERIC = {
    4: ("Request problem", "Notice torn down", "Something went wrong with that request.",
        "QuestersBoard couldn't do what was asked. Go back and try again, or start again from the board.",
        "Try: start from the board"),
    5: ("Server problem", "The board fell over", "Something went wrong on our side.",
        "It's not you. Try again in a few minutes. If it keeps happening, let an officer know in Discord.",
        "Bounty: report it in Discord"),
}


def page(code: int) -> dict:
    short, title, heading, text, line = PAGES.get(code) or GENERIC[5 if code >= 500 else 4]
    return {"code": code, "short": short, "notice": title, "heading": heading, "text": text, "line": line}

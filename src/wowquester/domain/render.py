"""Safe rendering of member-written text: Markdown with raw HTML disabled, then an allow-list sanitizer."""

from __future__ import annotations

import re

import bleach
from markdown_it import MarkdownIt

from ..config import Config
from . import privacy

_md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False}).disable(["image"])
TAGS = ["p", "em", "strong", "ul", "ol", "li", "blockquote", "h3", "h4", "br", "code", "a", "hr"]
ATTRS = {"a": ["href", "title", "rel"]}
OUTCOMES = {"completed": "Completed", "partial": "Partly completed", "not_completed": "Not completed",
            "social": "Social / no objective", "unknown": "Outcome not recorded"}


def _link_attrs(attrs, new=False):
    href = attrs.get((None, "href"), "")
    if not href.startswith("https://"):
        return None
    attrs[(None, "rel")] = "nofollow noopener noreferrer ugc"
    return attrs


def markdown(text: str) -> str:
    html = _md.render(text or "")
    html = re.sub(r"<h[12]>", "<h3>", html)
    html = re.sub(r"</h[12]>", "</h3>", html)
    html = re.sub(r"<(/?)h[56]>", r"<\1h4>", html)
    clean = bleach.clean(html, tags=TAGS, attributes=ATTRS, protocols=["https"], strip=True)
    return bleach.linkify(clean, callbacks=[_link_attrs], skip_tags=["code"], parse_email=False)


def plain(text: str, n: int) -> str:
    t = re.sub(r"[#*_>`\[\]()~-]+", " ", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= n else t[: n - 1].rsplit(" ", 1)[0] + "…"


def compose_template(rev) -> str:
    """Fallback (and default) story body built only from what the author wrote."""
    parts = []
    if rev.attempted.strip():
        parts.append("### What we set out to do\n\n" + rev.attempted.strip())
    if rev.happened.strip():
        parts.append("### What happened\n\n" + rev.happened.strip())
    if rev.moments.strip():
        parts.append("### Moments\n\n" + rev.moments.strip())
    return "\n\n".join(parts)


def story_html(cfg: Config, rev, people: list[privacy.Participant]) -> tuple[str, str]:
    body = rev.body.strip() or compose_template(rev)
    body = privacy.redact(body, people, cfg)
    summary_src = privacy.redact(rev.happened or rev.attempted or rev.body, people, cfg)
    html = f'<p class="wq-outcome">Outcome: {bleach.clean(OUTCOMES.get(rev.outcome, ""))}</p>' + markdown(body)
    return html, plain(summary_src, 300)

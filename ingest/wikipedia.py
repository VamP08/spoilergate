"""Wikipedia episode summaries: {{Episode list}} ShortSummary fields.

Each ShortSummary is bound to exactly one episode row by construction —
the position tag is free. Text is CC BY-SA 4.0, stored verbatim with
attribution (source_url per unit).
"""
import re

import mwparserfromhell

from ingest import http

API = "https://en.wikipedia.org/w/api.php"
UA = "SpoilerGate/0.1 (jayanisanjay@gmail.com)"


def fetch_wikitext(page: str) -> str | None:
    r = http.get(
        API,
        params={
            "action": "parse",
            "page": page,
            "prop": "wikitext",
            "format": "json",
            "formatversion": 2,
            "redirects": 1,
        },
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        return None
    return data["parse"]["wikitext"]


def find_episode_page(show_title: str) -> tuple[str, str] | None:
    """Return (page_title, wikitext) for the page that holds the episode tables."""
    for candidate in (
        f"List of {show_title} episodes",
        show_title,
        f"{show_title} (TV series)",
    ):
        text = fetch_wikitext(candidate)
        if text and "{{Episode list" in text:
            return candidate, text
    return None


def collect_rows(wikitext: str) -> list[dict]:
    """Episode rows for a list page. Big shows transclude {{:Show season N}}
    subpages that hold the actual summaries; when those exist, use only them
    (the list page's own tables are extras like minisodes)."""
    transcluded = re.findall(r"\{\{:([^}|#]+)\}\}", wikitext)
    if transcluded:
        rows = []
        for page in transcluded:
            text = fetch_wikitext(page.strip())
            if text:
                rows.extend(parse_summaries(text))
        if rows:
            return rows
    return parse_summaries(wikitext)


def parse_summaries(wikitext: str) -> list[dict]:
    """All {{Episode list}} rows in document order: [{title, summary, links}].

    `links` is [(target, display)] kept from the summary before markup is
    stripped — Wikipedia links a character on first mention, so those links
    date every character against the episode for free. See ingest/characters.py.
    """
    code = mwparserfromhell.parse(wikitext)
    rows = []
    for tpl in code.filter_templates():
        if not tpl.name.strip().lower().startswith("episode list"):
            continue
        title = tpl.get("Title").value.strip_code().strip() if tpl.has("Title") else ""
        links: list[tuple[str, str | None]] = []
        summary = ""
        if tpl.has("ShortSummary"):
            value = tpl.get("ShortSummary").value
            links = [
                (str(link.title).strip(), str(link.text).strip() if link.text else None)
                for link in value.filter_wikilinks()
            ]
            summary = value.strip_code().strip()
        rows.append({"title": title.strip('"'), "summary": summary, "links": links})
    return rows


def match_to_spine(spine: list[dict], rows: list[dict]) -> list[dict]:
    """Attach summaries to spine episodes. By sequence when counts line up,
    else by normalized title. Unmatched episodes keep an empty summary."""
    def norm(t: str) -> str:
        return "".join(c for c in t.lower() if c.isalnum())

    if len(rows) == len(spine):
        pairs = zip(spine, rows)
    else:
        by_title = {norm(r["title"]): r for r in rows if r["title"]}
        pairs = ((ep, by_title.get(norm(ep["title"]))) for ep in spine)

    out = []
    for ep, row in pairs:
        out.append({**ep,
                    "summary": (row or {}).get("summary", ""),
                    "links": (row or {}).get("links", [])})
    return out

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


def search_titles(term: str, limit: int = 6) -> list[str]:
    """Wikipedia's own search, for shows whose page is not where we guessed."""
    r = http.get(
        API,
        params={"action": "query", "list": "search", "srsearch": term,
                "srlimit": limit, "format": "json", "formatversion": 2},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    return [hit["title"] for hit in r.json().get("query", {}).get("search", [])]


def _normalise(title: str) -> str:
    return "".join(c for c in title.lower() if c.isalnum())


def titles_match(spine: list[dict], rows: list[dict], need: float = 0.3) -> bool:
    """Does this page describe the show we asked about?

    Searching Wikipedia for an ambiguous title returns pages that genuinely have
    episode tables belonging to a different show — "Ghosts" turns up Scooby-Doo.
    Taking the first page with episodes would index another show's plot under
    this show's name, which is worse than finding nothing, so a candidate has to
    prove itself: its episode titles must overlap the ones TVMaze already gave us.
    """
    ours = {_normalise(u["title"]) for u in spine if u.get("title")}
    theirs = {_normalise(r["title"]) for r in rows if r.get("title")}
    ours.discard("")
    theirs.discard("")
    if not ours or not theirs:
        return False
    return len(ours & theirs) / min(len(ours), len(theirs)) >= need


def locate_episode_page(show_name: str, spine: list[dict],
                        year: str = "") -> tuple[str, list[dict]]:
    """The page holding this show's episode summaries, and its parsed rows.

    Guessed titles first, since they are one request and cover most shows. Only
    when they miss does it fall back to search, and anything search returns has
    to pass `titles_match`.
    """
    found = find_episode_page(show_name)
    if found:
        rows = collect_rows(found[1])
        if titles_match(spine, rows):
            return found[0], rows

    for candidate in search_titles(f"List of {show_name} episodes {year}".strip()):
        if _normalise(show_name) not in _normalise(candidate):
            continue                      # a page about some other show entirely
        text = fetch_wikitext(candidate)
        if not text or "{{Episode list" not in text:
            continue
        rows = collect_rows(text)
        if titles_match(spine, rows):
            return candidate, rows
    return "", []


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

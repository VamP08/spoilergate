"""Entities and their first appearance, derived without an LLM.

Wikipedia links a character on first mention inside an episode summary, and
each summary is bound to one episode — so the links alone date every character
against `abs_order`, which is exactly what the post-guard needs. No character
page to parse (their formats vary wildly: headings, giant tables, or no page at
all), no extra request, and `[[Helena Eagan|Helly]]` hands over the alias too.

Summaries also link ordinary terms (`[[lactation consultant]]`,
`[[Board of directors|board]]`). Those are separated by name shape, not
discarded: see `entities_from_units`.
"""
import re

from server.core import name_pattern  # one definition of "this name occurs here"

# A person's name: two or more capitalised words. Rules out "lactation
# consultant", "Board of directors", "Diethyl ether" — all lowercase after the
# first word — while keeping "Mark Scout" and "Jesse Pinkman".
PERSON = re.compile(r"^[A-Z][\w'.-]*(?: [A-Z][\w'.-]*)+$")
PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def clean_target(target: str) -> str:
    """'Walter White (Breaking Bad)' -> 'Walter White'. Wikipedia disambiguates
    fictional characters by their show, which is noise for matching."""
    return PARENTHETICAL.sub("", target.split("#")[0]).strip()


SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv"}

# Words that begin a name without being one. "The Hood" gave a given name of
# "The", which matches every summary ever written, so it dated to episode one
# and turned up in the character list as a character called "The". Honorifics
# are here for the same reason and take "Queen Consolidated" and "The Wall" out
# of the character list with them.
NOT_A_NAME = {
    "the", "a", "an", "his", "her", "their", "my", "our",
    "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "miss", "dr", "dr.", "doctor",
    "sir", "lord", "lady", "king", "queen", "prince", "princess", "captain",
    "detective", "agent", "officer", "sergeant", "professor", "father",
    "mother", "uncle", "aunt", "saint", "st.", "general", "colonel",
}


def is_person(name: str) -> bool:
    return bool(PERSON.match(name)) and "," not in name


def is_character_name(name: str) -> bool:
    """Stricter than `is_person`, for typing entities that came from a link.

    "Gray Matter Technologies", "Los Pollos Hermanos" and "United States
    Environmental Protection Agency" all pass `is_person` — capitalised words
    in a row — and all three turned up in the character list. Organisations run
    long; people are two words, plus a generational suffix.

    Only used for link-derived entities. The billed cast is authoritative about
    who is a character, so it keeps the loose test. Getting this wrong costs a
    name missing from a browsable list; it never costs a block, because the
    post-guard uses every entity regardless of type.
    """
    if not is_person(name):
        return False
    words = name.split()
    if words[0].lower() in NOT_A_NAME:
        return False
    if len(words) == 3 and words[-1].lower() in SUFFIXES:
        return True
    return len(words) == 2


NICKNAME = re.compile(r"^(.*?)\s*['\"]([^'\"]+)['\"]\s*(.*)$")


def cast_variants(raw: str) -> tuple[str, list[str]]:
    """TVMaze writes the nickname into the name: "Gustavo 'Gus' Fring" becomes
    ("Gustavo Fring", ["Gus Fring", "Gus"]) — the primary name plus the forms a
    summary or an answer is likely to actually use."""
    m = NICKNAME.match(raw)
    if not m:
        return raw.strip(), []
    first, nick, last = (p.strip() for p in m.groups())
    primary = " ".join(p for p in (first, last) if p)
    variants = [" ".join(p for p in (nick, last) if p), nick]
    return primary, [v for v in dict.fromkeys(variants) if v and v != primary]


def given_name_variants(cast: list[str]) -> dict[str, str]:
    """Given names that identify exactly one billed character.

    Summaries introduce "Irving Bailiff" once, if at all, and say "Irving"
    forever after — so matching only the billed name drops him entirely, and
    Severance's character list came out with three of its leads instead of
    eight. A given name shared by two cast members identifies neither, so those
    are left out rather than guessed.
    """
    firsts = {}
    counts: dict[str, int] = {}
    for raw in cast:
        primary, _ = cast_variants(raw)
        parts = primary.split()
        if len(parts) >= 2 and len(parts[0]) >= 3 and parts[0].lower() not in NOT_A_NAME:
            firsts[raw] = parts[0]
            counts[parts[0]] = counts.get(parts[0], 0) + 1
    return {raw: first for raw, first in firsts.items() if counts[first] == 1}


def entities_from_cast(cast: list[str], units: list[tuple[int, str]]) -> list[dict]:
    """Date the billed cast against the summaries.

    Wikipedia links a character on first mention, but episode tables rarely
    link the series regulars — so the link pass finds the guests and misses the
    leads. It matters less than it sounds (a regular appears from episode one,
    so gating never blocks them) but a character list without the leads is a
    poor one, and the cast list is one request.
    """
    ordered = sorted(units)
    unambiguous_first = given_name_variants(cast)
    found = []
    for raw in cast:
        primary, variants = cast_variants(raw)
        if not is_person(primary):
            continue
        first = unambiguous_first.get(raw)
        forms = [primary, *variants, *([first] if first else [])]
        patterns = [(form, name_pattern(form)) for form in forms]
        for abs_order, text in ordered:
            matched = [form for form, pattern in patterns if pattern.search(text)]
            if not matched:
                continue
            # Billing is not usage: TVMaze says "Michael 'Mike' Ehrmantraut" but
            # the summaries only ever say "Mike", and a question about the name
            # nobody writes retrieves nothing. Lead with the form the corpus
            # actually uses, and keep the rest as aliases so all of them block.
            name = primary if primary in matched else matched[0]
            found.append({
                "name": name,
                "aliases": "|".join(f for f in forms if f != name),
                "type": "character",
                "first_appearance_abs": abs_order,
            })
            break
    return found


def entities_from_units(units: list[dict]) -> list[dict]:
    """[{name, aliases, type, first_appearance_abs}] from the units' links.

    Everything linked is kept, tagged `character` or `term`. Terms are not
    noise to the post-guard: `first_appearance_abs` means "the first episode
    whose summary mentions this", so blocking a name below that point is right
    whatever the name refers to — under the gate there is no text supporting
    it, so the model can only have got it from its own memory of the show.
    The `character` tag is what the browsable character list filters on.
    """
    first: dict[str, int] = {}
    aliases: dict[str, set[str]] = {}
    for unit in sorted(units, key=lambda u: u["abs_order"]):
        for target, display in unit.get("links", []):
            name = clean_target(target)
            if not name:
                continue
            first.setdefault(name, unit["abs_order"])
            # An alias must look like a name: [[Walter White|Walt]] is useful,
            # [[Walter White|his father]] would blocklist an everyday phrase.
            if display and display != name and len(display) > 2 and display[0].isupper():
                aliases.setdefault(name, set()).add(display)
    return [
        {
            "name": name,
            # Only alias a person: "board" for "Board of directors" would block
            # an ordinary word every time it appeared in an answer.
            "aliases": "|".join(sorted(aliases.get(name, ()))) if is_person(name) else "",
            "type": "character" if is_character_name(name) else "term",
            "first_appearance_abs": abs_order,
        }
        for name, abs_order in sorted(first.items(), key=lambda kv: (kv[1], kv[0]))
    ]


def redate_by_text(entities: list[dict], units: list[tuple[int, str]]) -> list[dict]:
    """Pull each first appearance back to the first *plain-text* mention.

    Wikipedia links a term once, and not always at its first mention:
    "marijuana" is linked in episode 9 but written in episode 3. Dating from
    links alone therefore blocked a word that was already sitting in gated
    context, and the guard refused a legitimate answer.

    The post-guard's rule is "this name has no support below the gate", so the
    date has to be the first time the string appears at all, linked or not.
    """
    ordered = sorted(units)
    for entity in entities:
        names = [entity["name"], *(a for a in entity["aliases"].split("|") if a)]
        patterns = [name_pattern(n) for n in names]
        for abs_order, text in ordered:
            if abs_order >= entity["first_appearance_abs"]:
                break
            if any(p.search(text) for p in patterns):
                entity["first_appearance_abs"] = abs_order
                break
    return entities


def drop_episode_titles(entities: list[dict], titles: list[str]) -> list[dict]:
    """Episode titles are linked from summaries and look exactly like names.

    Breaking Bad's "One Minute" and "Gray Matter" entered the character list
    that way, and the eval caught them as characters nobody could ask about.
    """
    banned = {t.strip().lower() for t in titles if t}
    return [e for e in entities if e["name"].strip().lower() not in banned]


def merge_entities(*sources: list[dict]) -> list[dict]:
    """Combine entity lists, earliest appearance winning.

    The cast pass and the link pass overlap on recurring characters. Taking the
    earliest is the safe direction: a name blocked from earlier than strictly
    necessary costs a refusal, one blocked from later costs a spoiler.
    """
    entities = [dict(e) for source in sources for e in source]

    # The two sources name the same person differently — Wikipedia links the
    # article title "Gus Fring", TVMaze bills "Gustavo 'Gus' Fring" — so fold
    # anything whose name is another entity's alias into that entity.
    canonical: dict[str, str] = {}
    for entity in entities:
        for alias in entity["aliases"].split("|"):
            if alias:
                canonical.setdefault(alias, entity["name"])

    merged: dict[str, dict] = {}
    for entity in entities:
        key = canonical.get(entity["name"], entity["name"])
        current = merged.get(key)
        if current is None:
            merged[key] = {**entity, "name": key}
            continue
        current["first_appearance_abs"] = min(
            current["first_appearance_abs"], entity["first_appearance_abs"]
        )
        if entity["type"] == "character":
            current["type"] = "character"
        aliases = {a for a in (current["aliases"] + "|" + entity["aliases"]).split("|") if a}
        if entity["name"] != key:
            aliases.add(entity["name"])  # the folded name still has to be blocked
        current["aliases"] = "|".join(sorted(aliases))
    return sorted(merged.values(), key=lambda e: (e["first_appearance_abs"], e["name"]))

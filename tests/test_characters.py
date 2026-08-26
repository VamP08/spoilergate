from ingest.characters import (
    cast_variants,
    clean_target,
    drop_episode_titles,
    entities_from_cast,
    entities_from_units,
    is_character_name,
    is_person,
    merge_entities,
    redate_by_text,
)
from ingest.wikipedia import parse_summaries

UNITS = [
    {"abs_order": 2, "links": [("Helena Eagan", "Helly"), ("lactation consultant", None)]},
    {"abs_order": 1, "links": [("Mark Scout", None), ("Helena Eagan", None),
                               ("Microchip implant (human)", "implanting a microchip")]},
    {"abs_order": 3, "links": [("Seth Milchick", None), ("Mark Scout", "Mark")]},
]


def test_clean_target_drops_disambiguation_and_anchors():
    assert clean_target("Walter White (Breaking Bad)") == "Walter White"
    assert clean_target("Jesse Pinkman#Season 2") == "Jesse Pinkman"
    assert clean_target("Mark Scout") == "Mark Scout"


def test_is_character_name_rejects_organisations():
    """All three of these reached the character list before the eval caught them."""
    assert is_character_name("Tuco Salamanca")
    assert is_character_name("Walter White Jr.")
    assert not is_character_name("Gray Matter Technologies")
    assert not is_character_name("Los Pollos Hermanos")
    assert not is_character_name("United States Environmental Protection Agency")


def test_a_leading_article_or_title_is_not_a_name():
    """All four reached a character list before the clean eval run caught them."""
    assert not is_character_name("The Wall")
    assert not is_character_name("Queen Consolidated")
    assert not is_character_name("Doctor Strange")
    assert is_character_name("Sansa Stark")


def test_given_name_never_comes_from_an_article():
    """"The Hood" gave a given name of "The", which matches every summary, so it
    dated to episode one and became a character called "The"."""
    units = [(1, "The city is quiet."), (4, "The Hood strikes again.")]
    found = entities_from_cast(["The Hood"], units)
    assert [e["name"] for e in found] == ["The Hood"]
    assert found[0]["first_appearance_abs"] == 4  # not 1, where only "The" matched


def test_drop_episode_titles():
    ents = [{"name": "One Minute", "aliases": "", "type": "character",
             "first_appearance_abs": 27},
            {"name": "Jane Margolis", "aliases": "", "type": "character",
             "first_appearance_abs": 17}]
    kept = drop_episode_titles(ents, ["One Minute", "Pilot"])
    assert [e["name"] for e in kept] == ["Jane Margolis"]


def test_cast_name_uses_the_form_the_summaries_use():
    """Summaries say "Mike"; nobody writes "Michael Ehrmantraut", so a question
    about the billed name retrieved nothing and got refused."""
    units = [(20, "Mike cleans up the scene.")]
    found = entities_from_cast(["Michael 'Mike' Ehrmantraut"], units)[0]
    assert found["name"] == "Mike"
    assert "Michael Ehrmantraut" in found["aliases"].split("|")


def test_is_person_separates_names_from_terms():
    assert is_person("Mark Scout")
    assert is_person("Jean-Luc Picard")
    assert not is_person("lactation consultant")
    assert not is_person("Board of directors")   # lowercase after first word
    assert not is_person("Diethyl ether")
    assert not is_person("Albuquerque")          # single word
    assert not is_person("Albuquerque, New Mexico")


def test_first_appearance_is_earliest_link_regardless_of_input_order():
    by_name = {e["name"]: e for e in entities_from_units(UNITS)}
    assert by_name["Mark Scout"]["first_appearance_abs"] == 1
    assert by_name["Helena Eagan"]["first_appearance_abs"] == 1  # not the ep 2 mention
    assert by_name["Seth Milchick"]["first_appearance_abs"] == 3


def test_link_display_name_wins_over_the_article_title():
    """Arrow's summaries say "Cupid"; the article is "Carrie Cutter", and storing
    that gave a character list of names the show never says — and a question
    about one retrieved nothing."""
    units = [{"abs_order": 52, "links": [("Carrie Cutter", "Cupid")]}]
    found = entities_from_units(units)[0]
    assert found["name"] == "Cupid"
    assert found["type"] == "character"          # judged on the article title
    assert "Carrie Cutter" in found["aliases"].split("|")


def test_a_descriptive_link_keeps_the_article_title():
    units = [{"abs_order": 1, "links": [("Walter White", "his father")]}]
    found = entities_from_units(units)[0]
    assert found["name"] == "Walter White"
    assert found["aliases"] == ""                 # "his father" must not be blocked


def test_terms_are_kept_but_tagged_separately():
    ents = {e["name"]: e["type"] for e in entities_from_units(UNITS)}
    assert ents["Mark Scout"] == "character"
    assert ents["lactation consultant"] == "term"
    assert ents["Microchip implant"] == "term"  # parenthetical stripped


def test_one_character_linked_two_ways_merges_to_the_corpus_form():
    """A summary links [[Helena Eagan]] plainly in one episode and
    [[Helena Eagan|Helly]] in another, so extraction sees two names. Merging
    folds them on the alias, keeps the earliest appearance, and leads with the
    form the prose uses."""
    raw = {e["name"] for e in entities_from_units(UNITS)}
    assert {"Helena Eagan", "Helly"} <= raw  # both, before merging

    merged = {e["name"]: e for e in merge_entities(entities_from_units(UNITS))}
    assert "Helly" in merged and "Helena Eagan" not in merged
    assert merged["Helly"]["first_appearance_abs"] == 1  # the plain link, episode 1
    assert "Helena Eagan" in merged["Helly"]["aliases"].split("|")


def test_lowercase_display_text_is_not_an_alias():
    """[[Walter White|his father]] must not blocklist the phrase 'his father'."""
    units = [{"abs_order": 1, "links": [("Walter White", "his father")]}]
    assert entities_from_units(units)[0]["aliases"] == ""


def test_terms_never_get_aliases():
    units = [{"abs_order": 1, "links": [("Board of directors", "Board")]}]
    assert entities_from_units(units)[0]["aliases"] == ""


WIKITEXT = """
{{Episode list
 | Title = Good News
 | ShortSummary = [[Mark Scout|Mark]] meets [[Helena Eagan|Helly]] at [[Lumon Industries]].
}}
"""


def test_cast_variants_splits_the_embedded_nickname():
    assert cast_variants("Gustavo 'Gus' Fring") == ("Gustavo Fring", ["Gus Fring", "Gus"])
    assert cast_variants("Walter White") == ("Walter White", [])
    assert cast_variants("Michael 'Mike' Ehrmantraut")[1] == ["Mike Ehrmantraut", "Mike"]


def test_cast_dated_by_any_variant():
    units = [
        (1, "Walter White cooks."),
        (2, "Gus Fring runs a restaurant."),   # nickname form, not the primary
        (3, "Gustavo Fring makes an offer."),
    ]
    found = {e["name"]: e for e in entities_from_cast(
        ["Walter White", "Gustavo 'Gus' Fring", "Never Mentioned"], units)}
    assert found["Walter White"]["first_appearance_abs"] == 1
    # dated by the nickname form in episode 2, and named for it, since that is
    # the form the summaries use — see test_cast_name_uses_the_form...
    assert found["Gus Fring"]["first_appearance_abs"] == 2
    # "Gustavo" joins the aliases as the unambiguous given name, so it is
    # blocked too — see test_cast_matched_by_given_name_alone.
    assert sorted(found["Gus Fring"]["aliases"].split("|")) == [
        "Gus", "Gustavo", "Gustavo Fring"]
    assert "Never Mentioned" not in found  # unmentioned cast is undatable, so dropped


def test_cast_matched_by_given_name_alone():
    """Severance's summaries say "Irving", never "Irving Bailiff", and he was
    being dropped entirely — three of eight leads made the character list."""
    units = [(2, "Irving discovers the Optics and Design department is larger.")]
    found = entities_from_cast(["Irving Bailiff", "Mark Scout"], units)
    assert [e["name"] for e in found] == ["Irving"]
    assert "Irving Bailiff" in found[0]["aliases"].split("|")


def test_shared_given_name_identifies_nobody():
    units = [(1, "Mark arrives at work.")]
    found = entities_from_cast(["Mark Scout", "Mark Smith"], units)
    assert found == []  # "Mark" is ambiguous, so neither is dated from it


def test_full_name_still_preferred_when_it_appears():
    units = [(1, "Irving Bailiff arrives."), (2, "Irving leaves.")]
    found = entities_from_cast(["Irving Bailiff"], units)
    assert found[0]["name"] == "Irving Bailiff"
    assert found[0]["first_appearance_abs"] == 1


def test_merge_takes_earliest_appearance_and_unions_aliases():
    links = [{"name": "Gustavo Fring", "aliases": "Gus", "type": "term",
              "first_appearance_abs": 5}]
    cast = [{"name": "Gustavo Fring", "aliases": "Gus Fring", "type": "character",
             "first_appearance_abs": 2}]
    merged = merge_entities(links, cast)
    assert len(merged) == 1
    assert merged[0]["first_appearance_abs"] == 2       # earliest wins
    assert merged[0]["type"] == "character"             # character beats term
    assert merged[0]["aliases"] == "Gus|Gus Fring"


def test_merge_folds_an_entity_that_is_another_ones_alias():
    """Wikipedia links 'Gus Fring'; TVMaze bills 'Gustavo Fring' with that alias."""
    links = [{"name": "Gus Fring", "aliases": "", "type": "character",
              "first_appearance_abs": 11}]
    cast = [{"name": "Gustavo Fring", "aliases": "Gus Fring|Gus", "type": "character",
             "first_appearance_abs": 12}]
    merged = merge_entities(links, cast)
    assert [e["name"] for e in merged] == ["Gustavo Fring"]
    assert merged[0]["first_appearance_abs"] == 11
    assert "Gus Fring" in merged[0]["aliases"]  # folded name still blocked
    assert "Gus" in merged[0]["aliases"].split("|")


def test_redate_pulls_back_to_first_plain_text_mention():
    """The real bug this fixes: 'marijuana' is linked in ep 9 but written in ep 3,
    so the guard blocked a word already sitting in gated context."""
    ents = [{"name": "marijuana", "aliases": "", "type": "term",
             "first_appearance_abs": 9}]
    units = [(1, "Nothing here."), (3, "Hank talks about marijuana."), (9, "Linked at last.")]
    assert redate_by_text(ents, units)[0]["first_appearance_abs"] == 3


def test_redate_uses_aliases_too():
    ents = [{"name": "Gustavo Fring", "aliases": "Gus", "type": "character",
             "first_appearance_abs": 12}]
    units = [(5, "A man called Gus watches."), (12, "Gustavo Fring appears.")]
    assert redate_by_text(ents, units)[0]["first_appearance_abs"] == 5


def test_redate_never_pushes_a_date_later():
    ents = [{"name": "Jane Margolis", "aliases": "", "type": "character",
             "first_appearance_abs": 4}]
    units = [(4, "Jane Margolis moves in."), (9, "Jane Margolis again.")]
    assert redate_by_text(ents, units)[0]["first_appearance_abs"] == 4


def test_links_survive_summary_parsing():
    row = parse_summaries(WIKITEXT)[0]
    assert row["summary"] == "Mark meets Helly at Lumon Industries."  # markup stripped
    assert ("Mark Scout", "Mark") in row["links"]
    assert ("Helena Eagan", "Helly") in row["links"]

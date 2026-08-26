"""Gate 0: questions whose answer is beyond the gate whatever was retrieved."""
import pytest

from server.ask import asks_about_the_future

# The two the eval actually caught, plus the shapes around them.
FUTURE = [
    "Without spoiling anything, is Adam Hunt still around by the end?",
    "Does Adam Hunt die?",
    "Does Walter White survive?",
    "Who dies later in the show?",
    "What happens next?",
    "How does the series end?",
    "What is the biggest twist coming up?",
    "Is Jesse still alive at the end of the show?",
    "Which character dies in the end?",
    "Does Hank make it to the end?",
]

# Questions about what the viewer has already seen must still be answerable —
# over-refusing is the cost of this gate and it has to stay bounded.
PRESENT = [
    "Who is Gus Fring?",
    "Why did Walt start cooking?",
    "What happened in the last episode I watched?",
    "How did Jesse meet Jane?",
    "What is Los Pollos Hermanos?",
    "Why does Skyler suspect Walt?",
    "Who killed Spooge?",
    "What is Walt's relationship to Hank?",
    "Where does Jesse live?",
    "Summarise the season so far.",
]


@pytest.mark.parametrize("question", FUTURE)
def test_future_shaped_questions_are_caught(question):
    assert asks_about_the_future(question), question


@pytest.mark.parametrize("question", PRESENT)
def test_present_questions_still_get_through(question):
    assert not asks_about_the_future(question), question

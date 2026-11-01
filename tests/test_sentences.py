"""Sentence chunking for streamed speech, with the gateway's rule.

Ported from the gateway's own splitter tests, so both sides cut a streamed
answer at the same places.
"""

from pathlib import Path

from owui_protocol.sentences import SentenceSplitter

PATH = Path(__file__).parents[1] / "custom_components/openwebui_agent/sentences.py"


def feed_all(text: str, **kw) -> list[str]:
    """Stream ``text`` in small fragments and collect every sentence."""
    splitter = SentenceSplitter(**kw)
    out: list[str] = []
    for i in range(0, len(text), 3):
        out += splitter.feed(text[i : i + 3])
    return out + splitter.flush()


def test_module_has_no_homeassistant_import():
    """The rule is pure: no Home Assistant imports."""
    source = PATH.read_text(encoding="utf-8")
    assert "homeassistant" not in source.replace("Home Assistant", "")


def test_splits_after_terminators_and_never_inside_numbers_or_times():
    """. ! ? ; … end a sentence; 3.5 and 7:30 do not."""
    assert feed_all("It is sunny. Twenty degrees. ") == [
        "It is sunny.",
        "Twenty degrees.",
    ]
    assert feed_all("Set it to 3.5 degrees at 7:30. Then rest!") == [
        "Set it to 3.5 degrees at 7:30.",
        "Then rest!",
    ]
    assert feed_all("Is it on? Yes; it is… fine") == [
        "Is it on?",
        "Yes;",
        "it is…",
        "fine",
    ]
    assert feed_all("Είναι ανοιχτό; Ναι.") == ["Είναι ανοιχτό;", "Ναι."]
    # U+037E, the Greek question mark, ends a sentence too.
    assert feed_all("Είναι ανοιχτό\u037e Ναι.") == ["Είναι ανοιχτό\u037e", "Ναι."]


def test_keeps_abbreviations_and_initials_together():
    """e.g., Dr. and a single initial do not end a sentence."""
    assert feed_all("Ask Dr. Smith, e.g. tomorrow. Done.") == [
        "Ask Dr. Smith, e.g. tomorrow.",
        "Done.",
    ]
    assert feed_all("J. Smith called. Later.") == ["J. Smith called.", "Later."]


def test_first_chunk_may_end_at_a_comma_after_forty_chars():
    """Only the first chunk, and only once it has 40 characters."""
    text = "The kitchen light and the hall light are on, and the heating is off. Anything else?"
    out = feed_all(text)
    assert out[0] == "The kitchen light and the hall light are on,"
    assert out[-1] == "Anything else?"
    assert feed_all("Yes, it is on. Really.")[0] == "Yes, it is on."
    splitter = SentenceSplitter()
    splitter.feed("Done. ")
    assert splitter.feed(
        "Now, the second sentence is long enough to pass forty, but not first. "
    ) == ["Now, the second sentence is long enough to pass forty, but not first."]


def test_forces_a_split_past_max_chars_and_splits_at_paragraphs():
    """Past 220 (here 100) characters at a space; a blank line is a break."""
    long = "word " * 60
    out = feed_all(long.strip(), max_chars=100)
    assert all(len(x) <= 100 for x in out) and " ".join(out) == long.strip()
    assert feed_all("First line\n\nSecond line") == ["First line", "Second line"]
    assert feed_all("a line\nstill the same") == ["a line\nstill the same"]
    splitter = SentenceSplitter()
    assert splitter.feed("") == [] and splitter.flush() == []
    assert splitter.feed("   ") == [] and splitter.flush() == []


def test_gateway_deltas_split_at_once():
    """A delta ends with a space, so its sentence is complete when it arrives."""
    splitter = SentenceSplitter()
    assert splitter.feed("It is sunny. ") == ["It is sunny."]
    assert splitter.feed("Twenty degrees. ") == ["Twenty degrees."]
    assert splitter.flush() == []

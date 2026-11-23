from pathlib import Path

import pytest

from hostops.models import ActionType, Category, Severity
from hostops.retrieval import BM25, RunbookIndex, parse_runbook, tokenize


@pytest.fixture(scope="module")
def index() -> RunbookIndex:
    return RunbookIndex.load()


def test_every_category_has_a_runbook(index: RunbookIndex) -> None:
    assert {rb.category for rb in index.runbooks} == set(Category)


def test_front_matter_is_parsed(index: RunbookIndex) -> None:
    access = index.by_category(Category.ACCESS)
    assert access.severity is Severity.HIGH
    assert access.default_actions == [ActionType.REPLY_GUEST, ActionType.ESCALATE_HOST]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("the wifi keeps dropping and the internet is slow", Category.WIFI),
        ("I'm locked out, the door code isn't working", Category.ACCESS),
        ("aircon is blowing warm air", Category.APPLIANCE),
        ("calendar sync failing ical 503", Category.SYSTEM_CALENDAR_SYNC),
        ("can we check in early, our flight lands at 9am", Category.CHECKIN),
    ],
)
def test_search_ranks_the_right_runbook_first(index: RunbookIndex, query: str, expected: Category) -> None:
    hits = index.search(query)
    assert hits, query
    assert hits[0].runbook.category is expected


def test_empty_or_unknown_query_returns_nothing(index: RunbookIndex) -> None:
    assert index.search("") == []
    assert index.search("zzqx") == []


def test_tokenize_lowercases_and_drops_stopwords() -> None:
    assert tokenize("The WiFi is NOT working!") == ["wifi", "not", "working"]


def test_bm25_prefers_rarer_term_matches() -> None:
    bm25 = BM25([["wifi", "router"], ["door", "lock"], ["door", "wifi"]])
    scores = bm25.scores(["lock"])
    assert scores.index(max(scores)) == 1


def test_parse_rejects_missing_front_matter(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_text("# no front matter\n")
    with pytest.raises(ValueError, match="front matter"):
        parse_runbook(bad)

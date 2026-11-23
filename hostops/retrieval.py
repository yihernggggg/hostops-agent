"""BM25 search over runbooks. Hand-written (Okapi BM25, k1=1.5, b=0.75) so it can be explained line by line.

Why BM25 and not embeddings: the corpus is ~a dozen short, keyword-heavy docs. BM25 is deterministic,
needs no model download or network, and is easy to debug ("which term matched?"). The tradeoff is no
synonym understanding, which the runbook `keywords:` front matter partly compensates for.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from hostops.models import ActionType, Category, Severity

RUNBOOK_DIR = Path(__file__).parent / "runbooks"

_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have i i'm im in is it it's its me my of on or our so "
    "that the their there this to us was we were what when with you your".split()
)
_TOKEN = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tfs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.avg_len = sum(self.lengths) / len(docs) if docs else 0.0
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        # The "+1" variant keeps idf positive even for terms in most documents.
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, length in zip(self.tfs, self.lengths, strict=True):
            score = 0.0
            for term in query:
                f = tf.get(term, 0)
                if f:
                    norm = f + self.k1 * (1 - self.b + self.b * length / self.avg_len)
                    score += self.idf[term] * f * (self.k1 + 1) / norm
            out.append(score)
        return out


@dataclass(frozen=True)
class Runbook:
    id: str
    title: str
    category: Category
    severity: Severity
    default_actions: list[ActionType]
    keywords: list[str]
    body: str


@dataclass(frozen=True)
class SearchHit:
    runbook: Runbook
    score: float


def parse_runbook(path: Path) -> Runbook:
    """Front matter is a tiny `key: value` block between `---` lines. No YAML dependency needed."""
    text = path.read_text()
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError(f"{path.name}: missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"{path.name}: bad front matter line {line!r}")
        meta[key.strip()] = value.strip()

    def as_list(key: str) -> list[str]:
        return [v.strip() for v in meta.get(key, "").split(",") if v.strip()]

    try:
        return Runbook(
            id=path.stem,
            title=meta["title"],
            category=Category(meta["category"]),
            severity=Severity(meta.get("severity", "medium")),
            default_actions=[ActionType(a) for a in as_list("default_actions")],
            keywords=as_list("keywords"),
            body=match.group(2).strip(),
        )
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{path.name}: invalid front matter ({exc})") from exc


class RunbookIndex:
    # Keywords and title are repeated so they weigh more than incidental words in the body.
    KEYWORD_WEIGHT = 3

    def __init__(self, runbooks: list[Runbook]) -> None:
        self.runbooks = runbooks
        docs = [
            tokenize(" ".join([rb.title, *rb.keywords] * self.KEYWORD_WEIGHT) + " " + rb.body)
            for rb in runbooks
        ]
        self._bm25 = BM25(docs)

    @classmethod
    def load(cls, directory: Path = RUNBOOK_DIR) -> RunbookIndex:
        return cls([parse_runbook(p) for p in sorted(directory.glob("*.md"))])

    def by_category(self, category: Category) -> Runbook:
        for rb in self.runbooks:
            if rb.category is category:
                return rb
        raise KeyError(category)

    def search(self, query: str, k: int = 3) -> list[SearchHit]:
        terms = tokenize(query)
        if not terms:
            return []
        scored = sorted(
            (SearchHit(rb, s) for rb, s in zip(self.runbooks, self._bm25.scores(terms), strict=True)),
            key=lambda h: h.score,
            reverse=True,
        )
        return [h for h in scored[:k] if h.score > 0]

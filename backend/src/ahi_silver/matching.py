"""Suggest a Silver column for every bronze column by independent votes.

Every method looks at every column, whatever the others said:

* saved    -- the mapping a reviewer approved earlier (this table's, or the same name elsewhere)
* exact    -- the same words as a Silver or DRT name
* fuzzy    -- a close spelling (rapidfuzz)
* semantic -- a close meaning (word2vec cosine)
* ai       -- an LLM's reading of the name (Azure OpenAI or Gemini), one call per table;
              it may also say "nothing fits"

Each method casts at most one vote per column (the AI may add a second choice, listed but
not counted). Votes for the same Silver column are pooled into a *candidate*; candidates
are ranked -- this table's approved mapping first, then by how many methods agree, then
by the strongest vote -- and the best one is pre-selected as the recommendation. The
reviewer sees every candidate with the methods behind it and can pick any of them, or
any other Silver column, or Ignore.

Nothing here decides. A person approves the whole mapping, and only then is it saved.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable

from . import normalize
from .catalog import MAPPED, SilverColumn

try:
    from rapidfuzz import fuzz, process
except ImportError:  # optional: the fuzzy method is skipped without it
    fuzz = process = None

# Voting methods, in the order the UI lists them.
SAVED = "saved"
EXACT = "exact"
FUZZY = "fuzzy"
SEMANTIC = "semantic"
AI = "ai"
METHODS = (SAVED, EXACT, FUZZY, SEMANTIC, AI)

# How the current choice was made.
RECOMMENDED = "recommended"  # pre-selected from the votes
MANUAL = "manual"  # set by the reviewer
NONE = "none"  # nothing chosen yet

# How far each method's score is trusted when two candidates have as many votes.
WEIGHT = {SAVED: 1.0, EXACT: 1.0, AI: 0.9, SEMANTIC: 0.85, FUZZY: 0.8}


@dataclass
class Vote:
    method: str
    # None: "no Silver column fits" (the AI).
    silver_column: str | None
    score: float
    reason: str = ""
    # This table's own approved mapping: a reviewer's decision, ranked above any count.
    own: bool = False
    # The AI's second choice: shown to the reviewer, not counted as a vote.
    second_choice: bool = False


@dataclass
class Candidate:
    silver_column: str | None  # None: do not load the column (Ignore)
    votes: list[Vote]
    recommended: bool = False

    @property
    def counted(self) -> list[Vote]:
        return [vote for vote in self.votes if not vote.second_choice]

    @property
    def methods(self) -> list[str]:
        found = {vote.method for vote in self.counted}
        return [method for method in METHODS if method in found]

    @property
    def support(self) -> int:
        return len(self.methods)

    @property
    def strength(self) -> float:
        return max((vote.score * WEIGHT.get(vote.method, 0.5) for vote in self.counted), default=0.0)

    @property
    def own(self) -> bool:
        return any(vote.own for vote in self.counted)

    def rank(self) -> tuple:
        return (self.own, self.support, round(self.strength, 6))


@dataclass
class Suggestion:
    bronze_column: str
    silver_column: str | None = None
    # The reviewer's explicit "do not load this column" decision (never saved).
    ignored: bool = False
    selection: str = NONE
    reason: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    # More Silver columns the same bronze column also loads into (the DRT mapping maps
    # one source column to two, e.g. one "EffectiveDate" to the policy and accounting dates).
    also: list[str] = field(default_factory=list)

    @property
    def decided(self) -> bool:
        return self.silver_column is not None or self.ignored

    @property
    def targets(self) -> list[str]:
        """Every Silver column this bronze column loads into."""
        return [] if self.ignored or not self.silver_column else [self.silver_column, *self.also]

    @property
    def split(self) -> bool:
        """Methods disagree: votes went to more than one candidate (or to "nothing fits").
        A second target the column also loads into is not a disagreement."""
        return sum(1 for candidate in self.candidates
                   if candidate.support and candidate.silver_column not in self.also) > 1

    @property
    def recommended(self) -> Candidate | None:
        return next((candidate for candidate in self.candidates if candidate.recommended), None)

    def backers(self) -> list[Vote]:
        """The counted votes for the current choice."""
        target = None if self.ignored else self.silver_column
        if target is None and not self.ignored:
            return []
        return next((c.counted for c in self.candidates if c.silver_column == target), [])


# The AI (or a test stub): (columns with samples, catalog) ->
#   {bronze: (silver | None, confidence, reason[, second choice | None])}
LlmMatcher = Callable[[dict[str, list[str]], list[SilverColumn]], dict[str, tuple]]
# word2vec: (words, words) -> cosine or None
Similarity = Callable[[tuple[str, ...], tuple[str, ...]], float | None]


# Words that say what *kind* of value a column holds, not what it is about.
GENERIC = {"name", "number", "date", "code", "amount", "id", "of", "the", "value", "total"}


def _shares_word(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """At least one distinctive word in common, allowing a typo (premum ~ premium)."""
    a = [word for word in left if word not in GENERIC]
    b = [word for word in right if word not in GENERIC]
    # The word splitter can break a typo apart ("premum" -> "pre mum"); keep it whole too.
    a.append("".join(a))
    return any(x == y or (len(x) > 3 and fuzz.ratio(x, y) >= 85) for x in a if x for y in b)


# What kind of value a name says it holds. Identifiers form one family (a branch code is
# a profit center number), names another; a carrier code is never an insurance company name.
KINDS = {
    "name": "name",
    "code": "identifier", "number": "identifier", "id": "identifier", "key": "identifier",
    "date": "date", "time": "date", "timestamp": "date",
    "amount": "amount", "total": "amount", "value": "amount",
}


def _kind(words) -> str | None:
    """The kind the name ends on, e.g. "carrier code" -> identifier; None when it states none."""
    for word in reversed(tuple(words)):
        if word in KINDS:
            return KINDS[word]
    return None


def _kinds_agree(source: tuple[str, ...], target: SilverColumn) -> bool:
    mine = _kind(source)
    theirs = _kind(normalize.words(target.name)) or _kind(normalize.words(target.drt_name))
    return mine is None or theirs is None or mine == theirs


def _has_abbreviation(words: tuple[str, ...]) -> bool:
    """A short token left after the known abbreviations were expanded (acc, nm, incp...)."""
    return any(len(word) <= 3 and word not in GENERIC and not word.isdigit() for word in words)


def synonyms(column: SilverColumn) -> list[str]:
    """Synonyms listed in the description's parentheses: "(carrier, insurer, ...)"."""
    found = re.findall(r"\(([^)]*)\)", column.description)
    return [part.strip() for group in found for part in group.split(",") if part.strip()]


# --- the voters -------------------------------------------------------------------


def _targets(value) -> list[str]:
    """A saved decision as a list: [silver] or [silver, silver] (1:N). Ignores are not saved."""
    if isinstance(value, (list, tuple)):
        return [target for target in value if target]
    return [value] if value else []


def _saved_votes(columns, saved, known, by_name, votes, stale) -> None:
    for column in columns:
        if _targets(saved.get(column)):
            gone = [t for t in _targets(saved[column]) if t not in by_name]
            live = [t for t in _targets(saved[column]) if t in by_name]
            if gone:
                # Approved earlier, but that Silver column is no longer in the list.
                stale[column] = f"Was mapped to {', '.join(gone)}, which is no longer a Silver column."
            for target in live:
                votes[column].append(Vote(SAVED, target, 1.0, "Approved earlier for this profit center", own=True))
        elif known.get(normalize.compact(column)) in by_name:
            votes[column].append(Vote(SAVED, known[normalize.compact(column)], 0.95,
                                      "Same column approved for another table"))


def _exact_votes(columns, catalog, votes) -> None:
    exact = {}
    for silver in catalog:
        exact.setdefault(normalize.compact(silver.name), silver.name)
        if silver.drt_name:
            exact.setdefault(normalize.compact(silver.drt_name), silver.name)
    for column in columns:
        target = exact.get(normalize.compact(column))
        if target:
            votes[column].append(Vote(EXACT, target, 1.0, "Same name"))


def _fuzzy_votes(columns, catalog, votes, minimum, margin) -> None:
    for column in columns:
        source = normalize.words(column)
        choices = {}
        for silver in catalog:
            # "policy id" spelled like "policy effective date" is still not a date.
            if not _kinds_agree(source, silver):
                continue
            choices[f"{silver.name}\x00n"] = normalize.phrase(silver.name)
            if silver.drt_name:
                choices[f"{silver.name}\x00d"] = normalize.phrase(silver.drt_name)
        if not choices:
            continue
        # Best score per Silver column (its own name or its DRT name), counting only
        # candidates that share a distinctive word -- "carrier name" and "profit center
        # name" have nothing in common but "name".
        per_target: dict[str, float] = {}
        for text, score, key in process.extract(normalize.phrase(column), choices, scorer=fuzz.WRatio, limit=None):
            if not _shares_word(source, tuple(text.split())):
                continue
            target = key.split("\x00")[0]
            per_target[target] = max(per_target.get(target, 0.0), score)
        if not per_target:
            continue
        ranked = sorted(per_target.items(), key=lambda item: item[1], reverse=True)
        target, score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
        # A shared generic word ("name", "date") scores two targets alike: a tie is no vote.
        if score >= minimum and score - runner_up >= margin:
            votes[column].append(Vote(FUZZY, target, round(score / 100, 3), f"Similar spelling ({score:.0f}%)"))


def _semantic_votes(columns, catalog, votes, similarity, minimum, margin) -> None:
    for column in columns:
        source = normalize.words(column)
        if _has_abbreviation(source):
            # word2vec reads "acc" as a word (the sports conference), not as
            # "accounting": abbreviations are for the AI to read.
            continue
        scored = []
        for silver in catalog:
            if not _kinds_agree(source, silver):
                continue
            phrases = [silver.name, silver.drt_name, *synonyms(silver)]
            scores = [similarity(source, normalize.words(text)) for text in phrases if text]
            scores = [score for score in scores if score is not None]
            if scores:
                scored.append((max(scores), silver.name))
        if not scored:
            continue
        scored.sort(reverse=True)
        score, target = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else 0.0
        # As with fuzzy: two targets about as close is a question for a person.
        if score >= minimum and score - runner_up >= margin:
            votes[column].append(Vote(SEMANTIC, target, round(score, 3), f"Similar meaning ({score:.2f})"))


def _ai_votes(columns, catalog, by_name, votes, samples, llm, notes) -> None:
    try:
        answers = llm({column: list(samples.get(column, []))[:3] for column in columns}, catalog)
    except Exception as error:  # noqa: BLE001 - an AI outage must not stop the review
        notes.append(f"AI unavailable: {str(error)[:200]}")
        return
    for column in columns:
        answer = answers.get(column)
        if not answer:
            continue
        target, confidence, reason = answer[:3]
        alternative = answer[3] if len(answer) > 3 else None
        confidence = round(max(0.0, min(1.0, float(confidence))), 3)
        if target is None:
            votes[column].append(Vote(AI, None, confidence, reason or "No Silver column fits"))
        elif target in by_name:
            votes[column].append(Vote(AI, target, confidence, reason or "Suggested by the AI"))
        # A name that is not in the list is dropped: the AI cannot invent a column.
        if alternative in by_name and alternative != target:
            votes[column].append(Vote(AI, alternative, 0.0, "The AI's second choice", second_choice=True))


# --- pooling and recommending -----------------------------------------------------


def _candidates(votes: list[Vote]) -> list[Candidate]:
    """Votes pooled by Silver column, best first (a real column before "nothing fits" on a tie)."""
    pooled: dict[str | None, Candidate] = {}
    for vote in votes:
        pooled.setdefault(vote.silver_column, Candidate(vote.silver_column, [])).votes.append(vote)
    by_name = sorted(pooled.values(), key=lambda c: c.silver_column or "")
    return sorted(by_name, key=lambda c: (c.rank(), c.silver_column is not None), reverse=True)


LABELS = {SAVED: "Saved", EXACT: "Exact", FUZZY: "Fuzzy", SEMANTIC: "Semantic", AI: "AI"}


def _label(candidate: Candidate) -> str:
    target = candidate.silver_column or "no column"
    return f"{target} ({' + '.join(LABELS[method] for method in candidate.methods)})"


def suggest(
    columns: list[str],
    catalog: list[SilverColumn],
    saved: dict[str, str | None],
    known: dict[str, str],
    samples: dict[str, list[str]] | None = None,
    similarity: Similarity | None = None,
    llm: LlmMatcher | None = None,
    fuzzy_min: float = 85,
    semantic_min: float = 0.72,
    fuzzy_margin: float = 5,
    semantic_margin: float = 0.05,
    one_to_many: set[str] | None = None,
) -> tuple[list[Suggestion], list[str]]:
    """One suggestion per bronze column, with every method's vote, plus notes on methods
    that could not run.

    ``saved``: this profit center's approved mapping (column -> silver, or a list of silver
    columns when one column feeds several).
    ``known``: approved mappings elsewhere, by normalized column name.
    ``one_to_many``: columns whose saved targets all load (one header mapped twice);
    for the others several saved targets are competing votes. None: every list loads.
    Only the catalog's mapped columns are targets; system columns are never offered.
    """
    catalog = [column for column in catalog if column.role == MAPPED]
    samples = samples or {}
    notes: list[str] = []
    by_name = {column.name: column for column in catalog}
    votes: dict[str, list[Vote]] = {column: [] for column in columns}
    stale: dict[str, str] = {}

    _saved_votes(columns, saved, known, by_name, votes, stale)
    _exact_votes(columns, catalog, votes)
    if process is None:
        notes.append("Fuzzy matching unavailable (install rapidfuzz).")
    else:
        _fuzzy_votes(columns, catalog, votes, fuzzy_min, fuzzy_margin)
    if similarity is None:
        notes.append("Semantic matching skipped: no word2vec vectors configured (AHI_WORD2VEC_PATH).")
    else:
        _semantic_votes(columns, catalog, votes, similarity, semantic_min, semantic_margin)
    if llm is None:
        notes.append("AI matching skipped: not configured (AZURE_OPENAI_* or GEMINI_API_KEY, AHI_AI_ENABLED).")
    elif columns:
        _ai_votes(columns, catalog, by_name, votes, samples, llm, notes)

    candidates = {column: _candidates(votes[column]) for column in columns}

    # Recommend: strongest candidates first across the table, so when two columns want
    # the same Silver column the better-supported one gets it and the other falls back
    # to its next candidate. "Nothing fits" stops a column's fallback: it is left for
    # the reviewer, who decides whether to Ignore it (nothing pre-selects Ignore).
    pairs = [(c.rank(), column, c) for column in columns for c in candidates[column] if c.support]
    pairs.sort(key=lambda p: (p[0], p[2].silver_column is not None), reverse=True)
    chosen: dict[str, Candidate] = {}
    stopped: dict[str, Candidate] = {}
    blocked: dict[str, tuple[str, str]] = {}
    taken: dict[str, str] = {}
    for _, column, candidate in pairs:
        if column in chosen or column in stopped:
            continue
        if candidate.silver_column is None:
            stopped[column] = candidate
            continue
        if candidate.silver_column in taken:
            blocked.setdefault(column, (candidate.silver_column, taken[candidate.silver_column]))
            continue
        taken[candidate.silver_column] = column
        chosen[column] = candidate

    # A saved one-to-many decision: the other saved targets come along as "also",
    # unless another column holds them.
    also: dict[str, list[str]] = {}
    for column, candidate in chosen.items():
        if not candidate.own or candidate.silver_column is None:
            continue
        if one_to_many is not None and column not in one_to_many:
            continue
        for target in _targets(saved.get(column)):
            if target and target != candidate.silver_column and target in by_name and target not in taken:
                taken[target] = column
                also.setdefault(column, []).append(target)

    result = []
    for column in columns:
        suggestion = Suggestion(column, candidates=candidates[column], samples=list(samples.get(column, []))[:3])
        candidate = chosen.get(column)
        others = [_label(c) for c in candidates[column] if c.support and c is not candidate]
        if candidate is not None:
            candidate.recommended = True
            suggestion.silver_column = candidate.silver_column
            suggestion.also = also.get(column, [])
            suggestion.selection = RECOMMENDED
            why = f"Recommended by {' + '.join(LABELS[m] for m in candidate.methods)}."
            if suggestion.also:
                why += f" Also loads into {', '.join(suggestion.also)}, as approved earlier."
        elif column in stopped:
            why = f"AI: {stopped[column].counted[0].reason.rstrip('.')}. Choose a column or Ignore."
        elif column in blocked:
            target, owner = blocked[column]
            why = f"{target} is recommended for {owner}. Choose another column or Ignore."
        else:
            why = "No method found a match. Choose a column or Ignore."
        if others and candidate is not None:
            why += f" Other votes: {', '.join(others)}."
        suggestion.reason = " ".join(filter(None, [stale.get(column), why]))
        result.append(suggestion)
    return result, notes


def choose(suggestion: Suggestion, silver_column: str | None, ignored: bool) -> None:
    """Apply the reviewer's choice. Picking the recommendation again restores it."""
    suggestion.silver_column = None if ignored else silver_column
    suggestion.ignored = ignored
    # The main column is no longer an extra one; an ignored column loads nowhere.
    suggestion.also = [] if ignored or not silver_column else [t for t in suggestion.also if t != silver_column]
    recommended = suggestion.recommended
    if recommended is not None and recommended.silver_column == suggestion.silver_column and \
            (recommended.silver_column is not None or ignored):
        suggestion.selection = RECOMMENDED
    else:
        suggestion.selection = MANUAL


def set_also(suggestion: Suggestion, targets: list[str]) -> None:
    """The reviewer's extra targets for a column (its main target and repeats dropped)."""
    if suggestion.ignored or not suggestion.silver_column:
        suggestion.also = []
        return
    suggestion.also = [t for t in dict.fromkeys(targets) if t and t != suggestion.silver_column]
    suggestion.selection = MANUAL


def problems(table: str, suggestions: list[Suggestion]) -> list[str]:
    """Why this table's mapping cannot be approved yet."""
    issues = []
    for suggestion in suggestions:
        if not suggestion.decided:
            issues.append(f"{table}.{suggestion.bronze_column}: choose a Silver column or Ignore.")
    targets: dict[str, list[str]] = {}
    for suggestion in suggestions:
        for target in suggestion.targets:
            targets.setdefault(target, []).append(suggestion.bronze_column)
    for target, sources in targets.items():
        if len(sources) > 1:
            issues.append(f"{table}: {', '.join(sources)} all map to {target}; keep one.")
    return issues


# --- storing a review ---------------------------------------------------------------


def suggestion_to_dict(suggestion: Suggestion) -> dict:
    """Everything about one column's suggestion, as JSON-ready values."""
    data = asdict(suggestion)
    for candidate in data["candidates"]:
        for vote in candidate["votes"]:
            vote["score"] = float(vote["score"])  # a matcher may hand back a numpy scalar
    return data


def suggestion_from_dict(data: dict) -> Suggestion:
    candidates = [
        Candidate(c.get("silver_column"), [Vote(**vote) for vote in c.get("votes", [])], bool(c.get("recommended")))
        for c in data.get("candidates", [])
    ]
    return Suggestion(
        bronze_column=data["bronze_column"], silver_column=data.get("silver_column"),
        ignored=bool(data.get("ignored")), selection=data.get("selection", NONE), reason=data.get("reason", ""),
        candidates=candidates, samples=list(data.get("samples", [])), also=list(data.get("also", [])),
    )

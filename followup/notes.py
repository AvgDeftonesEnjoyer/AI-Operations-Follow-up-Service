"""Small English phrase rules for offline classification and safe routing."""

import re


MATERIALS = re.compile(
    r"\b(backorder(?:ed)?|awaiting materials|waiting for materials|"
    r"materials? shortage|materials? delay(?:ed)?)\b"
)
UPSTREAM = re.compile(
    r"\b(awaiting|waiting (?:for|on)|blocked by|dependent on|"
    r"cannot proceed until|unable to proceed until)\b"
)
NEGATION = re.compile(r"\b(no|not|never)\b")
RESOLUTION = re.compile(r"\b(resolved|delivered|cleared|completed|received|arrived)\b")
CONDITION = re.compile(r"\b(if|unless|in case)\b")
# A polite request (could you/we...) is not a hypothetical blocker.
HYPOTHETICAL = re.compile(r"\b(?:might|may|could)\s+(?!(?:you|we)\b)|\bhypothetical\b")


def is_active(blocker: str, qualifiers: list[str]) -> bool:
    """Check a blocker clause together with the clauses that qualify it."""
    if NEGATION.search(blocker) or RESOLUTION.search(blocker):
        return False
    if CONDITION.search(blocker) or HYPOTHETICAL.search(blocker):
        return False
    for text in qualifiers:
        # "..., if the delivery slips" makes the blocker conditional;
        # "..., call me if anything changes" does not.
        if CONDITION.match(text.strip()):
            return False
        # "now resolved" resolves the blocker; "not delivered yet" does not.
        if RESOLUTION.search(text) and not NEGATION.search(text):
            # "materials delivered" resolves a materials blocker, not a permit wait.
            if MATERIALS.search(blocker) or not re.search(r"\bmaterials?\b", text):
                return False
    return True


def active_clauses(notes: str):
    """Yield blocker clauses that are still active.

    A clause with a blocker phrase starts a new group. The clauses after it without
    their own blocker ("now resolved", "no ETA") qualify only that blocker, so a
    resolved backorder cannot cancel a separate, still active dependency.
    """
    for sentence in re.split(r"[.!?;\n]+", notes.casefold()):
        # A leading condition applies to the whole sentence.
        if CONDITION.match(sentence.strip()):
            continue
        groups = []
        for clause in re.split(r",|\b(?:but|however)\b", sentence):
            # Missing updates or ETAs do not negate a dependency.
            clause = re.sub(r"\bno (?:eta|delivery date|updates?)\b", "", clause)
            if MATERIALS.search(clause) or UPSTREAM.search(clause):
                groups.append((clause, []))
            elif groups:
                groups[-1][1].append(clause)
        for blocker, qualifiers in groups:
            if is_active(blocker, qualifiers):
                yield blocker


def simple_interpretation(notes: str) -> str:
    """Recognize a few explicit blockers; unknown language stays unknown."""
    upstream = False
    for clause in active_clauses(notes):
        if MATERIALS.search(clause):
            return "materials"
        if UPSTREAM.search(clause):
            upstream = True
    return "upstream" if upstream else "none"


def warehouse_dependency(notes: str) -> bool:
    """Require an active materials clause linking the dependency to warehouse."""
    for clause in active_clauses(notes):
        if not MATERIALS.search(clause):
            continue
        if re.search(r"\b(?:awaiting|waiting for) materials from (?:the )?warehouse\b", clause):
            return True
        if re.match(r"\s*(?:the )?warehouse (?:confirmed )?backorder(?:ed)?\b", clause):
            return True
    return False

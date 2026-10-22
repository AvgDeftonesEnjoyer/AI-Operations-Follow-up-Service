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


def active_clauses(notes: str):
    """Yield recognized candidate clauses, skipping hypothetical/resolved text."""
    for sentence in re.split(r"[.!?;\n]+", notes.casefold()):
        # Keep a leading condition attached to the whole sentence.
        if re.match(r"\s*(if|unless|in case)\b", sentence):
            continue
        for clause in re.split(r",|\b(?:but|however)\b", sentence):
            # Missing updates or ETAs do not negate a dependency.
            text = re.sub(r"\bno (?:eta|delivery date|updates?)\b", "", clause)
            # A polite request (could you/we...) is not a hypothetical blocker.
            if re.search(r"\b(?:might|may|could)\s+(?!(?:you|we)\b)|\bhypothetical\b", text):
                continue
            if re.search(r"\b(no|not|never|resolved|delivered|cleared|completed)\b", text):
                continue
            yield text


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

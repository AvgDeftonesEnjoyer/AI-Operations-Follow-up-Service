"""Small English phrase rules for offline classification and safe routing."""

import re


MATERIALS = re.compile(
    r"\b(backorder(?:ed)?|"
    r"(?:awaiting|waiting (?:for|on)|blocked by|pending|until) (?:the )?materials?|"
    r"materials? (?:shortage|delay(?:ed)?))\b"
)
UPSTREAM = re.compile(
    r"\b(awaiting|waiting (?:for|on)|blocked by|dependent on|"
    r"cannot proceed until|unable to proceed until)\b"
)
# "no material shortage", "not waiting for materials": the negation sits right before the blocker.
NEGATED_BEFORE = re.compile(r"\b(?:no|not|never)\s+(?:\w+\s+)?$")
RESOLUTION = re.compile(r"\b(resolved|delivered|cleared|completed|received|arrived|no longer)\b")
# Words after which a resolution is only expected, not done: "until delivered", "to be delivered".
NOT_DONE_YET = re.compile(r"\b(?:until|once|when|after|to be|will be|not|never)\b")
CONDITION = re.compile(r"\b(if|unless|in case)\b")
# A polite request (could you/we...) or "could not" is not a hypothetical blocker.
HYPOTHETICAL = re.compile(r"\b(?:might|may|could)\s+(?!(?:you|we|not)\b)|\bhypothetical\b")


def reports_resolution(text: str) -> bool:
    """True for "backorder resolved"; false for "to be delivered" or "not delivered yet"."""
    done_part = NOT_DONE_YET.split(text, maxsplit=1)[0]
    return bool(RESOLUTION.search(done_part))


def is_active(blocker: str, qualifiers: list[str]) -> bool:
    """Check a blocker clause together with the clauses that qualify it."""
    match = MATERIALS.search(blocker) or UPSTREAM.search(blocker)
    if NEGATED_BEFORE.search(blocker[:match.start()]) or reports_resolution(blocker):
        return False
    if CONDITION.search(blocker) or HYPOTHETICAL.search(blocker):
        return False
    for text in qualifiers:
        # "..., if the delivery slips" makes the blocker conditional;
        # "..., call me if anything changes" does not.
        if CONDITION.match(text.strip()):
            return False
        if reports_resolution(text):
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
        if re.search(r"\bmaterials?(?: delivery)? from (?:the )?warehouse\b", clause):
            return True
        if re.match(r"\s*(?:the )?warehouse (?:confirmed )?backorder(?:ed)?\b", clause):
            return True
    return False

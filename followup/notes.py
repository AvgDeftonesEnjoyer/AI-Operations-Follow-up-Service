"""Small English keyword rules for offline classification and warehouse routing.

Each clause of a note is judged on its own. Deliberately simple: richer or ambiguous
notes are the LLM's job, not this fallback's.
"""

import re


ITEMS = r"materials?|parts|equipment|cable|conduit|shipment|deliver(?:y|ies)|backorder\w*|out of stock|shortage"
MATERIALS = re.compile(rf"\b({ITEMS}|warehouse|supplier|vendor)\b")
MATERIAL_ITEMS = re.compile(rf"\b({ITEMS})\b")
# Waiting on something: materials if the clause names materials, otherwise upstream work.
WAITING = re.compile(
    r"\b(await\w*|waiting|blocked by|dependent on|pending|backorder\w*|out of stock|"
    r"(?:proceed|start) until|(?:not|never|nothing) (?:yet )?(?:delivered|shipped|received|arrived))\b"
)
# Signals that count only next to a materials word ("short on conduit", "materials delayed").
SHORTAGE = re.compile(r"\b(shortage|short on|missing|not available|unavailable|need\w*|delay\w*)\b")
# "No backorder", "not delayed", "no missing materials": a negated blocker is not a blocker.
NEGATED = re.compile(
    r"\b(?:no|not|nothing|never) (?:await\w*|waiting|blocked by|dependent on|pending|"
    r"delay\w*|backorder\w*|out of stock)\b|\bno (?:\w+ )?(?:shortage|missing)\b"
)
RESOLVED = re.compile(r"\b(resolved|received|arrived|delivered|completed|approved|cleared|no longer)\b")
STILL_PENDING = re.compile(r"\b(no(?! longer)|not|never|nothing|until|yet)\b|n't")
# "If a backorder occurs" or "could become a problem" is not a blocker yet; "if possible" is courtesy.
HYPOTHETICAL = re.compile(r"\b(if(?! possible)|unless|in case|might|could become|may become)\b")
FILLER = {"have", "been", "were", "with", "from", "that", "this", "today", "fully", "also"}


def is_resolved(clause: str) -> bool:
    """'Backorder resolved' counts; 'not yet received' or 'until it is received' does not."""
    return bool(RESOLVED.search(clause)) and not STILL_PENDING.search(clause)


def subject(clause: str) -> set:
    """What a clause is about: 'permit completed' -> {'permit'}."""
    return set(re.findall(r"[a-z]{4,}", RESOLVED.sub("", clause))) - FILLER


def blocker_kind(clause: str):
    """'materials', 'upstream', or None for one clause."""
    if is_resolved(clause):
        return None
    if MATERIALS.search(clause) and (WAITING.search(clause) or SHORTAGE.search(clause)):
        return "materials"
    return "upstream" if WAITING.search(clause) else None


def active_blockers(notes: str):
    """(kind, clause) for every clause that still describes a blocker."""
    active = []
    text = NEGATED.sub("", " ".join(notes.casefold().split()))
    for clause in re.split(r"[.;,!?]|\b(?:and|but|however)\b", text):
        if HYPOTHETICAL.search(clause):
            continue
        kind = blocker_kind(clause)
        if kind:
            active.append((kind, clause))
        elif is_resolved(clause) and active and not WAITING.search(clause):
            # A later update closes the blocker it talks about.
            if MATERIAL_ITEMS.search(clause):  # "All materials have arrived."
                active = [b for b in active if b[0] != "materials"]
            elif subject(clause):  # "Permit completed." closes the permit wait, not others.
                active = [b for b in active if not subject(clause) & subject(b[1])]
            else:  # "..., now resolved." closes the blocker just mentioned.
                active.pop()
    return active


def simple_interpretation(notes: str) -> str:
    """Return materials, upstream, or none; anything unrecognized stays none."""
    kinds = {kind for kind, _ in active_blockers(notes)}
    if "materials" in kinds:
        return "materials"
    return "upstream" if kinds else "none"


def warehouse_dependency(notes: str) -> bool:
    """Ask the warehouse only when an active materials clause names it."""
    return any(kind == "materials" and "warehouse" in clause for kind, clause in active_blockers(notes))

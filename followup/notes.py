"""Small English keyword rules for offline classification and warehouse routing.

Each clause of a note is judged on its own. When in doubt the answer is "none", so the owner
gets the reminder: richer or ambiguous notes are the LLM's job, not this fallback's.
"""

import re


ITEMS = (r"materials?|supplies|parts|equipment|hardware|cable|fiber(?! splic)|conduit|inventory|"
         r"stock|shipment|shipping|(?<!work )order|deliver(?:y|ies)|backorder\w*|shortage")
MATERIALS = re.compile(rf"\b({ITEMS}|warehouse|supplier|vendor)\b")
MATERIAL_ITEMS = re.compile(rf"\b({ITEMS})\b")
# A blocker cue: waiting for, short of, or not received. What it waits for decides the kind.
WAITING = re.compile(
    r"\b(await\w*|waiting|pending|blocked|held up|on order|dependent on|depends on|backorder\w*|"
    r"out of|shortage|short on|missing|not available|unavailable|delay\w*|late|ran out|"
    r"run out|(?:proceed|start) until|(?:date|eta) (?:unknown|tbd)|unknown eta|no eta|"
    r"no (?:materials?|supplies|parts|equipment|cable|conduit))\b"
    r"|(?:\bnot|\bnever|\bnothing|n't) (?:yet )?(?:been )?(?:deliver|ship|receiv|arriv)\w*"
)
NOT_RECEIVED = re.compile(r"(?:\bnot|\bnever|\bnothing|n't) (?:yet )?(?:been )?(?:deliver|ship|receiv|arriv)\w*")
# Only a wait on upstream work, a permit, or another task ("blocked by TC-1008": letters, a
# dash, three or more digits; "Oct-25" is a date) goes past the owner to coordination.
UPSTREAM = re.compile(r"\b(upstream|permit|[a-z]{2,5}-\d{3,})\b")
# "No backorder", "not delayed", "no materials issues": a negated blocker is not a blocker.
NEGATED = re.compile(
    r"\b(?:no longer|no|not|nothing|never) (?:await\w*|waiting|pending|delay\w*|backorder\w*|out of stock|"
    r"blocked (?:by|on)|dependent on|upstream|late|missing)\b"
    r"|\bno (?:\w+ )?(?:shortage|missing|issues?|problems?|concerns?|needed|required)\b"
)
RESOLVED = re.compile(r"\b(resolved|received|arrived|delivered|completed|approved|cleared|no longer)\b")
# "Not yet received", "until it is received", "awaiting approved materials": not done yet.
STILL_PENDING = re.compile(r"\b(no(?! longer)|not|never|nothing|until|yet|await\w*|waiting|pending)\b|n't")
# "..., now resolved", "they arrived today": the update refers to the blocker just mentioned.
BARE_RESOLUTION = re.compile(
    r"\s*(?:(?:it|this|that|they|these|those) )?(?:is |are |was |were |has |have )?(?:been )?"
    r"(?:now |all )?(?:resolved|cleared|arrived|received|delivered)(?: today| yesterday)?\s*"
)
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
    if is_resolved(clause) or not WAITING.search(clause):
        return None
    if MATERIALS.search(clause):
        return "materials"
    return "upstream" if UPSTREAM.search(clause) else None


def active_blockers(notes: str):
    """(kind, clause) for every clause that still describes a blocker."""
    active = []
    previous = ""
    text = NEGATED.sub("", " ".join(notes.casefold().split()))
    for clause in re.split(r"[.;,!?]|\b(?:and|but|however)\b", text):
        if HYPOTHETICAL.search(clause):
            continue
        kind = blocker_kind(clause)
        if kind is None and NOT_RECEIVED.search(clause) and not is_resolved(previous):
            # "Materials ordered, but not received yet": it refers to the item just named.
            kind = blocker_kind(f"{previous} {clause}")
        previous = clause
        if kind:
            active.append((kind, clause))
        elif is_resolved(clause) and active:
            # A later update closes the blocker it talks about, and only that one.
            if MATERIAL_ITEMS.search(clause):  # "All materials have arrived."
                active = [b for b in active if b[0] != "materials"]
            elif BARE_RESOLUTION.fullmatch(clause):  # "..., now resolved."
                active.pop()
            else:  # "Permit completed." closes the permit wait; "PO approved" closes nothing.
                active = [b for b in active if not subject(clause) & subject(b[1])]
    return active


def simple_interpretation(notes: str) -> str:
    """Return materials, upstream, or none; anything unrecognized stays none."""
    kinds = {kind for kind, _ in active_blockers(notes)}
    if "materials" in kinds:
        return "materials"
    return "upstream" if kinds else "none"


def warehouse_dependency(notes: str) -> bool:
    """Ask the warehouse only when an active materials clause names it; otherwise the owner."""
    return any(kind == "materials" and "warehouse" in clause for kind, clause in active_blockers(notes))

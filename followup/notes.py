"""Offline note rules: the fallback when there is no model answer.

A note is split into clauses. A clause is a blocker when a blocker cue ("awaiting",
"short on", "not received") stands next to what it waits for: a materials word makes it
"materials", an upstream word makes it "upstream". Negated and hypothetical clauses do not
count, and a later resolved clause closes the blocker it talks about. When in doubt the
answer is "none", so the owner gets the reminder; richer notes are the LLM's job.
"""

import re


def any_of(words):
    return "|".join(words)


# What a materials blocker waits for.
ITEMS = [
    r"materials?", "supplies", "parts", "equipment", "hardware", "cable", "conduit", r"handholes?",
    "inventory", "stock", "shipment", "shipping", r"deliver(?:y|ies)", r"back[- ]?order\w*", "shortage",
    r"fiber(?! splic)",  # fiber is a material; "fiber splicing" is work
    r"(?<!work )order",  # a purchase order, not a work order
]
SOURCES = ["warehouse", "supplier", "vendor"]

# What an upstream blocker waits for: other work, a permit, or another task such as TC-1008
# (letters, a dash, three or more digits; "Oct-25" is a date and PO-4471 is an order).
UPSTREAM_WORDS = ["upstream", "permit", r"(?!po-)[a-z]{2,5}-\d{3,}"]

# "Not delivered", "never arrived", "haven't received".
NOT_RECEIVED_PATTERN = r"(?:\bnot|\bnever|\bnothing|n't) (?:yet )?(?:been )?(?:deliver|ship|receiv|arriv)\w*"

CUES = [
    r"await\w*", "waiting", "pending", "blocked", "held up", "stuck", "in transit", "on order",
    "dependent on", "depends on", r"back[- ]?order\w*", "out of", "shortage", "short on", "missing",
    "not available", "unavailable", r"delay\w*", "late", "ran out", "run out",
    r"(?:proceed|start) (?:until|unless)", r"(?:date|eta) (?:unknown|tbd)", "unknown eta", "no eta",
    r"no (?:materials?|supplies|parts|equipment|cable|conduit)",
]

# A negation right before a cue cancels it: "no backorder", "not delayed", "no longer waiting".
NEGATIONS = ["no longer", "no", "not", "nothing", "never"]
NEGATABLE_CUES = [
    r"await\w*", "waiting", "pending", r"delay\w*", r"back[- ]?order\w*", "out of stock",
    "blocked (?:by|on)", "dependent on", "upstream", "late", "missing",
]
# "No materials issues", "no parts required": nothing is wrong.
NO_PROBLEM = ["shortage", "missing", r"issues?", r"problems?", r"concerns?", "needed", "required"]

RESOLUTIONS = ["resolved", "received", "arrived", "delivered", "completed", "approved", "issued",
               "fulfilled", "filled", "cleared", "no longer"]
# A resolution word in the same clause as one of these has not happened yet:
# "not yet received", "until it is received", "awaiting approved materials".
NOT_DONE_YET = [r"no(?! longer)", "not", "never", "nothing", "until", "yet", r"await\w*", "waiting", "pending"]

# "If a backorder occurs" or "could become a problem" is not a blocker yet.
HYPOTHETICALS = [
    r"if(?! possible)",      # "if possible" is courtesy
    r"(?<!proceed )unless",  # "can't proceed unless" is a real wait
    "in case", "might", "could become", "may become",
]


MATERIALS = re.compile(rf"\b(?:{any_of(ITEMS + SOURCES)})\b")
MATERIAL_ITEMS = re.compile(rf"\b(?:{any_of(ITEMS)})\b")
UPSTREAM = re.compile(rf"\b(?:{any_of(UPSTREAM_WORDS)})\b")
NOT_RECEIVED = re.compile(NOT_RECEIVED_PATTERN)
WAITING = re.compile(rf"\b(?:{any_of(CUES)})\b|{NOT_RECEIVED_PATTERN}")
NEGATED = re.compile(rf"\b(?:{any_of(NEGATIONS)}) (?:{any_of(NEGATABLE_CUES)})\b"
                     rf"|\bno (?:\w+ )?(?:{any_of(NO_PROBLEM)})\b")
RESOLVED = re.compile(rf"\b(?:{any_of(RESOLUTIONS)})\b")
STILL_PENDING = re.compile(rf"\b(?:{any_of(NOT_DONE_YET)})\b|n't")
HYPOTHETICAL = re.compile(rf"\b(?:{any_of(HYPOTHETICALS)})\b")
# "..., now resolved", "they arrived today", "Update: received": a resolution that names nothing.
BARE_RESOLUTION = re.compile(
    r"\s*(?:update: ?)?(?:(?:it|this|that|they|these|those) )?(?:is |are |was |were |has |have )?(?:been )?"
    r"(?:now |all )?(?:resolved|cleared|arrived|received|delivered)(?: today| yesterday)?\s*"
)
FILLER = {"have", "been", "were", "with", "from", "that", "this", "today", "fully", "also"}


def is_resolved(clause: str) -> bool:
    """'Backorder resolved' counts; 'not yet received' does not."""
    return bool(RESOLVED.search(clause)) and not STILL_PENDING.search(clause)


def blocker_kind(clause: str):
    """'materials', 'upstream', or None for one clause."""
    if is_resolved(clause) or not WAITING.search(clause):
        return None
    if MATERIALS.search(clause):
        return "materials"
    return "upstream" if UPSTREAM.search(clause) else None


def subject(clause: str) -> set:
    """What a clause is about: 'permit completed' -> {'permit'}."""
    return set(re.findall(r"[a-z]{4,}", RESOLVED.sub("", clause))) - FILLER


def close(active: list, update: str) -> list:
    """Remove the blockers that a resolved clause talks about."""
    if MATERIAL_ITEMS.search(update):  # "All materials have arrived."
        return [blocker for blocker in active if blocker[0] != "materials"]
    if BARE_RESOLUTION.fullmatch(update):  # "..., now resolved."
        return active[:-1]
    # "Permit completed." closes the permit wait; "PO approved" shares no word and closes nothing.
    return [blocker for blocker in active if not subject(update) & subject(blocker[1])]


def active_blockers(notes: str) -> list:
    """(kind, clause) for every clause that still describes a blocker."""
    active, previous = [], ""
    text = NEGATED.sub("", " ".join(notes.casefold().split()))
    for clause in re.split(r"[.;,!?]|\b(?:and|but|however)\b", text):
        if not clause.strip() or HYPOTHETICAL.search(clause):
            continue
        kind = blocker_kind(clause)
        if kind is None and NOT_RECEIVED.search(clause) and not is_resolved(previous):
            # "Materials ordered, but not received yet": it refers to the item named before.
            kind = blocker_kind(f"{previous} {clause}")
        previous = clause
        if kind:
            active.append((kind, clause))
        elif is_resolved(clause) and active:
            active = close(active, clause)
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

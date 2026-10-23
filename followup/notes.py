"""Small English keyword rules for offline classification and warehouse routing.

Deliberately simple: mixed or ambiguous notes are the LLM's job, not this fallback's.
"""

import re


BLOCKING_WORDS = r"await\w*|waiting|blocked by|dependent on|pending|delay\w*|backorder\w*|out of stock"
MATERIALS = re.compile(
    r"\b(materials?|parts|equipment|cable|conduit|shipment|deliver(?:y|ies)|supplier|vendor|"
    r"warehouse|backorder\w*|out of stock|shortage)\b"
)
WAITING = re.compile(
    rf"\b({BLOCKING_WORDS}|(?:proceed|start) until|"
    r"(?:not|never|nothing) (?:yet )?(?:delivered|shipped|received|arrived))\b"
)
# "No backorder", "not delayed", "nothing pending": a negated blocking word is not a blocker.
NEGATED = re.compile(rf"\b(?:no|not|nothing|never)\s+(?:{BLOCKING_WORDS})\b")
RESOLVED = re.compile(r"\b(resolved|received|arrived|no longer)\b")
STILL_PENDING = re.compile(r"\b(not|never|nothing|until|yet)\b|n't")


def is_resolved(text: str) -> bool:
    """'Backorder resolved' counts; 'not yet received' or 'until it is received' does not."""
    return any(RESOLVED.search(clause) and not STILL_PENDING.search(clause)
               for clause in re.split(r"[.;,]", text))


def simple_interpretation(notes: str) -> str:
    """Return materials, upstream, or none; anything unrecognized stays none."""
    text = NEGATED.sub("", notes.casefold())
    if not WAITING.search(text) or is_resolved(text):
        return "none"
    return "materials" if MATERIALS.search(text) else "upstream"


def warehouse_dependency(notes: str) -> bool:
    """A materials blocker goes to the warehouse only when the notes name it."""
    return "warehouse" in notes.casefold()

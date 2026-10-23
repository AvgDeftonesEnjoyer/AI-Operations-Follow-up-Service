"""Small English keyword rules for offline classification and warehouse routing.

Deliberately simple: mixed or ambiguous notes are the LLM's job, not this fallback's.
"""

import re


MATERIALS = re.compile(
    r"\b(materials?|parts|equipment|cable|conduit|shipment|backorder\w*|out of stock|"
    r"from (?:the )?warehouse)\b"
)
WAITING = re.compile(
    r"\b(await\w*|waiting|blocked by|dependent on|pending|delay\w*|backorder\w*|out of stock|"
    r"(?:proceed|start) until|not (?:yet )?(?:delivered|shipped))\b"
)
RESOLVED = re.compile(r"\b(?<!not )(resolved|no longer|received|arrived)\b")


def simple_interpretation(notes: str) -> str:
    """Return materials, upstream, or none; anything unrecognized stays none."""
    text = notes.casefold()
    if not WAITING.search(text) or RESOLVED.search(text):
        return "none"
    return "materials" if MATERIALS.search(text) else "upstream"


def warehouse_dependency(notes: str) -> bool:
    """A materials blocker goes to the warehouse only when the notes name it."""
    return "warehouse" in notes.casefold()

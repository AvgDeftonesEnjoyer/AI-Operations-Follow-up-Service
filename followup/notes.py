"""Small English phrase rules for offline classification and safe routing."""

from dataclasses import dataclass
import re


MATERIALS = re.compile(
    r"\b(backorder(?:ed)?|awaiting materials|waiting (?:for|on) materials|"
    r"missing materials|materials? shortage|materials? delay(?:ed)?|"
    r"materials? (?:have |has )?not (?:yet )?arrived)\b"
)
UPSTREAM = re.compile(
    r"\b(awaiting|waiting (?:for|on)|blocked by|dependent on|"
    r"cannot proceed until|unable to proceed until|upstream dependency)\b"
)


@dataclass
class BlockerMention:
    text: str
    kind: str
    active: bool = True


def negated_prefix(text: str) -> bool:
    return re.search(r"\b(?:no|not|never)(?: longer| all| any)?\s*$", text) is not None


def resolved_kind(text: str):
    """Recognize explicit completion of a dependency, not unrelated work."""
    completed = r"(?:is |are |was |were |has been |have been |has |have )?(?:now )?"
    completed += r"(?:resolved|delivered|cleared|completed|arrived)\b"
    patterns = [
        ("materials", r"\b(?:materials?(?: shortage| delay)?|backorder) " + completed),
        ("upstream", r"\b(?:upstream dependency|permit|splice) " + completed),
        ("materials", r"\bwarehouse (?:has )?delivered (?:all |the )?materials\b"),
    ]
    for kind, pattern in patterns:
        match = re.search(pattern, text)
        if match and not negated_prefix(text[:match.start()]):
            return kind
    return None


def active_clauses(notes: str):
    """Track explicit blockers, their negations, and subsequent resolutions."""
    mentions = []
    notes = re.sub(r"\bif possible\b", "", notes.casefold())
    for sentence in re.split(r"[.!?;\n]+", notes):
        if re.match(r"\s*(if|unless|in case)\b", sentence):
            continue
        previous = None
        for clause in re.split(r",|\b(?:but|however|and)\b", sentence):
            text = re.sub(r"\bno (?:eta|delivery date|updates?)\b", "", clause)
            text = " ".join(text.split())
            adjacent, previous = previous, None
            kind, match = "materials", MATERIALS.search(text)
            if match is None:
                kind, match = "upstream", UPSTREAM.search(text)

            if re.search(r"\b(if|unless|in case|hypothetical)\b|\b(?:might|may|could)\s+(?!(?:you|we)\b)", text):
                # A detached condition applies only to the immediately preceding clause.
                if adjacent and re.match(r"if (?:the )?(?:delivery|shipment) (?:slips|is delayed)\b", text):
                    adjacent.active = False
                continue

            resolved = resolved_kind(text)
            # Negation must directly precede the blocker, not an unrelated later fact.
            negated = match and negated_prefix(text[:match.start()])
            if resolved or negated:
                for mention in mentions:
                    if mention.kind == (resolved or kind):
                        mention.active = False
                continue
            if re.fullmatch(r"(?:(?:it|this|that) (?:is |was )?)?(?:now )?(?:resolved|delivered|cleared|completed)", text):
                if mentions:
                    mentions[-1].active = False
                continue
            if match:
                previous = BlockerMention(text, kind)
                mentions.append(previous)

    for mention in mentions:
        if mention.active:
            yield mention.text


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
        if re.search(r"\b(?:awaiting|waiting (?:for|on)|missing) materials from (?:the )?warehouse\b", clause):
            return True
        if re.search(r"\bmaterials? (?:have |has )?not (?:yet )?arrived from (?:the )?warehouse\b", clause):
            return True
        if re.match(r"\s*(?:the )?warehouse (?:confirmed )?backorder(?:ed)?\b", clause):
            return True
    return False

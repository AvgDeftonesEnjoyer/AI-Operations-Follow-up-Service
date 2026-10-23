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
    r"cannot proceed until|unable to proceed until)\b"
)


@dataclass
class BlockerMention:
    text: str
    kind: str
    active: bool = True


def active_clauses(notes: str):
    """Keep blocker mentions until later clauses have resolved or qualified them.

    Named resolutions affect that category; a short "now resolved" update affects
    the latest mention. Unrelated completed work does not resolve a dependency.
    """
    mentions = []
    for sentence in re.split(r"[.!?;\n]+", notes.casefold()):
        if re.match(r"\s*(if|unless|in case)\b", sentence):
            continue
        for clause in re.split(r",|\b(?:but|however|and)\b", sentence):
            text = re.sub(r"\bno (?:eta|delivery date|updates?)\b", "", clause)
            # Courtesy applies to the request, not to whether a blocker exists.
            text = re.sub(r"\bif possible\b", "", text)
            kind = None
            if MATERIALS.search(text):
                kind = "materials"
            elif UPSTREAM.search(text):
                kind = "upstream"
            conditional = re.search(
                r"\b(if|unless|in case|hypothetical)\b|\b(?:might|may|could)\s+(?!(?:you|we)\b)", text
            )
            if conditional:
                # Only an explicit delivery condition can cancel a preceding mention.
                delivery_condition = re.match(
                    r"\s*if (?:the )?(?:delivery|shipment) (?:slips|is delayed)\b", text
                )
                if kind is None and delivery_condition and mentions:
                    mentions[-1].active = False
                continue

            # Negative completion/arrival statements keep a blocker active.
            state_text = re.sub(r"\bnot (?:yet )?(?:resolved|delivered|cleared|completed|arrived)\b", "", text)
            inactive = re.search(r"\b(no|not|never|resolved|delivered|cleared|completed)\b", state_text)
            if inactive:
                # An unrelated negation, e.g. "warehouse is not involved", is not a resolution.
                if kind is None and not re.search(r"\b(resolved|delivered|cleared|completed)\b", state_text):
                    continue
                subject = kind
                if subject is None and re.search(
                    r"\b(?:materials?|backorder) (?:are |were |is |was )?"
                    r"(?:now )?(?:resolved|delivered|cleared|completed)\b", text
                ):
                    subject = "materials"
                if subject is None and re.search(
                    r"\b(?:upstream|permit|splice) (?:is |was )?"
                    r"(?:now )?(?:resolved|cleared|completed)\b", text
                ):
                    subject = "upstream"
                if subject is not None:
                    for mention in mentions:
                        if mention.kind == subject:
                            mention.active = False
                elif mentions and re.fullmatch(
                    r"\s*(?:(?:it|this|that) (?:is |was )?)?(?:now )?"
                    r"(?:resolved|delivered|cleared|completed)\s*", text
                ):
                    mentions[-1].active = False
                continue
            if kind is not None:
                mentions.append(BlockerMention(text, kind))

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

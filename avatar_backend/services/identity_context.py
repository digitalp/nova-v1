"""Identity-aware prompt context injection.

Given a person_id, builds a context block that tells the LLM who it's speaking with
and how to adjust its behaviour.
"""
from __future__ import annotations

from avatar_backend.services.family_service import FamilyService, Person

# Per-person communication style
_PERSON_PROFILES: dict[str, dict] = {
    "penn": {
        "style": "Direct, technical, concise. Full detail is fine. Penn is the system admin.",
        "topics": "home automation, tech, parenting, work",
    },
    "tse": {
        "style": "Warm, helpful, practical. Tse manages the household.",
        "topics": "cooking, kids schedules, shopping, home organisation",
    },
    "jason": {
        "style": "Friendly, age-appropriate (10+). Keep explanations simple but not patronising. Jason is tech-curious.",
        "topics": "gaming, school, football, YouTube",
    },
    "joel": {
        "style": "Playful, encouraging, simple language (7+). Joel likes jokes and fun facts.",
        "topics": "cartoons, Roblox, football, animals",
    },
    "miya": {
        "style": "Gentle, very simple language (5+). Short sentences. Miya likes stories and drawing.",
        "topics": "drawing, stories, dolls, animals",
    },
}


def build_identity_context(person_id: str | None, family_service: FamilyService | None = None) -> str:
    """Build a prompt injection block for the identified person."""
    if not person_id:
        return ""

    person: Person | None = None
    if family_service:
        person = family_service.get_person(person_id)

    profile = _PERSON_PROFILES.get(person_id.lower(), {})
    if not profile and not person:
        return ""

    name = person.display_name if person else person_id.title()
    role = person.role if person else "adult"
    style = profile.get("style", "Be helpful and friendly.")

    lines = [
        f"\n[ACTIVE USER: {name} ({role})]",
        f"Communication style: {style}",
    ]

    # Add parental restrictions for children
    if role == "child":
        lines.append("RESTRICTIONS: Do not share adult content, financial details, or other family members' private conversations.")
        lines.append("If asked about another family member's private matters, politely decline.")

    return "\n".join(lines)

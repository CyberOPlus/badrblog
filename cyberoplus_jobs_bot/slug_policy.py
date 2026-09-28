from __future__ import annotations

import re
import unicodedata

from .job_identity import identity_key


ARABIC_MAP = {
    "ا":"a","أ":"a","إ":"a","آ":"a","ب":"b","ت":"t","ث":"th","ج":"j","ح":"h",
    "خ":"kh","د":"d","ذ":"dh","ر":"r","ز":"z","س":"s","ش":"sh","ص":"s","ض":"d",
    "ط":"t","ظ":"z","ع":"a","غ":"gh","ف":"f","ق":"q","ك":"k","ل":"l","م":"m",
    "ن":"n","ه":"h","ة":"a","و":"w","ؤ":"w","ي":"y","ى":"a","ئ":"y",
}

STOP = {
    "job", "jobs", "emploi", "offre", "recrutement", "hiring", "vacancy",
    "poste", "postes", "positions", "position",
}


def _latinize(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    out = []
    for ch in text:
        if ch in ARABIC_MAP:
            out.append(ARABIC_MAP[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append(" ")
    value = "".join(out).casefold()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    tokens = [x for x in value.split("-") if x and x not in STOP]
    return "-".join(tokens)


def _campaign_token(candidate):
    raw = candidate.raw if isinstance(candidate.raw, dict) else {}
    token = str(raw.get("_campaign_id") or "").strip().lower()
    token = re.sub(r"[^a-z0-9]", "", token)
    if token:
        return token[:8]

    # Preview-only fallback. Live publishing should reserve a persistent
    # campaign_id before calling desired_slug().
    return identity_key(candidate)[:8]


def desired_slug(candidate):
    """Stable slug key for one recruitment campaign.

    Human-readable part = company + role only.
    Stable suffix = persistent campaign_id.

    Deliberately excluded:
    - publication year/date
    - deadline
    - number of positions
    - salary
    - location
    - mutable contract details

    Therefore 100 -> 20 positions, a deadline extension, salary correction, or
    city correction never changes the slug of an existing Blogger post.
    """
    company = _latinize(candidate.company) or "employer"
    role = _latinize(candidate.title) or "position"

    base = "-".join([
        *company.split("-")[:3],
        *role.split("-")[:5],
    ])
    base = re.sub(r"-+", "-", base).strip("-")[:74].strip("-")
    return f"{base}-{_campaign_token(candidate)}"


def title_disambiguator(candidate):
    """Human-readable title helper; never part of the stable permalink key."""
    parts = []
    if candidate.company:
        parts.append(candidate.company)
    if candidate.location:
        parts.append(candidate.location)
    try:
        positions = int(candidate.number_of_positions or 0)
    except (TypeError, ValueError):
        positions = 0
    if positions > 1:
        parts.append(f"{positions} منصب")
    return " – ".join(parts)

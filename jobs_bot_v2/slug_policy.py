from __future__ import annotations

import hashlib
import re
import unicodedata

from .job_identity import identity_key, normalize_text


ARABIC_MAP = {
    "ا":"a","أ":"a","إ":"a","آ":"a","ب":"b","ت":"t","ث":"th","ج":"j","ح":"h",
    "خ":"kh","د":"d","ذ":"dh","ر":"r","ز":"z","س":"s","ش":"sh","ص":"s","ض":"d",
    "ط":"t","ظ":"z","ع":"a","غ":"gh","ف":"f","ق":"q","ك":"k","ل":"l","م":"m",
    "ن":"n","ه":"h","ة":"a","و":"w","ؤ":"w","ي":"y","ى":"a","ئ":"y",
}

STOP = {"job","jobs","emploi","offre","recrutement","hiring","vacancy","poste"}


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


def desired_slug(candidate):
    """Deterministic future-proof slug key.

    Blogger v3 insert does not accept a custom permalink in this project, so
    this is stored for identity/migration/QA. The actual public Blogger URL is
    always the URL returned by Blogger and is never guessed.
    """
    company = _latinize(candidate.company) or "company"
    role = _latinize(candidate.title) or "position"
    location = _latinize(candidate.location) or "morocco"
    core = "-".join([
        *company.split("-")[:2],
        *role.split("-")[:4],
        *location.split("-")[:2],
    ])
    core = re.sub(r"-+", "-", core).strip("-")[:72].strip("-")
    suffix = identity_key(candidate)[:7]
    return f"{core}-{suffix}"


def title_disambiguator(candidate):
    """Small human-readable differentiator for genuinely separate campaigns."""
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

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone

from .job_identity import canonicalize_url, external_reference, semantic_key


CRITICAL_FIELDS = (
    "number_of_positions",
    "deadline",
    "location",
    "application_url",
    "salary",
    "contract_type",
)

FILL_FIELDS = (
    "company",
    "location",
    "country",
    "contract_type",
    "salary",
    "deadline",
    "published_at",
    "description",
    "application_url",
    "diploma",
    "experience",
    "main_image",
)


def _priority_rank(candidate):
    rank = {
        "s+": 50,
        "s": 45,
        "a+": 40,
        "a": 35,
        "b": 25,
    }.get(str(candidate.source_priority or "").strip().lower(), 10)
    if candidate.official_source:
        rank += 100
    return rank


def _parse_date(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _same_campaign_signal(a, b):
    ref_a, ref_b = external_reference(a), external_reference(b)
    if ref_a and ref_b and ref_a == ref_b:
        return True

    apply_a = canonicalize_url(a.application_url)
    apply_b = canonicalize_url(b.application_url)
    if apply_a and apply_b and apply_a == apply_b:
        return True

    canonical_a = canonicalize_url(a.canonical_url or a.source_url)
    canonical_b = canonicalize_url(b.canonical_url or b.source_url)
    if canonical_a and canonical_b and canonical_a == canonical_b:
        return True

    # A discovery record without a stable application URL may be attached to an
    # official record only when the semantic identity matches exactly.
    if semantic_key(a) == semantic_key(b):
        if a.official_source != b.official_source and (not apply_a or not apply_b):
            return True
    return False


def _conflicts(a, b):
    conflicts = []
    for field in CRITICAL_FIELDS:
        av = getattr(a, field, None)
        bv = getattr(b, field, None)
        if field == "application_url":
            av, bv = canonicalize_url(av), canonicalize_url(bv)
        if av not in (None, "", 0, []) and bv not in (None, "", 0, []) and str(av) != str(bv):
            conflicts.append(field)
    return conflicts


def _choose_master(a, b):
    ra, rb = _priority_rank(a), _priority_rank(b)
    if ra != rb:
        return (a, b) if ra > rb else (b, a)

    da, db = _parse_date(a.published_at), _parse_date(b.published_at)
    if da and db and da != db:
        return (a, b) if da > db else (b, a)
    return a, b


def _merge(master, secondary):
    merged = deepcopy(master)
    for field in FILL_FIELDS:
        current = getattr(merged, field, None)
        incoming = getattr(secondary, field, None)
        if current in (None, "", 0, []) and incoming not in (None, "", 0, []):
            setattr(merged, field, incoming)

    merged.remote = bool(master.remote or secondary.remote)
    merged.visa_sponsorship = bool(master.visa_sponsorship or secondary.visa_sponsorship)
    merged.relocation = bool(master.relocation or secondary.relocation)
    merged.entry_level = bool(master.entry_level or secondary.entry_level)

    docs = []
    for value in list(master.documents_required or []) + list(secondary.documents_required or []):
        if value and value not in docs:
            docs.append(value)
    merged.documents_required = docs

    raw = dict(master.raw or {})
    raw.setdefault("_merged_sources", [])
    raw["_merged_sources"].append({
        "source_name": secondary.source_name,
        "source_url": secondary.source_url,
    })
    merged.raw = raw
    return merged


def reconcile_batch(candidates):
    """Merge clearly identical cross-source jobs and hold unresolved official conflicts."""
    remaining = list(candidates)
    output = []
    held = []

    while remaining:
        current = remaining.pop(0)
        group = [current]
        rest = []

        for other in remaining:
            if _same_campaign_signal(current, other):
                group.append(other)
            else:
                rest.append(other)
        remaining = rest

        if len(group) == 1:
            output.append(current)
            continue

        master = group[0]
        blocked = False
        group_conflicts = []

        for other in group[1:]:
            chosen, secondary = _choose_master(master, other)
            conflicts = _conflicts(chosen, secondary)

            # Conflicts between two official records of equal source strength are
            # too risky to guess. A newer timestamp can resolve it; otherwise hold.
            if conflicts and chosen.official_source and secondary.official_source:
                chosen_date = _parse_date(chosen.published_at)
                secondary_date = _parse_date(secondary.published_at)
                same_strength = _priority_rank(chosen) == _priority_rank(secondary)
                if same_strength and (not chosen_date or not secondary_date or chosen_date == secondary_date):
                    blocked = True
                    group_conflicts.extend(conflicts)
                    continue

            master = _merge(chosen, secondary)

        if blocked:
            held.append({
                "semantic_key": semantic_key(current),
                "title": current.title,
                "company": current.company,
                "reason": "conflicting authoritative sources",
                "conflicting_fields": sorted(set(group_conflicts)),
                "sources": [x.source_name for x in group],
            })
        else:
            output.append(master)

    return output, held

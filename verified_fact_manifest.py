# ============================================================
# verified_fact_manifest.py - Jobs fact contract and validation
# ============================================================

import re
from datetime import datetime

from duplicate_utils import canonicalize_url
from job_core import is_application_url_bound_to_job


HIGH = "high"
MEDIUM = "medium"
HEURISTIC = "heuristic"

SPECIALTY_HINTS = (
    "التخصص", "التخصصات", "الشعبة", "الشعب", "المسلك", "المسالك",
    "specialite", "spécialité", "specialites", "spécialités",
    "filiere", "filière", "profil", "profile", "grade", "الدرجة", "الإطار", "الاطار",
)
TEST_HINTS = (
    "اختبار", "الاختبار", "امتحان", "الامتحان", "المباراة", "كتابي", "شفوي",
    "epreuve", "épreuve", "epreuves", "épreuves", "test", "exam", "oral", "ecrit", "écrit",
)
POSITION_HINTS = ("منصب", "مناصب", "poste", "postes", "position", "positions")
DEADLINE_HINTS = (
    "آخر أجل", "اخر اجل", "موعد انتهاء", "تاريخ انتهاء", "deadline",
    "date limite", "clôture", "cloture", "fin de candidature",
)
SALARY_HINTS = ("راتب", "الراتب", "الأجر", "الاجر", "salaire", "salary", "rémunération", "remuneration")
EXPERIENCE_HINTS = ("خبرة", "الخبرة", "experience", "expérience")
DIPLOMA_HINTS = ("دبلوم", "الدبلوم", "شهادة", "الشهادة", "diplome", "diplôme", "degree")
EXAM_DATE_HINTS = ("تاريخ المباراة", "تاريخ الاختبار", "موعد المباراة", "exam date", "date du concours")

HEADER_LIKE_HINTS = (
    "التخصص", "التخصصات", "الشعبة", "الشعب", "المسلك", "المسالك",
    "عدد المناصب", "المناصب", "منصب", "الدرجة", "الإطار", "الاطار",
    "الاختبار", "الاختبارات", "المعامل", "المدة", "الشهادة", "الدبلوم",
    "specialite", "spécialité", "specialites", "spécialités",
    "filiere", "filière", "poste", "postes", "nombre de postes",
    "grade", "epreuve", "épreuve", "coefficient", "duree", "durée",
)


def _looks_like_header_value(value):
    normalized = _normalize(value)
    if not normalized:
        return True
    if any(normalized == _normalize(hint) for hint in HEADER_LIKE_HINTS):
        return True
    return False


def _normalize(value):
    text = str(value or "").casefold()
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _canonical_dates(value):
    text = _normalize(value)
    out = set()
    for a, b, c in re.findall(r"\b(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\b", text):
        try:
            if len(a) == 4:
                dt = datetime(int(a), int(b), int(c))
            elif len(c) == 4:
                dt = datetime(int(c), int(b), int(a))
            else:
                continue
            out.add(dt.strftime("%Y-%m-%d"))
        except (TypeError, ValueError):
            continue

    month_numbers = {
        "يناير": 1, "فبراير": 2, "مارس": 3, "ابريل": 4, "ماي": 5,
        "مايو": 5, "يونيو": 6, "يوليوز": 7, "يوليو": 7, "غشت": 8,
        "اغسطس": 8, "شتنبر": 9, "سبتمبر": 9, "اكتوبر": 10,
        "نونبر": 11, "نوفمبر": 11, "دجنبر": 12, "ديسمبر": 12,
        "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
        "décembre": 12,
    }
    normalized_months = {_normalize(name): number for name, number in month_numbers.items()}
    if normalized_months:
        month_pattern = "|".join(
            sorted((re.escape(name) for name in normalized_months), key=len, reverse=True)
        )
        for day, month_name, year in re.findall(
            rf"\b(\d{{1,2}})\s+({month_pattern})\s+(\d{{4}})\b",
            text,
            flags=re.I,
        ):
            try:
                dt = datetime(int(year), normalized_months[month_name], int(day))
                out.add(dt.strftime("%Y-%m-%d"))
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _text_contains_value(text, value):
    value_text = _normalize(value)
    if not value_text:
        return False
    haystack = _normalize(text)
    if value_text in haystack:
        return True
    value_dates = _canonical_dates(value_text)
    return bool(value_dates and value_dates & _canonical_dates(haystack))


def _table_text(article):
    parts = []
    for table in article.get("source_tables") or []:
        if not isinstance(table, dict):
            continue
        if table.get("caption"):
            parts.append(str(table.get("caption")))
        for row in table.get("rows") or []:
            if isinstance(row, (list, tuple)):
                parts.append(" | ".join(str(cell or "") for cell in row))
    return "\n".join(parts)


def _pdf_text(article):
    return "\n".join(
        str(page.get("text") or "")
        for page in (article.get("job_document_texts") or [])
        if isinstance(page, dict) and page.get("text")
    )


def _detail_text(article):
    return str(
        article.get("full_article_text")
        or article.get("content_full")
        or article.get("content_preview")
        or ""
    )


def _has_context_value(text, value, hints):
    normalized = _normalize(text)
    value_norm = _normalize(value)
    if not normalized or not value_norm:
        return False
    if value_norm not in normalized and not (_canonical_dates(value_norm) & _canonical_dates(normalized)):
        return False
    return any(_normalize(hint) in normalized for hint in hints)


def _source_for_scalar(article, value, hints=()):
    if value in (None, "", [], {}):
        return "missing", HEURISTIC

    official = bool(article.get("official_source") or article.get("job_official_source"))
    table_text = _table_text(article)
    pdf_text = _pdf_text(article)
    detail_text = _detail_text(article)

    if hints:
        if _has_context_value(table_text, value, hints):
            return "source_table", HIGH if official else MEDIUM
        if _has_context_value(pdf_text, value, hints):
            return "official_pdf", HIGH if official else MEDIUM
        if _has_context_value(detail_text, value, hints):
            return "official_detail_page", HIGH if official else MEDIUM
    else:
        if _text_contains_value(table_text, value):
            return "source_table", HIGH if official else MEDIUM
        if _text_contains_value(pdf_text, value):
            return "official_pdf", HIGH if official else MEDIUM
        if _text_contains_value(detail_text, value):
            return "official_detail_page", HIGH if official else MEDIUM

    return "extracted_field", MEDIUM


def _fact(value, source, confidence, *, required=False, aliases=None, meta=None):
    return {
        "value": value,
        "source": source,
        "confidence": confidence,
        "blocking": bool(confidence == HIGH and required),
        "required_in_output": bool(required),
        "aliases": list(aliases or []),
        "meta": dict(meta or {}),
    }


def _append_fact(manifest, category, fact):
    if fact.get("value") in (None, "", [], {}):
        return
    manifest["facts"].setdefault(category, []).append(fact)


def _position_source(article, positions):
    if not positions:
        return "missing", HEURISTIC
    value = str(positions)
    for source_name, text in (
        ("source_table", _table_text(article)),
        ("official_pdf", _pdf_text(article)),
        ("official_detail_page", _detail_text(article)),
    ):
        normalized = _normalize(text)
        if not normalized:
            continue
        for hint in POSITION_HINTS:
            hint_norm = _normalize(hint)
            if re.search(rf"(?:{re.escape(value)}\s+\w*{re.escape(hint_norm)}|{re.escape(hint_norm)}[^.\n]{{0,40}}\b{re.escape(value)}\b)", normalized):
                confidence = HIGH if bool(article.get("official_source") or article.get("job_official_source")) else MEDIUM
                return source_name, confidence
    return "extracted_field", MEDIUM


def _table_confidence(article):
    official = bool(article.get("official_source") or article.get("job_official_source"))
    truncated = bool(article.get("source_tables_truncated"))
    return HIGH if official and not truncated else MEDIUM


def _column_table_facts(article, hints, category):
    facts = []
    confidence = _table_confidence(article)

    for table_index, table in enumerate(article.get("source_tables") or []):
        if not isinstance(table, dict):
            continue
        rows = [
            list(row)
            for row in (table.get("rows") or [])
            if isinstance(row, (list, tuple)) and row
        ]
        if len(rows) < 2:
            continue

        header_index = None
        matching_columns = []
        for row_index, row in enumerate(rows[:3]):
            normalized_cells = [_normalize(cell) for cell in row]
            columns = [
                index
                for index, cell in enumerate(normalized_cells)
                if any(_normalize(hint) in cell for hint in hints)
            ]
            if columns:
                header_index = row_index
                matching_columns = columns
                break

        if header_index is None:
            continue

        for row_index in range(header_index + 1, len(rows)):
            row = rows[row_index]
            for column_index in matching_columns:
                if column_index >= len(row):
                    continue
                value = re.sub(r"\s+", " ", str(row[column_index] or "")).strip()
                if len(_normalize(value)) < 3 or _looks_like_header_value(value):
                    continue
                facts.append(_fact(
                    value,
                    "source_table_column",
                    confidence,
                    required=(confidence == HIGH),
                    meta={
                        "table_index": table_index,
                        "row_index": row_index,
                        "column_index": column_index,
                        "kind": category,
                    },
                ))
    return facts


def _dedupe_facts(facts):
    deduped = []
    seen = set()
    for fact in facts:
        key = (
            _normalize(fact.get("value")),
            str(fact.get("confidence") or ""),
            str(fact.get("source") or ""),
        )
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    return deduped


def _labeled_table_facts(article, hints, category):
    facts = []
    confidence = _table_confidence(article)

    for table_index, table in enumerate(article.get("source_tables") or []):
        if not isinstance(table, dict):
            continue
        rows = table.get("rows") or []
        for row_index, row in enumerate(rows):
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            cells = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row]
            label = _normalize(cells[0])
            if not any(_normalize(hint) in label for hint in hints):
                continue
            for cell in cells[1:]:
                value = cell.strip()
                if len(_normalize(value)) < 3 or _looks_like_header_value(value):
                    continue
                facts.append(_fact(
                    value,
                    "source_table",
                    confidence,
                    required=(confidence == HIGH),
                    meta={"table_index": table_index, "row_index": row_index, "kind": category},
                ))
    return facts


def _labeled_text_facts(article, hints, category):
    facts = []
    official = bool(article.get("official_source") or article.get("job_official_source"))
    for source_name, text in (
        ("official_pdf", _pdf_text(article)),
        ("official_detail_page", _detail_text(article)),
    ):
        if not text:
            continue
        confidence = HIGH if official else MEDIUM
        for raw_line in re.split(r"[\r\n]+", str(text)):
            line = re.sub(r"\s+", " ", raw_line).strip()
            if len(line) < 5 or len(line) > 260:
                continue
            match = re.match(r"^([^:：]{2,80})\s*[:：]\s*(.{3,180})$", line)
            if not match:
                continue
            label = _normalize(match.group(1))
            if not any(_normalize(hint) in label for hint in hints):
                continue
            value = match.group(2).strip()
            if _looks_like_header_value(value):
                continue
            facts.append(_fact(
                value,
                source_name,
                confidence,
                required=(confidence == HIGH),
                meta={"kind": category, "explicit_label": match.group(1).strip()},
            ))
    return facts


def build_verified_fact_manifest(article):
    article = dict(article or {})
    manifest = {
        "version": 1,
        "policy": {
            "blocking_confidence": [HIGH],
            "non_high_behavior": "warning",
            "raw_table_rows_are_not_blocking": True,
            "heuristic_regex_facts_are_not_blocking": True,
        },
        "facts": {},
        "warnings": [],
    }

    deadline = str(article.get("job_deadline") or "").strip()
    deadline_display = str(article.get("job_deadline_display") or "").strip()
    if deadline:
        source, confidence = _source_for_scalar(article, deadline, DEADLINE_HINTS)
        _append_fact(
            manifest,
            "deadline",
            _fact(
                deadline,
                source,
                confidence,
                required=True,
                aliases=[deadline_display] if deadline_display else [],
            ),
        )

    try:
        positions = int(article.get("job_number_of_positions") or 0)
    except (TypeError, ValueError):
        positions = 0
    if positions > 0:
        source, confidence = _position_source(article, positions)
        _append_fact(
            manifest,
            "positions",
            _fact(positions, source, confidence, required=True),
        )

    scalar_specs = (
        ("salary", article.get("job_salary"), SALARY_HINTS, True),
        ("experience", article.get("job_experience"), EXPERIENCE_HINTS, True),
        ("diploma", article.get("job_diploma"), DIPLOMA_HINTS, True),
        ("exam_date", article.get("job_exam_date"), EXAM_DATE_HINTS, True),
        ("contract_type", article.get("job_contract_type"), (), False),
        ("location", article.get("job_location"), (), False),
        ("reference", article.get("job_external_reference") or article.get("ats_reference"), (), False),
    )
    for category, value, hints, required in scalar_specs:
        if value in (None, "", [], {}):
            continue
        if (
            category == "reference"
            and str(article.get("ats_reference") or "").strip()
            and str(value).strip() == str(article.get("ats_reference") or "").strip()
        ):
            source, confidence = "structured_ats", HIGH
        else:
            source, confidence = _source_for_scalar(article, value, hints)
        aliases = []
        if category == "exam_date" and article.get("job_exam_date_display"):
            aliases.append(article.get("job_exam_date_display"))
        _append_fact(
            manifest,
            category,
            _fact(value, source, confidence, required=required, aliases=aliases),
        )

    notice_type = str(article.get("job_notice_type") or "").strip().lower()
    if notice_type:
        notice_source = str(article.get("job_notice_type_source") or "heuristic").strip().lower()
        confidence = HIGH if notice_source in {"official", "verified", "structured", "ats", "source"} else HEURISTIC
        _append_fact(
            manifest,
            "notice_type",
            _fact(notice_type, notice_source or "heuristic", confidence, required=False),
        )

    application_url = str(article.get("job_application_url") or "").strip()
    if application_url:
        bound = is_application_url_bound_to_job(article, application_url)
        confidence = HIGH if bound else HEURISTIC
        _append_fact(
            manifest,
            "application",
            _fact(
                application_url,
                "verified_application_channel" if bound else "unverified_application_field",
                confidence,
                required=True,
                meta={"kind": str(article.get("job_application_link_kind") or "")},
            ),
        )
        if not bound:
            manifest["warnings"].append("application URL is not verified as belonging to this notice")

    official = bool(article.get("official_source") or article.get("job_official_source"))
    for item in article.get("job_document_links") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        _append_fact(
            manifest,
            "documents",
            _fact(
                url,
                "official_notice_link",
                HIGH if official else MEDIUM,
                required=True,
                meta={
                    "label": str(item.get("label") or ""),
                    "kind": str(item.get("kind") or "document"),
                },
            ),
        )

    explicit_table_specs = (
        ("specialties", SPECIALTY_HINTS, "specialty"),
        ("tests", TEST_HINTS, "test"),
        ("salary", SALARY_HINTS, "salary"),
        ("experience", EXPERIENCE_HINTS, "experience"),
        ("diploma", DIPLOMA_HINTS, "diploma"),
    )
    for manifest_category, hints, kind in explicit_table_specs:
        table_facts = _dedupe_facts(
            _labeled_table_facts(article, hints, kind)
            + _column_table_facts(article, hints, kind)
            + _labeled_text_facts(article, hints, kind)
        )
        existing_values = {
            _normalize(fact.get("value"))
            for fact in (manifest.get("facts") or {}).get(manifest_category) or []
        }
        for fact in table_facts:
            if _normalize(fact.get("value")) in existing_values:
                continue
            _append_fact(manifest, manifest_category, fact)
            existing_values.add(_normalize(fact.get("value")))

    manifest["summary"] = {
        "high": sum(
            1
            for facts in manifest["facts"].values()
            for fact in facts
            if fact.get("confidence") == HIGH
        ),
        "medium": sum(
            1
            for facts in manifest["facts"].values()
            for fact in facts
            if fact.get("confidence") == MEDIUM
        ),
        "heuristic": sum(
            1
            for facts in manifest["facts"].values()
            for fact in facts
            if fact.get("confidence") == HEURISTIC
        ),
    }
    return manifest


def _fact_present(text, category, fact):
    value = fact.get("value")
    aliases = [value] + list(fact.get("aliases") or [])
    normalized = _normalize(text)

    if category in {"application", "documents"}:
        key = canonicalize_url(value) or str(value or "")
        urls = {
            canonicalize_url(match) or match
            for match in re.findall(r"https?://[^\s<>'\"]+", str(text or ""))
        }
        return key in urls or str(value or "") in str(text or "")

    if category == "positions":
        try:
            number = int(value)
        except (TypeError, ValueError):
            return False
        return bool(re.search(
            rf"\b{number}\s*(?:منصب|مناصب|منصبا|poste|postes|position|positions)\b",
            normalized,
            flags=re.I,
        ))

    if category in {"deadline", "exam_date"}:
        target_dates = set()
        for alias in aliases:
            target_dates.update(_canonical_dates(alias))
        if target_dates and target_dates & _canonical_dates(normalized):
            return True

    for alias in aliases:
        alias_norm = _normalize(alias)
        if alias_norm and alias_norm in normalized:
            return True

    if category in {"specialties", "tests"}:
        value_tokens = {
            token for token in re.findall(r"[\w\u0600-\u06ff]+", _normalize(value))
            if len(token) >= 3
        }
        output_tokens = set(re.findall(r"[\w\u0600-\u06ff]+", normalized))
        if value_tokens and len(value_tokens & output_tokens) / len(value_tokens) >= 0.75:
            return True
    return False


def _explicit_sensitive_claims(text):
    normalized = _normalize(text)
    claims = {"positions": set(), "salary": set(), "experience": set(), "deadline": set()}

    for value in re.findall(r"\b(\d{1,4})\s*(?:منصب|مناصب|منصبا|poste|postes|position|positions)\b", normalized):
        claims["positions"].add(str(int(value)))

    for value in re.findall(r"\b(\d[\d\s.,]{1,14})\s*(?:درهم|mad|dh)\b", normalized, flags=re.I):
        claims["salary"].add(re.sub(r"\s+", "", value))

    for value in re.findall(
        r"\b(\d{1,2})\s*(?:سنوات|سنة|عاما|عام|ans?|years?)\s+(?:من\s+)?(?:الخبرة|experience|expérience)\b",
        normalized,
        flags=re.I,
    ):
        claims["experience"].add(str(int(value)))

    for sentence in re.split(r"[\n.!؟؛]+", normalized):
        if not any(_normalize(hint) in sentence for hint in DEADLINE_HINTS):
            continue
        claims["deadline"].update(_canonical_dates(sentence))

    return claims


def _manifest_category_values(manifest, category, confidence=None):
    values = []
    for fact in (manifest.get("facts") or {}).get(category) or []:
        if confidence and fact.get("confidence") != confidence:
            continue
        values.append(fact)
    return values


def validate_output_against_manifest(manifest, seo_title, html_content):
    manifest = dict(manifest or {})
    if not manifest.get("facts"):
        return [], ["Verified Fact Manifest is missing or empty"]

    output = f"{seo_title}\n{html_content}"
    blocking = []
    warnings = list(manifest.get("warnings") or [])

    for category, facts in (manifest.get("facts") or {}).items():
        for fact in facts or []:
            present = _fact_present(output, category, fact)
            if fact.get("required_in_output") and not present:
                message = (
                    f"manifest fact missing from Jobs output: {category}="
                    f"{str(fact.get('value'))[:120]}"
                )
                if fact.get("confidence") == HIGH:
                    blocking.append(message)
                else:
                    warnings.append(message)

    claims = _explicit_sensitive_claims(output)
    for category, values in claims.items():
        high_facts = _manifest_category_values(manifest, category, confidence=HIGH)
        if not high_facts:
            if values:
                warnings.append(
                    f"output contains {category} claim(s) without high-confidence manifest support"
                )
            continue

        if category == "positions":
            allowed = {str(int(fact.get("value"))) for fact in high_facts}
        elif category == "deadline":
            allowed = set()
            for fact in high_facts:
                allowed.update(_canonical_dates(fact.get("value")))
                for alias in fact.get("aliases") or []:
                    allowed.update(_canonical_dates(alias))
        elif category == "salary":
            allowed = set()
            for fact in high_facts:
                value = _normalize(fact.get("value"))
                for number in re.findall(
                    r"\d[\d\s.,]{0,14}(?=\s*(?:درهم|mad|dh)\b)",
                    value,
                    flags=re.I,
                ):
                    compact = re.sub(r"\s+", "", number).strip(".,")
                    if compact:
                        allowed.add(compact.replace(",", "."))
        elif category == "experience":
            allowed = set()
            for fact in high_facts:
                value = _normalize(fact.get("value"))
                matches = re.findall(
                    r"\b(\d{1,2})\s*(?:سنوات|سنة|عاما|عام|ans?|years?)\b",
                    value,
                    flags=re.I,
                )
                allowed.update(str(int(number)) for number in matches)
        else:
            allowed = set()

        normalized_values = {str(value).replace(",", ".") for value in values}
        if allowed and normalized_values and not normalized_values.issubset(allowed):
            blocking.append(
                f"Jobs output contradicts high-confidence manifest {category}"
            )

    # Warnings are advisory/repair hints, never publish blockers.
    deduped_warnings = []
    for warning in warnings:
        warning = str(warning or "").strip()
        if warning and warning not in deduped_warnings:
            deduped_warnings.append(warning)
    return blocking[:8], deduped_warnings[:12]

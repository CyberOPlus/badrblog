import re
from urllib.parse import parse_qsl, urlparse

from config import TRUSTED_SECURITY_SOURCES


BYPASS_MESSAGE = "Trusted source bypass applied"

SECURITY_REPORT_KEYWORDS = (
    "vulnerability summary",
    "weekly report",
    "cybersecurity advisory",
    "security advisory",
    "threat report",
    "cve",
    "kev",
    "zero-day",
    "zero day",
    "data breach",
    "ransomware",
    "exploit",
)

EXPLICIT_MARKETING_PATTERNS = (
    r"\bsponsored\s+(post|article|content|placement|deal|offer)\b",
    r"\bpaid\s+(partnership|promotion|placement|review)\b",
    r"\badvertorial\b",
    r"\baffiliate\s+(link|links|commission|deal|offer|blog|post)\b",
    r"\bpromo\s*code\b",
    r"\bcoupon\s+code\b",
    r"\bbuy\s+now\b",
)

MARKETING_INTENT_PATTERNS = (
    r"\bbest\s+(vpn|hosting|antivirus|password\s+manager|security\s+tool|deal|deals)\b",
    r"\btop\s+\d+\s+(vpn|hosting|antivirus|deals|tools)\b",
    r"\bdeal(s)?\b",
    r"\boffer(s)?\b",
    r"\bpromo(tion)?\b",
    r"\bcoupon(s)?\b",
    r"\bdiscount(s)?\b",
    r"\bsponsored\b",
    r"\baffiliate\b",
    r"\bpartner\b",
    r"\breferral\b",
    r"\bsave\s+\d+%?\b",
    r"\bbuy\s+.+\s+now\b",
)

SELLING_LANGUAGE_PATTERNS = (
    r"\bbuy\b",
    r"\bdiscount(s)?\b",
    r"\bcoupon(s)?\b",
    r"\boffer(s)?\b",
    r"\bdeal(s)?\b",
    r"\bpromo(tion)?\b",
    r"\bsave\s+\d+%?\b",
    r"\b\d+%\s+off\b",
)

AFFILIATE_SIGNAL_PATTERNS = (
    r"\breferral\b",
    r"\baffiliate\b",
    r"\bsponsored\b",
    r"\bpartner(ship)?\b",
)

PRIMARY_SELLING_PATTERNS = (
    r"\bbest\s+(vpn|hosting|antivirus|password\s+manager)\b",
    r"\bbuy\s+.+\s+now\b",
    r"\bdeal(s)?\b",
    r"\boffer(s)?\b",
    r"\bcoupon(s)?\b",
    r"\bdiscount(s)?\b",
    r"\bpromo\s*code\b",
    r"\b\d+%\s+off\b",
)

COMMERCIAL_ROUNDUP_PATTERNS = (
    r"\bbest\s+(vpn|hosting|antivirus|password\s+manager)\b",
    r"\btop\s+\d+\s+(vpn|hosting|antivirus|deals|tools)\b",
)

PROMO_URL_HINTS = (
    "coupon",
    "promo",
    "deal",
    "discount",
    "affiliate",
    "sponsored",
    "partner",
)

AFFILIATE_QUERY_HINTS = (
    "tag",
    "aff",
    "affiliate",
    "ref",
    "partner",
    "irclickid",
    "campid",
    "ascsubtag",
)


def _article_text(article):
    return " ".join(
        str(article.get(field) or "")
        for field in (
            "title",
            "fetched_title",
            "seo_title",
            "rss_summary",
            "meta_description",
            "content_preview",
            "summary",
            "content",
            "final_html",
        )
    )


def _host_from_value(value):
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    if "@" in host:
        host = host.rsplit("@", 1)[-1]
    return host.split(":", 1)[0].casefold().lstrip("www.")


def _article_hosts(article):
    hosts = set()
    for field in ("url", "source_url", "original_url", "canonical_url", "source_domain", "domain"):
        host = _host_from_value(article.get(field))
        if host:
            hosts.add(host)
    return hosts


def _is_trusted_security_source(article):
    trusted_hosts = tuple(source.casefold().lstrip("www.") for source in TRUSTED_SECURITY_SOURCES)
    for host in _article_hosts(article):
        if any(host == trusted or host.endswith(f".{trusted}") for trusted in trusted_hosts):
            return True
    return False


def _has_pattern(patterns, text):
    return any(re.search(pattern, text, flags=re.I) for pattern in patterns)


def _has_security_report_keyword(article):
    text = _article_text(article).casefold()
    return any(keyword in text for keyword in SECURITY_REPORT_KEYWORDS)


def _affiliate_query_detected(parsed_url):
    query_names = {key.casefold() for key, _value in parse_qsl(parsed_url.query, keep_blank_values=True)}
    return any(any(hint in key for hint in AFFILIATE_QUERY_HINTS) for key in query_names)


def _mark_bypass(article):
    if isinstance(article, dict):
        article["content_filter_bypass_message"] = BYPASS_MESSAGE


def is_promotional_article(article):
    article = article or {}
    url = str(article.get("url") or article.get("source_url") or article.get("original_url") or "")
    text = _article_text(article)
    normalized_text = text.casefold()

    if _is_trusted_security_source(article):
        _mark_bypass(article)
        return False, BYPASS_MESSAGE

    if _has_security_report_keyword(article) and not _has_pattern(EXPLICIT_MARKETING_PATTERNS, text):
        return False, ""

    parsed = urlparse(url)
    path = parsed.path.casefold()
    promo_path = any(hint in path for hint in PROMO_URL_HINTS)
    affiliate_query = _affiliate_query_detected(parsed)

    marketing_intent = _has_pattern(MARKETING_INTENT_PATTERNS, normalized_text) or promo_path or affiliate_query
    selling_language = _has_pattern(SELLING_LANGUAGE_PATTERNS, normalized_text) or promo_path
    affiliate_signal = (
        _has_pattern(AFFILIATE_SIGNAL_PATTERNS, normalized_text)
        or _has_pattern(COMMERCIAL_ROUNDUP_PATTERNS, normalized_text)
        or affiliate_query
        or "affiliate" in path
    )
    primary_goal_selling = _has_pattern(PRIMARY_SELLING_PATTERNS, normalized_text) or promo_path

    if marketing_intent and selling_language and affiliate_signal and primary_goal_selling:
        return True, "ad/affiliate/sponsored title or summary"

    direct_sales_cta = _has_pattern((r"\bbuy\s+.+\s+now\b",), normalized_text)
    if direct_sales_cta and marketing_intent and selling_language and primary_goal_selling:
        return True, "ad/affiliate/sponsored title or summary"

    return False, ""

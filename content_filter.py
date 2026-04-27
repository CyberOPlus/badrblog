import re
from urllib.parse import parse_qsl, urlparse


PROMO_TITLE_PATTERNS = (
    r"\bsponsored\b",
    r"\badvertorial\b",
    r"\baffiliate\b",
    r"\bpromo\s*code\b",
    r"\bcoupon\b",
    r"\bdiscount\b",
    r"\bdaily\s+deal(s)?\b",
    r"\bdeal\s+of\s+the\s+day\b",
    r"\bbuy\s+now\b",
    r"\bbest\s+vpn\b",
    r"\bbest\s+hosting\b",
    r"\bblack\s+friday\b",
)

PROMO_SUMMARY_HARD_PATTERNS = (
    r"\bsponsored\s+(post|article|content|placement)\b",
    r"\bpaid\s+(partnership|promotion|placement)\b",
    r"\badvertorial\b",
    r"\baffiliate\s+(link|links|commission|deal|offer)\b",
    r"\bpromo\s*code\b",
    r"\bcoupon\s+code\b",
    r"\bbuy\s+now\b",
    r"\bbest\s+vpn\b",
    r"\bbest\s+hosting\b",
)

PROMO_SUMMARY_SOFT_PATTERNS = (
    r"\baffiliate\b",
    r"\bcoupon\b",
    r"\bdiscount\b",
    r"\bdeal(s)?\b",
    r"\bpromo\b",
    r"\bcommission\b",
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


def is_promotional_article(article):
    title = str(article.get("title") or article.get("fetched_title") or "").casefold()
    url = str(article.get("url") or "").casefold()
    summary = str(article.get("rss_summary") or article.get("meta_description") or article.get("content_preview") or "").casefold()

    if any(re.search(pattern, title, flags=re.I) for pattern in PROMO_TITLE_PATTERNS):
        return True, "ad/affiliate/sponsored title or summary"

    if any(re.search(pattern, summary, flags=re.I) for pattern in PROMO_SUMMARY_HARD_PATTERNS):
        return True, "ad/affiliate/sponsored title or summary"

    soft_matches = sum(1 for pattern in PROMO_SUMMARY_SOFT_PATTERNS if re.search(pattern, summary, flags=re.I))
    if soft_matches >= 2:
        return True, "ad/affiliate/sponsored title or summary"

    parsed = urlparse(url)
    path = parsed.path.casefold()
    if any(hint in path for hint in PROMO_URL_HINTS):
        return True, "promotional URL path"

    query_names = {key.casefold() for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if any(any(hint in key for hint in AFFILIATE_QUERY_HINTS) for key in query_names):
        return True, "affiliate tracking URL"

    return False, ""

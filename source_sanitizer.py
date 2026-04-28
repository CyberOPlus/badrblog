from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from production_logging import log_event


RELATED_HINTS = (
    "related",
    "recommend",
    "recommended",
    "more-stories",
    "more_posts",
    "also-read",
    "read-more",
    "pRelate",
)

RELATED_TEXT_HINTS = (
    "قد يهمك",
    "اقرأ ايضا",
    "اقرأ أيضًا",
    "مواضيع ذات صلة",
    "مقالات ذات صلة",
    "related posts",
    "you may also like",
    "read also",
    "more from",
)


def _normalize_host(value):
    parsed = urlparse(str(value or "").strip())
    host = parsed.netloc or parsed.path
    return host.lower().removeprefix("www.").split("/")[0]


def _same_domain(url, source_domain):
    host = _normalize_host(urlparse(url).netloc)
    source_host = _normalize_host(source_domain)
    return bool(source_host and (host == source_host or host.endswith("." + source_host)))


def _is_related_block(tag):
    if not tag:
        return False
    attrs = " ".join(
        str(value)
        for value in (
            tag.get("class", []),
            tag.get("id", ""),
            tag.get("role", ""),
            tag.get("aria-label", ""),
        )
    ).lower()
    text = tag.get_text(" ", strip=True).lower()
    return any(hint.lower() in attrs for hint in RELATED_HINTS) or any(
        hint.lower() in text for hint in RELATED_TEXT_HINTS
    )


def _related_ancestor(link):
    for parent in link.parents:
        if getattr(parent, "name", None) in {"p", "div", "section", "aside", "ul", "ol", "li"} and _is_related_block(parent):
            return parent
    return None


def sanitize_source_links(html, source_domain):
    """
    Remove links that point back to the scraped source domain.

    Related/recommended blocks are removed as a whole. Ordinary source-domain
    links are unwrapped so the visible text remains without a clickable href.
    Returns ``(sanitized_html, removed_source_links_count)``.
    """
    if not html or not source_domain:
        return html or "", 0

    soup = BeautifulSoup(html, "html.parser")
    removed_count = 0

    for link in list(soup.select("a[href]")):
        href = (link.get("href") or "").strip()
        if href.startswith(("#", "mailto:", "tel:")):
            continue
        absolute_href = urljoin(f"https://{_normalize_host(source_domain)}/", href)
        if not _same_domain(absolute_href, source_domain):
            continue

        related = _related_ancestor(link)
        if related:
            removed_count += len(
                [
                    related_link
                    for related_link in related.select("a[href]")
                    if _same_domain(
                        urljoin(f"https://{_normalize_host(source_domain)}/", related_link.get("href", "")),
                        source_domain,
                    )
                ]
            )
            related.decompose()
        else:
            removed_count += 1
            link.unwrap()

    log_event(
        "source_links_sanitized",
        source_domain=_normalize_host(source_domain),
        removed_source_links_count=removed_count,
    )
    return str(soup), removed_count

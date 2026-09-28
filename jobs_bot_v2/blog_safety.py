from __future__ import annotations

from urllib.parse import urlparse

from .config import EXPECTED_BLOG_HOST, TEST_BLOG_URL


def expected_host():
    explicit = str(EXPECTED_BLOG_HOST or "").strip().casefold()
    if explicit:
        return explicit
    return urlparse(TEST_BLOG_URL).netloc.casefold()


def validate_blogger_result_url(url):
    """Hard stop if a test run unexpectedly publishes to another Blogger host."""
    parsed = urlparse(str(url or "").strip())
    actual = parsed.netloc.casefold()
    expected = expected_host()
    if not parsed.scheme.startswith("http") or not actual:
        raise RuntimeError("Blogger did not return a valid public URL")
    if expected and actual != expected:
        raise RuntimeError(
            f"Safety stop: Blogger returned host {actual!r}, expected {expected!r}"
        )
    return str(url).strip()

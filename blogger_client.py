# ============================================================
# blogger_client.py - Blogger API Publishing Module
# ============================================================

import json
import re
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    BLOGGER_CLIENT_ID,
    BLOGGER_CLIENT_SECRET,
    BLOG_ID,
    CREDENTIALS_FILE,
    LOCAL_PUBLISH_FALLBACK,
    LOCAL_PUBLISH_DIR,
    MAX_RETRIES,
    PUBLISH_MODE,
    PUBLISH_DELAY_SECONDS,
    RETRY_DELAY,
    SAFE_MODE,
    SCOPES,
    TOKEN_FILE,
)
from production_logging import html_word_count, log_event
from quality_gate import validate_before_publish

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


SIMILARITY_STOPWORDS = {
    "this",
    "that",
    "with",
    "from",
    "into",
    "about",
    "your",
    "their",
    "have",
    "more",
    "than",
    "will",
    "using",
    "used",
    "what",
    "when",
    "where",
    "which",
    "into",
    "على",
    "من",
    "في",
    "إلى",
    "الى",
    "هذا",
    "هذه",
    "ذلك",
    "تلك",
    "بعد",
    "قبل",
    "مع",
    "عن",
    "عبر",
    "لدى",
    "لها",
    "لهذا",
    "يمكن",
    "تكون",
    "يكون",
    "أداة",
    "اداة",
    "مفتوح",
    "المصدر",
    "المفتوح",
    "الرسمي",
    "رابط",
    "روابط",
    "مقال",
    "مقالات",
}


class LocalFilePublisher:
    """
    Fallback publisher that saves finished posts to local HTML/JSON files.
    """

    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._pending_blog_id = ""
        self._pending_body = {}
        self._pending_is_draft = False

    def posts(self):
        return self

    def insert(self, blogId, body, isDraft=False):
        self._pending_blog_id = blogId
        self._pending_body = body
        self._pending_is_draft = isDraft
        return self

    def execute(self):
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        slug = _slugify(
            self._pending_body.get("slug") or self._pending_body.get("title", "post")
        )
        base_name = f"{timestamp}-{slug}"

        metadata = {
            "id": f"local-{base_name}",
            "blog_id": self._pending_blog_id,
            "title": self._pending_body.get("title", "Untitled Post"),
            "seo_title": self._pending_body.get("seo_title", ""),
            "meta_description": self._pending_body.get("customMetaData", ""),
            "slug": self._pending_body.get("slug", ""),
            "labels": self._pending_body.get("labels", []),
            "is_draft": self._pending_is_draft,
            "saved_at": datetime.now().isoformat(),
        }

        written = _write_post_archive(
            base_name=base_name,
            title=metadata["title"],
            labels=metadata["labels"],
            content=self._pending_body.get("content", ""),
            metadata=metadata,
            output_dir=self.output_dir,
        )

        return {
            "id": written["id"],
            "url": str(Path(written["html_file"]).resolve()),
            "title": written["title"],
        }


def _slugify(value):
    cleaned = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE).strip().lower()
    cleaned = re.sub(r"[-\s]+", "-", cleaned, flags=re.UNICODE)
    return cleaned[:80] or "post"


def _normalize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())


def _safe_positive_int(value):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _build_local_post_html(title, labels, content, meta_description=""):
    safe_title = escape(title)
    safe_description = escape(meta_description or "")
    labels_html = ""

    if labels:
        safe_labels = ", ".join(escape(label) for label in labels)
        labels_html = f"<p><strong>Labels:</strong> {safe_labels}</p>"

    return f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="{safe_description}">
  <title>{safe_title}</title>
</head>
<body>
  <main>
    <h1>{safe_title}</h1>
    <p>{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
    {labels_html}
    {content}
  </main>
</body>
</html>
"""


def _write_post_archive(base_name, title, labels, content, metadata, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    html_path = output_dir / f"{base_name}.html"
    json_path = output_dir / f"{base_name}.json"

    metadata = dict(metadata)
    metadata["html_file"] = str(html_path.resolve())

    html_content = _build_local_post_html(
        title=title,
        labels=labels,
        content=content,
        meta_description=metadata.get("meta_description", ""),
    )
    html_path.write_text(html_content, encoding="utf-8")
    json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def _archive_live_post(article, post_id, post_url, final_content):
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    base_name = f"{timestamp}-{_slugify(article.get('slug') or article.get('title', 'post'))}"
    metadata = {
        "id": post_id,
        "blog_id": BLOG_ID,
        "title": article.get("title", "Untitled Post"),
        "seo_title": article.get("seo_title", ""),
        "meta_description": article.get("meta_description", ""),
        "slug": article.get("slug", ""),
        "labels": article.get("labels", []),
        "blogger_post_id": post_id,
        "blogger_url": post_url,
        "original_url": article.get("original_url", ""),
        "original_title": article.get("original_title", ""),
        "trusted_sources": article.get("trusted_sources", []),
        "image": article.get("image"),
        "is_draft": False,
        "saved_at": datetime.now().isoformat(),
        "published_to_blogger_at": datetime.now().isoformat(),
    }
    return _write_post_archive(
        base_name=base_name,
        title=metadata["title"],
        labels=metadata["labels"],
        content=final_content,
        metadata=metadata,
        output_dir=LOCAL_PUBLISH_DIR,
    )


def _strip_legacy_source_blocks(content):
    soup = BeautifulSoup(content or "", "html.parser")

    for p_tag in soup.select("p.pRef"):
        p_tag.decompose()

    for alert_tag in soup.select("div.alert"):
        alert_text = alert_tag.get_text(" ", strip=True)
        if "المصدر الأصلي" in alert_text or "Original source" in alert_text or "Source:" in alert_text:
            alert_tag.decompose()

    return str(soup)


def _build_image_html(image, fallback_alt):
    if not image or not image.get("url"):
        return ""

    attrs = [
        "class='full'",
        f"alt='{escape(image.get('alt') or fallback_alt, quote=True)}'",
        f"src='{escape(image['url'], quote=True)}'",
    ]

    width = _safe_positive_int(image.get("width"))
    height = _safe_positive_int(image.get("height"))
    if width:
        attrs.insert(2, f"width='{width}'")
    if height:
        attrs.insert(3, f"height='{height}'")

    return "<!--[ Standard image ]-->\n<img " + " ".join(attrs) + "/>"


def _insert_image_after_first_paragraph(content, image_html):
    if not image_html or re.search(r"<img\b", content or "", flags=re.IGNORECASE):
        return content

    soup = BeautifulSoup(content or "", "html.parser")
    first_paragraph = None

    for paragraph in soup.find_all("p"):
        if paragraph.find_parent(["blockquote", "figcaption", "summary"]):
            continue
        if _normalize_text(paragraph.get_text(" ", strip=True)):
            first_paragraph = paragraph
            break

    if not first_paragraph:
        return image_html + ("\n" + content if content else "")

    fragment = BeautifulSoup(image_html, "html.parser")
    nodes = [node.extract() for node in fragment.contents]
    for node in reversed(nodes):
        first_paragraph.insert_after(node)

    return str(soup)


def _trusted_source_label(source):
    kind = source.get("kind", "")
    title = _normalize_text(source.get("title", ""))
    host = urlparse(source.get("url", "")).netloc.lower()

    if kind == "github":
        return "المستودع الرسمي على GitHub"
    if kind == "gitlab":
        return "المستودع الرسمي على GitLab"
    if kind == "docs":
        return "الوثائق الرسمية"
    if kind == "cisa":
        return "تنبيه CISA الرسمي"
    if kind == "cve":
        return "مرجع CVE الرسمي"
    if kind == "nvd":
        return "مرجع NVD الرسمي"
    if kind == "mitre":
        return "مرجع MITRE الرسمي"
    if kind == "report":
        return title or "التقرير الرسمي"
    if kind == "site":
        return "الموقع الرسمي" if not title or "." in title else title

    if title:
        return title
    return host or "رابط موثوق"


def _build_trusted_sources_html(article):
    sources = article.get("trusted_sources") or []
    if not sources:
        return ""

    items = []
    seen_urls = set()

    for source in sources:
        href = source.get("url", "").strip()
        if not href or href in seen_urls:
            continue
        seen_urls.add(href)
        label = _trusted_source_label(source)
        items.append(
            "    <li>"
            f"<a class='extL' href='{escape(href, quote=True)}' rel='nofollow noreferrer noopener' target='_blank'>"
            f"{escape(label)}</a></li>"
        )

    if not items:
        return ""

    return (
        "\n<div class='pRelate'>\n"
        "  <b>روابط موثوقة ومفيدة:</b>\n"
        "  <ul>\n"
        + "\n".join(items)
        + "\n  </ul>\n</div>"
    )


def _tokenize_similarity(text):
    normalized = re.sub(r"[^\w\u0600-\u06FF]+", " ", (text or "").lower())
    return {
        token
        for token in normalized.split()
        if len(token) > 2 and token not in SIMILARITY_STOPWORDS
    }


def _load_internal_link_candidates():
    candidates = []
    seen_urls = set()

    if not Path(LOCAL_PUBLISH_DIR).exists():
        return candidates

    for json_path in sorted(Path(LOCAL_PUBLISH_DIR).glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        url = data.get("blogger_url") or data.get("url")
        title = _normalize_text(data.get("title", ""))
        if not url or not str(url).startswith("http") or not title or url in seen_urls:
            continue

        seen_urls.add(url)
        candidates.append(
            {
                "title": title,
                "url": url,
                "labels": data.get("labels", []),
                "original_url": data.get("original_url", ""),
            }
        )

    return candidates


def _score_internal_candidate(article, candidate):
    if candidate.get("url") == article.get("blogger_url"):
        return -1
    if candidate.get("original_url") and candidate.get("original_url") == article.get("original_url"):
        return -1
    if candidate.get("title") == article.get("title"):
        return -1

    current_label_set = {_normalize_text(label).lower() for label in article.get("labels", []) if label}
    candidate_label_set = {_normalize_text(label).lower() for label in candidate.get("labels", []) if label}

    current_title_tokens = _tokenize_similarity(article.get("title", ""))
    candidate_title_tokens = _tokenize_similarity(candidate.get("title", ""))
    current_body_tokens = _tokenize_similarity(re.sub(r"<[^>]+>", " ", article.get("content", "")))

    score = 0
    score += len(current_label_set & candidate_label_set) * 6
    score += len(current_title_tokens & candidate_title_tokens) * 3
    score += min(2, len(current_body_tokens & candidate_title_tokens)) * 2

    candidate_title_lower = candidate.get("title", "").lower()
    if candidate_title_lower and candidate_title_lower in article.get("content", "").lower():
        score += 4

    return score


def _select_internal_related_posts(article, candidates, limit=2):
    ranked = []
    for candidate in candidates or []:
        score = _score_internal_candidate(article, candidate)
        if score >= 3:
            ranked.append((score, candidate))

    ranked.sort(key=lambda item: (-item[0], item[1]["title"].lower()))
    return [candidate for _, candidate in ranked[:limit]]


def _build_internal_links_html(article, related_candidates):
    related_posts = _select_internal_related_posts(article, related_candidates)
    if not related_posts:
        return ""

    links = [
        f"<a href='{escape(post['url'], quote=True)}'>{escape(post['title'])}</a>"
        for post in related_posts
    ]

    if len(links) == 1:
        sentence = f"للتوسع اكثر في هذا المسار، قد يفيدك ايضا الاطلاع على {links[0]}."
    else:
        sentence = f"للتوسع اكثر في هذا المسار، قد يفيدك ايضا الاطلاع على {links[0]} و {links[1]}."

    return f"\n<p>{sentence}</p>"


def _finalize_article_content(article, related_candidates=None):
    content = _strip_legacy_source_blocks(article.get("content", "").strip())
    content = _insert_image_after_first_paragraph(
        content,
        _build_image_html(article.get("image"), article.get("title", "")),
    )

    internal_links_html = _build_internal_links_html(article, related_candidates or [])
    trusted_sources_html = _build_trusted_sources_html(article)

    if internal_links_html:
        content = content.rstrip() + "\n" + internal_links_html
    if trusted_sources_html:
        content = content.rstrip() + "\n" + trusted_sources_html

    return content.strip()


def is_local_publisher(service):
    return isinstance(service, LocalFilePublisher)


def get_publish_target_name(service):
    return "local files" if is_local_publisher(service) else "Blogger"


def _save_credentials(creds):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")


def get_credentials():
    """
    Handle Blogger OAuth when credentials are available.
    Returns None when local publishing should be used instead.
    """
    creds = None
    has_env_oauth = bool(BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET)
    has_blogger_auth = CREDENTIALS_FILE.exists() or has_env_oauth

    if TOKEN_FILE.exists():
        print("Found saved login tokens. Loading...")
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
            if creds and creds.expired and creds.refresh_token:
                print("  Token expired. Refreshing...")
                creds.refresh(Request())
                _save_credentials(creds)
                print("  Token refreshed successfully!")
            elif creds:
                print("  Token is still valid!")
        except Exception as e:
            print(f"  Error loading saved token: {e}")
            creds = None

    if creds and creds.valid:
        return creds

    if not has_blogger_auth:
        if LOCAL_PUBLISH_FALLBACK:
            print("\nBlogger credentials not configured. Using local file publisher.")
            return None
        print("\nBlogger credentials are missing and local fallback is disabled.")
        return None

    print("\nFirst-time setup: opening browser for Google login...")

    try:
        if CREDENTIALS_FILE.exists():
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        else:
            flow = InstalledAppFlow.from_client_config(
                {
                    "installed": {
                        "client_id": BLOGGER_CLIENT_ID,
                        "client_secret": BLOGGER_CLIENT_SECRET or "",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"],
                    }
                },
                SCOPES,
            )

        creds = flow.run_local_server(
            port=0,
            prompt="consent",
            authorization_prompt_message="",
        )
        _save_credentials(creds)
        print("  Authentication successful!")
        print("  Login tokens saved.")
        return creds

    except Exception as e:
        error_text = str(e)
        print(f"  Authentication failed: {error_text}")
        normalized_error = error_text.lower()
        if (
            "deleted_client" in normalized_error
            or "client_secret is missing" in normalized_error
            or "invalid_client" in normalized_error
        ):
            print("  The configured Blogger OAuth client is no longer usable.")
            print("  Add a fresh client_secret.json file or set new BLOGGER_CLIENT_ID and BLOGGER_CLIENT_SECRET values in .env.")
        if LOCAL_PUBLISH_FALLBACK:
            print("  Falling back to local file publishing for this run.")
            return None
        return None


def create_blogger_service(creds):
    """
    Create the Blogger API service or the local fallback publisher.
    """
    if not creds:
        if not LOCAL_PUBLISH_FALLBACK:
            return None
        print("Local publisher ready!")
        return LocalFilePublisher(LOCAL_PUBLISH_DIR)

    print("Connecting to Blogger API...")
    service = build("blogger", "v3", credentials=creds)
    print("Blogger API connected!")
    return service


def _publish_quality_error(article, html_content):
    gate_article = dict(article)
    gate_article["final_html"] = html_content
    gate_article["seo_title"] = article.get("seo_title") or article.get("title", "")
    gate_article["seo_description"] = article.get("meta_description") or article.get("seo_description", "")
    result = validate_before_publish(gate_article, check_duplicate=False)
    if not result.passed:
        return result.reason
    return ""


def _is_public_post_url(url):
    parsed = urlparse(str(url or "").strip())
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.netloc
        and parsed.path.strip("/")
    )


def publish_post(service, article, related_candidates=None):
    """
    Publish one translated article through Blogger or the local fallback.
    """
    print(f'\nPublishing: "{article["title"][:60]}..."')

    final_content = _finalize_article_content(article, related_candidates=related_candidates)
    quality_error = _publish_quality_error(article, final_content)
    if quality_error:
        log_event(
            "blogger_publish_blocked",
            title=article.get("title"),
            original_url=article.get("original_url"),
            reason=quality_error,
        )
        print(f"  Article was not published: {quality_error}")
        return None

    post_body = {
        "kind": "blogger#post",
        "title": article["title"],
        "content": final_content,
        "labels": article["labels"],
    }
    publish_live = (not SAFE_MODE) and PUBLISH_MODE == "live"
    if article.get("meta_description"):
        post_body["customMetaData"] = article["meta_description"]
    if is_local_publisher(service) and article.get("seo_title"):
        post_body["seo_title"] = article["seo_title"]
    if is_local_publisher(service) and article.get("slug"):
        post_body["slug"] = article["slug"]

    for attempt in range(MAX_RETRIES + 1):
        try:
            post = (
                service.posts()
                .insert(blogId=BLOG_ID, body=post_body, isDraft=not publish_live)
                .execute()
            )

            post_url = post.get("url", "URL not available")
            post_id = post.get("id", "unknown")
            if publish_live and not is_local_publisher(service) and not _is_public_post_url(post_url):
                log_event(
                    "blogger_publish_result",
                    status="failed",
                    post_id=post_id,
                    url=post_url,
                    error="missing public post permalink",
                    title=article.get("title"),
                )
                print("  Article was not marked published: Blogger did not return a post permalink.")
                return None

            if publish_live and not is_local_publisher(service) and str(post_url).startswith("http"):
                _archive_live_post(article, post_id, post_url, final_content)

            print("  Published successfully!")
            print(f"  URL: {post_url}")
            log_event(
                "blogger_publish_result",
                status="success",
                post_id=post_id,
                url=post_url,
                words=html_word_count(final_content),
                title=article.get("title"),
            )
            return {
                "id": post_id,
                "url": post_url,
                "title": article["title"],
            }

        except HttpError as e:
            status_code = e.resp.status
            error_message = e._get_reason().strip()
            print(f"  API Error ({status_code}): {error_message}")
            log_event(
                "blogger_publish_result",
                status="api_error",
                http_status=status_code,
                error=error_message,
                title=article.get("title"),
            )

            if status_code == 429:
                wait_time = RETRY_DELAY * 3
            elif status_code in (401, 403):
                return None
            else:
                wait_time = RETRY_DELAY * (attempt + 1)

            if attempt < MAX_RETRIES:
                print(f"  Retrying in {wait_time} seconds... (Attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait_time)
            else:
                return None

        except Exception as e:
            print(f"  Unexpected error: {e}")
            log_event(
                "blogger_publish_result",
                status="error",
                error=e,
                title=article.get("title"),
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                return None

    return None


def publish_all_articles(service, articles, delay_seconds=PUBLISH_DELAY_SECONDS):
    """
    Publish a list of translated articles.
    """
    print("\n" + "=" * 60)
    print(f"STEP 3: Publishing articles to {get_publish_target_name(service)}")
    print("=" * 60)

    published = []
    related_candidates = _load_internal_link_candidates()

    for i, article in enumerate(articles, 1):
        print(f"\n--- Publishing article {i}/{len(articles)} ---")
        result = publish_post(service, article, related_candidates=related_candidates)

        if result:
            published.append(result)
            if str(result.get("url", "")).startswith("http"):
                related_candidates.append(
                    {
                        "title": article["title"],
                        "url": result["url"],
                        "labels": article.get("labels", []),
                        "original_url": article.get("original_url", ""),
                    }
                )
        else:
            print("  Article was not published")

        if i < len(articles) and delay_seconds > 0:
            print(f"  Waiting {delay_seconds} seconds before next post...")
            time.sleep(delay_seconds)

    print(f"\nSuccessfully finished {len(published)}/{len(articles)} article(s)!")
    return published

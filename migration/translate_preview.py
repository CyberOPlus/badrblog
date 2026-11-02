#!/usr/bin/env python3
"""Translate one Blogger post and one page for review; never write to Blogger.

Only visible Arabic text nodes, selected accessibility attributes and titles
are translated. Original HTML tags, links, embedded code and assets are kept.
If strict validation fails, no preview is accepted. No live updates are made.
"""
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import requests

ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
PROTECTED = {"script", "style", "code", "pre", "kbd", "samp", "textarea", "noscript", "svg", "math", "iframe"}
ATTRS = {"alt", "title", "placeholder", "aria-label"}
ATTR_PATTERN = re.compile(r"(?<![\w:-])(?P<attr>alt|title|placeholder|aria-label)\s*=\s*(?P<quote>['\"])(?P<value>.*?)(?P=quote)", re.I | re.S)
NUMBERS = re.compile(r"[0-9]+(?:[.,][0-9]+)*")
URLS = re.compile(r"https?://[^\s<>\"']+")


class TextSpans(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=False)
        self.source = source
        self.starts = [0]
        for m in re.finditer("\n", source):
            self.starts.append(m.end())
        self.depth = 0
        self.spans = []

    def pos(self):
        line, column = self.getpos()
        return self.starts[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag in PROTECTED:
            self.depth += 1
            return
        if self.depth:
            return
        raw = self.get_starttag_text()
        base = self.pos()
        for m in ATTR_PATTERN.finditer(raw):
            if m.group("attr").lower() not in ATTRS:
                continue
            text = m.group("value")
            if ARABIC.search(text):
                self.spans.append((base + m.start("value"), base + m.end("value"), text, "attr"))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag in PROTECTED:
            self.depth -= 1

    def handle_endtag(self, tag):
        if tag in PROTECTED and self.depth:
            self.depth -= 1

    def handle_data(self, data):
        if self.depth or not ARABIC.search(data):
            return
        a = self.pos()
        if self.source[a:a + len(data)] != data:
            raise RuntimeError("Parser/source position mismatch; refusing translation")
        self.spans.append((a, a + len(data), data, "text"))


def collect_spans(html_source):
    parser = TextSpans(html_source)
    parser.feed(html_source)
    parser.close()
    spans = sorted(parser.spans)
    for x, y in zip(spans, spans[1:]):
        if x[1] > y[0]:
            raise RuntimeError("Overlapping HTML span positions")
    return spans


def safe_translation(source, translated):
    if not isinstance(translated, str) or not translated.strip():
        raise RuntimeError("Missing or empty translation")
    if ARABIC.search(translated):
        raise RuntimeError("Translation still contains Arabic text")
    if sorted(NUMBERS.findall(source)) != sorted(NUMBERS.findall(translated)):
        raise RuntimeError("Translation modified numerical values")
    if sorted(URLS.findall(source)) != sorted(URLS.findall(translated)):
        raise RuntimeError("Translation modified URL(s)")
    if re.search(r"<\/?[A-Za-z][^>]*>", translated):
        raise RuntimeError("Model returned HTML in plain-text output")
    return translated


def translate_batch(api_key, model, context, segments):
    query = {
        "task": "Translate the following Arabic text into natural, precise English.",
        "strict_rules": [
            "This is translation, not rewriting, summarizing, fact-checking or adding information.",
            "Preserve every fact, version number, CVE, date, command, acronym, proper name, and URL exactly.",
            "Never add, remove, correct, update or speculate about any fact.",
            "Do not output HTML, Markdown wrappers or explanatory commentary.",
            "Translate into English even for short labels; keep English technical expressions.",
            "Return JSON with one string per original segment ID. Do not merge segments.",
        ],
        "article_context_for_terminology_only": context[:1200],
        "segments": [{"id": sid, "text": txt} for sid, txt in segments],
    }
    import time
    payload = {
        "contents": [{"role": "user", "parts": [{"text": json.dumps(query, ensure_ascii=False)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0},
    }
    candidates = [model]
    if model == "gemini-3.8-flash":
        candidates.append("gemini-3.6-flash")
    response = None
    for chosen_model in candidates:
        for attempt in range(3):
            try:
                response = requests.post(
                    "https://generativelanguage.googleapis.com/v1beta/models/" + chosen_model + ":generateContent",
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                    json=payload,
                    timeout=120,
                )
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("Translation API unreachable (no Blogger write)")
                time.sleep([2, 5, 10][attempt])
                continue
            if response.status_code == 200:
                break
            if response.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError("Translation provider HTTP {} (no Blogger write)".format(response.status_code))
            print("RETRY: translation provider temporarily unavailable; model={}, attempt={}".format(chosen_model, attempt + 1))
            if attempt < 2:
                time.sleep([2, 5, 10][attempt])
        if response is not None and response.status_code == 200:
            break
    if response is None or response.status_code != 200:
        raise RuntimeError("All translation API retries failed (no Blogger write)")
    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Translation provider returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(p.get("text", "") for p in parts)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("Translation provider returned invalid JSON") from e
    items = decoded.get("items") if isinstance(decoded, dict) else decoded
    if not isinstance(items, list):
        raise RuntimeError("Expected JSON list of translation items")
    translated = {}
    for item in items:
        if not isinstance(item, dict):
            raise RuntimeError("Invalid translation record")
        sid, value = item.get("id"), item.get("translation")
        if not isinstance(sid, str) or sid in translated:
            raise RuntimeError("Bad/duplicate translation ID")
        translated[sid] = value
    expected = dict(segments)
    if set(translated) != set(expected):
        raise RuntimeError("Output segments do not match inputs")
    for sid in expected:
        translated[sid] = safe_translation(expected[sid], translated[sid])
    return translated


def translate_document(key, model, title, content):
    spans = collect_spans(content)
    segments = []
    if ARABIC.search(title):
        segments.append(("__title__", title))
    for i, (_, _, raw, _) in enumerate(spans):
        segments.append(("part{:04d}".format(i), raw))
    translated = {}
    for start in range(0, len(segments), 12):
        chunk = segments[start:start + 12]
        translated.update(translate_batch(key, model, title, chunk))
    new_title = translated.get("__title__", title)
    new_html = content
    for i in reversed(range(len(spans))):
        begin, end, original, typ = spans[i]
        output = translated["part{:04d}".format(i)]
        if typ == "text":
            output = html.escape(output, quote=False)
        else:
            output = html.escape(output, quote=True)
        new_html = new_html[:begin] + output + new_html[end:]
    if len(collect_spans(new_html)) != 0:
        raise RuntimeError("Some Arabic visible text remained after translation")
    # All HTML tags must remain in the same order, with the same attributes.
    def tag_shape(markup):
        class Shape(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.events = []
            def handle_starttag(self, tag, attrs):
                self.events.append(("+", tag, tuple(k for k, v in attrs)))
            def handle_startendtag(self, tag, attrs):
                self.events.append(("*", tag, tuple(k for k, v in attrs)))
            def handle_endtag(self, tag):
                self.events.append(("-", tag))
        p = Shape()
        p.feed(markup)
        p.close()
        return p.events
    if tag_shape(content) != tag_shape(new_html):
        raise RuntimeError("HTML element sequence changed")
    return new_title, new_html, len(spans)


def main():
    p = Path("migration-output/blogger-originals.json")
    originals = json.loads(p.read_text(encoding="utf-8"))
    if originals.get("domain") != "cyberoplus.com" or len(originals.get("posts", [])) != 259 or len(originals.get("pages", [])) != 6:
        raise RuntimeError("Unexpected source inventory")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    model = os.environ.get("MIGRATION_GEMINI_MODEL", "gemini-3.8-flash")
    if not re.fullmatch(r"gemini-[A-Za-z0-9.\-]+", model):
        raise RuntimeError("Invalid model identifier")
    examples = {}
    for typ in ("posts", "pages"):
        eligible = [x for x in originals[typ] if ARABIC.search(str(x.get("title", "")) + str(x.get("content", "")))]
        # Select a representative but bounded example to avoid excessive unreviewed inference API cost.
        suitable = [x for x in eligible if len(x.get("content", "")) < 5500]
        if not suitable:
            raise RuntimeError("No bounded Arabic sample found for " + typ)
        candidate = sorted(suitable, key=lambda x: abs(len(x.get("content", "")) - 1800))[0]
        original_content = str(candidate["content"])
        new_title, new_content, pieces = translate_document(key, model, str(candidate["title"]), original_content)
        examples[typ[:-1]] = {
            "id": candidate["id"], "url": candidate.get("url"),
            "original_title": candidate["title"], "english_title": new_title,
            "original_html": original_content, "english_html": new_content,
            "segments_translated": pieces, "ready_for_review": True,
        }
        print("PREVIEW_VALIDATED: {} translated; {} visible spans; same HTML tag structure".format(typ[:-1], pieces))
    output = Path("migration-output/translation-preview.json")
    output.write_text(json.dumps({
        "status": "REVIEW_ONLY_NO_BLOGGER_WRITES",
        "provider_model": model,
        "examples": examples,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print("TRANSLATION_PREVIEW_OK: one post and one page saved for human review, no publishing.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("TRANSLATION_PREVIEW_FAIL: {}: {}".format(type(exc).__name__, str(exc)), file=sys.stderr)
        raise SystemExit(1)

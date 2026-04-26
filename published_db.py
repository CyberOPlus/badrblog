# ============================================================
# published_db.py - Duplicate Prevention Database
# ============================================================
# This module manages a simple JSON file that tracks which
# articles have already been published. This prevents the bot
# from posting the same article twice.
#
# HOW IT WORKS:
# - Each published article is identified by its original URL
# - The URLs are stored in a JSON file: data/published_ids.json
# - Before publishing, we check if the URL is already in the file
# - After publishing, we add the URL to the file
# ============================================================

import json

# Import our configuration
from config import PUBLISHED_DB_PATH


def load_published_ids():
    """
    Load the set of already-published article URLs from disk.
    
    Returns:
        set: A set of URL strings that have already been published.
             Returns an empty set if the file doesn't exist yet.
    """
    if not PUBLISHED_DB_PATH.exists():
        # First run — no database file exists yet
        return set()

    try:
        with open(PUBLISHED_DB_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            return set(data.get("published_urls", []))
    except (json.JSONDecodeError, IOError) as e:
        print(f"⚠️  Warning: Could not read published IDs database: {e}")
        print(f"   Starting with empty database to be safe.")
        return set()


def save_published_ids(published_set):
    """
    Save the set of published URLs to disk.
    
    Args:
        published_set (set): The set of URL strings to save
    """
    # Ensure the data directory exists
    PUBLISHED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    data = {
        "published_urls": sorted(list(published_set)),
    }

    with open(PUBLISHED_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def is_already_published(url, published_set):
    """
    Check if an article URL has already been published.
    
    Args:
        url (str): The article URL to check
        published_set (set): The set of published URLs
    
    Returns:
        bool: True if already published, False if it's new
    """
    return url in published_set


def mark_as_published(url, published_set):
    """
    Add a URL to the set of published articles and save to disk.
    
    Args:
        url (str): The article URL to mark as published
        published_set (set): The current set of published URLs
    
    Returns:
        set: The updated set of published URLs
    """
    published_set.add(url)
    save_published_ids(published_set)
    return published_set


def mark_many_as_published(urls, published_set):
    """
    Add multiple URLs to the published set and save once.

    Args:
        urls (iterable): URLs to mark as published
        published_set (set): The current set of published URLs

    Returns:
        set: The updated set of published URLs
    """
    published_set.update(urls)
    save_published_ids(published_set)
    return published_set


def filter_new_articles(articles, published_set):
    """
    Filter out articles that have already been published.
    
    This function takes a list of articles and returns only the
    ones that haven't been published yet. It prints a summary
    showing how many were skipped and how many are new.
    
    Args:
        articles (list): List of article dictionaries with 'url' key
        published_set (set): Set of already-published URLs
    
    Returns:
        list: List of new (unpublished) article dictionaries
    """
    new_articles = []
    skipped = 0

    for article in articles:
        if is_already_published(article["url"], published_set):
            skipped += 1
            print(f"  ⏭️  Skipping (already published): {article['title'][:60]}")
        else:
            new_articles.append(article)

    if skipped > 0:
        print(f"\n  ℹ️  Skipped {skipped} already-published article(s).")

    return new_articles

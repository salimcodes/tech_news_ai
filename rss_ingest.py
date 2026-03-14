import logging
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Callable, Dict, List, Optional, Sequence
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

import feedparser
from newspaper import Article


LOGGER = logging.getLogger(__name__)
TECHCABAL_FEED_URL = "https://techcabal.com/feed/"
TECHCABAL_SITEMAP_URL = "https://techcabal.com/wp-sitemap.xml"
TECHCABAL_SITEMAP_CANDIDATES = [
    TECHCABAL_SITEMAP_URL,
    "https://techcabal.com/sitemap.xml",
    "https://techcabal.com/sitemap_index.xml",
]
DEFAULT_FEED_URLS = [TECHCABAL_FEED_URL]


DEFAULT_REQUEST_TIMEOUT = 30
DEFAULT_FETCH_RETRIES = 3


def fetch_latest_articles_from_feed(
    store,
    feed_url: str,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> int:
    feed = feedparser.parse(feed_url)
    if getattr(feed, "bozo", 0):
        LOGGER.warning("RSS feed parsing reported issues: %s", getattr(feed, "bozo_exception", "unknown"))

    fresh_articles: List[Dict[str, str]] = []
    for entry in feed.entries:
        url = entry.get("link")
        if not url or store.has_url(url):
            continue

        article_payload = _download_article(entry, url, feed_url=feed_url, request_timeout=request_timeout)
        if article_payload:
            fresh_articles.append(article_payload)

    added = store.add_articles(fresh_articles)
    if added:
        LOGGER.info("Stored %s new article(s) from %s.", added, feed_url)
    return added


def fetch_latest_articles(
    store,
    feed_urls: Sequence[str] = DEFAULT_FEED_URLS,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> int:
    total_added = 0
    for feed_url in feed_urls:
        total_added += fetch_latest_articles_from_feed(
            store=store,
            feed_url=feed_url,
            request_timeout=request_timeout,
        )
    return total_added


def start_background_updater(
    store,
    feed_urls: Sequence[str] = DEFAULT_FEED_URLS,
    interval_seconds: int = 900,
) -> threading.Thread:
    def _run() -> None:
        while True:
            try:
                fetch_latest_articles(store=store, feed_urls=feed_urls)
            except Exception as exc:
                LOGGER.exception("Background RSS update failed: %s", exc)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_run, name="rss-updater", daemon=True)
    thread.start()
    return thread


def start_archive_backfill(
    store,
    sitemap_url: str = TECHCABAL_SITEMAP_URL,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> threading.Thread:
    def _run() -> None:
        try:
            backfill_articles_from_sitemap(
                store=store,
                sitemap_url=sitemap_url,
                request_timeout=request_timeout,
            )
        except Exception as exc:
            LOGGER.exception("Archive sitemap backfill failed: %s", exc)

    thread = threading.Thread(target=_run, name="archive-backfill", daemon=True)
    thread.start()
    return thread


def backfill_articles_from_sitemap(
    store,
    sitemap_url: str = TECHCABAL_SITEMAP_URL,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
    batch_size: int = 25,
    progress_callback: Optional[Callable[[Dict[str, int]], None]] = None,
) -> int:
    entries = discover_article_entries_from_sitemap(
        sitemap_url=sitemap_url,
        request_timeout=request_timeout,
    )
    total_entries = len(entries)
    if progress_callback:
        progress_callback(
            {
                "discovered": total_entries,
                "processed": 0,
                "stored": 0,
                "failed": 0,
                "skipped": 0,
            }
        )

    pending_articles: List[Dict[str, str]] = []
    remaining_capacity = _remaining_capacity(store)
    if remaining_capacity == 0:
        return 0

    processed = 0
    stored = 0
    failed = 0
    skipped = 0

    for entry in entries:
        if remaining_capacity > 0 and stored >= remaining_capacity:
            break

        url = entry["url"]
        processed += 1
        if store.has_url(url):
            skipped += 1
            _emit_progress(progress_callback, total_entries, processed, stored, failed, skipped)
            continue

        article_payload = _download_article(
            entry=entry,
            url=url,
            feed_url=sitemap_url,
            request_timeout=request_timeout,
        )
        if article_payload:
            pending_articles.append(article_payload)
            if len(pending_articles) >= batch_size:
                stored += store.add_articles(pending_articles)
                pending_articles = []
        else:
            failed += 1

        _emit_progress(progress_callback, total_entries, processed, stored, failed, skipped)

    if pending_articles:
        stored += store.add_articles(pending_articles)

    added = stored
    if added:
        LOGGER.info("Stored %s backfilled article(s) from sitemap %s.", added, sitemap_url)
    return added


def discover_article_entries_from_sitemap(
    sitemap_url: str = TECHCABAL_SITEMAP_URL,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> List[Dict[str, str]]:
    xml_bytes, resolved_sitemap_url = _fetch_sitemap_bytes(sitemap_url, request_timeout=request_timeout)
    root = ET.fromstring(xml_bytes)
    tag = _strip_namespace(root.tag)

    if tag == "sitemapindex":
        sitemap_links = [_find_child_text(node, "loc") for node in root.findall(_ns_wildcard("sitemap"))]
        child_entries: List[Dict[str, str]] = []
        relevant_sitemaps = [link for link in sitemap_links if link and _is_article_sitemap(link)]
        if not relevant_sitemaps:
            relevant_sitemaps = [link for link in sitemap_links if link]
        for child_sitemap_url in relevant_sitemaps:
            child_entries.extend(discover_article_entries_from_sitemap(sitemap_url=child_sitemap_url, request_timeout=request_timeout))
        child_entries.sort(key=_article_sort_key, reverse=True)
        return child_entries

    if tag != "urlset":
        LOGGER.warning("Unsupported sitemap format at %s: %s", resolved_sitemap_url, tag)
        return []

    entries: List[Dict[str, str]] = []
    for node in root.findall(_ns_wildcard("url")):
        url = _find_child_text(node, "loc")
        if not url or not _looks_like_article_url(url):
            continue
        entries.append(
            {
                "url": url,
                "title": "",
                "published": _find_child_text(node, "lastmod"),
            }
        )

    entries.sort(key=_article_sort_key, reverse=True)
    return entries


def _download_article(
    entry,
    url: str,
    feed_url: str,
    request_timeout: int = DEFAULT_REQUEST_TIMEOUT,
) -> Optional[Dict[str, str]]:
    try:
        article = Article(url, request_timeout=request_timeout)
        article.download()
        article.parse()
    except Exception as exc:
        LOGGER.warning("Failed to download article %s: %s", url, exc)
        return None

    text = (article.text or "").strip()
    if not text:
        LOGGER.warning("Skipping article with empty text: %s", url)
        return None

    title = (article.title or entry.get("title") or "Untitled").strip()
    published = _extract_published_date(entry)

    return {
        "title": title,
        "url": url,
        "date": published,
        "text": text,
        "feed_url": feed_url,
    }


def _extract_published_date(entry) -> str:
    raw_date = entry.get("published") or entry.get("updated") or ""
    if raw_date:
        try:
            return parsedate_to_datetime(raw_date).isoformat()
        except (TypeError, ValueError, IndexError, OverflowError):
            try:
                return raw_date.replace("Z", "+00:00")
            except Exception:
                pass
    return ""


def _article_sort_key(article: Dict[str, str]) -> str:
    return article.get("date") or article.get("published") or ""


def _remaining_capacity(store) -> int:
    max_articles = getattr(store, "max_articles", 0)
    if not max_articles:
        return -1

    count_articles = getattr(store, "count_articles", None)
    if callable(count_articles):
        current_count = count_articles()
    else:
        get_articles = getattr(store, "get_articles", None)
        current_count = len(get_articles()) if callable(get_articles) else 0
    return max(0, max_articles - current_count)


def _emit_progress(
    progress_callback: Optional[Callable[[Dict[str, int]], None]],
    discovered: int,
    processed: int,
    stored: int,
    failed: int,
    skipped: int,
) -> None:
    if progress_callback:
        progress_callback(
            {
                "discovered": discovered,
                "processed": processed,
                "stored": stored,
                "failed": failed,
                "skipped": skipped,
            }
        )


def _fetch_bytes(url: str, request_timeout: int = DEFAULT_REQUEST_TIMEOUT) -> bytes:
    last_error = None
    for attempt in range(1, DEFAULT_FETCH_RETRIES + 1):
        try:
            request = Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; TechNewsAI/1.0; +https://techcabal.com/)",
                },
            )
            with urlopen(request, timeout=request_timeout) as response:
                return response.read()
        except Exception as exc:
            last_error = exc
            LOGGER.warning("Fetch attempt %s/%s failed for %s: %s", attempt, DEFAULT_FETCH_RETRIES, url, exc)
            if attempt < DEFAULT_FETCH_RETRIES:
                time.sleep(2)

    if last_error:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def _fetch_sitemap_bytes(sitemap_url: str, request_timeout: int = DEFAULT_REQUEST_TIMEOUT) -> tuple[bytes, str]:
    candidates = [sitemap_url]
    if sitemap_url == TECHCABAL_SITEMAP_URL:
        for candidate in TECHCABAL_SITEMAP_CANDIDATES:
            if candidate not in candidates:
                candidates.append(candidate)

    last_error = None
    for candidate in candidates:
        try:
            return _fetch_bytes(candidate, request_timeout=request_timeout), candidate
        except Exception as exc:
            last_error = exc
            LOGGER.warning("Failed to fetch sitemap %s: %s", candidate, exc)

    if last_error:
        raise last_error
    raise RuntimeError("No sitemap candidates configured.")


def _strip_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _ns_wildcard(tag: str) -> str:
    return f".//{{*}}{tag}"


def _find_child_text(node: ET.Element, child_name: str) -> str:
    child = node.find(_ns_wildcard(child_name))
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _is_article_sitemap(sitemap_url: str) -> bool:
    lowered = sitemap_url.lower()
    return "post-sitemap" in lowered or "posts-sitemap" in lowered


def _looks_like_article_url(url: str) -> bool:
    lowered = url.lower()
    excluded_segments = (
        "/tag/",
        "/category/",
        "/author/",
        "/page/",
    )
    return lowered.startswith("https://techcabal.com/") and not any(segment in lowered for segment in excluded_segments)

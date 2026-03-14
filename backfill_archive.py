import logging
import os
from datetime import datetime

from dotenv import load_dotenv

from archive_store import ArchiveStore
from rss_ingest import DEFAULT_REQUEST_TIMEOUT, TECHCABAL_SITEMAP_URL, backfill_articles_from_sitemap


load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def get_archive_db_path() -> str:
    return os.getenv("TECHCABAL_ARCHIVE_DB", "techcabal_archive.db").strip() or "techcabal_archive.db"


def get_max_articles() -> int:
    raw_value = os.getenv("TECHCABAL_MAX_ARTICLES", "0").strip()
    try:
        return max(0, int(raw_value))
    except ValueError:
        return 0


def get_sitemap_url() -> str:
    return os.getenv("TECHCABAL_SITEMAP_URL", TECHCABAL_SITEMAP_URL).strip() or TECHCABAL_SITEMAP_URL


def get_request_timeout() -> int:
    raw_value = os.getenv("TECHCABAL_REQUEST_TIMEOUT", str(DEFAULT_REQUEST_TIMEOUT)).strip()
    try:
        return max(10, int(raw_value))
    except ValueError:
        return DEFAULT_REQUEST_TIMEOUT


def main() -> None:
    db_path = get_archive_db_path()
    max_articles = get_max_articles()
    sitemap_url = get_sitemap_url()
    request_timeout = get_request_timeout()
    store = ArchiveStore(db_path=db_path, max_articles=max_articles)
    last_logged_processed = -1

    def on_progress(progress: dict) -> None:
        nonlocal last_logged_processed
        processed = progress["processed"]
        if processed == last_logged_processed:
            return
        if processed == 0 or processed % 25 == 0 or processed == progress["discovered"]:
            last_logged_processed = processed
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"processed={processed}/{progress['discovered']} "
                f"stored={progress['stored']} skipped={progress['skipped']} failed={progress['failed']}"
            )

    print(f"Archive DB: {db_path}")
    print(f"Sitemap: {sitemap_url}")
    print(f"Article cap: {'unlimited' if max_articles == 0 else max_articles}")
    print(f"Request timeout: {request_timeout}s")
    before_count = store.count_articles()
    print(f"Stored before backfill: {before_count}")

    added = backfill_articles_from_sitemap(
        store=store,
        sitemap_url=sitemap_url,
        request_timeout=request_timeout,
        progress_callback=on_progress,
    )

    after_count = store.count_articles()
    print(f"Added this run: {added}")
    print(f"Stored after backfill: {after_count}")


if __name__ == "__main__":
    main()

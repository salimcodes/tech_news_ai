import logging
import os
import threading
from typing import List

from archive_store import ArchiveStore
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from openai import OpenAI

from rss_ingest import (
    DEFAULT_FEED_URLS,
    TECHCABAL_SITEMAP_URL,
    fetch_latest_articles,
    start_archive_backfill,
    start_background_updater,
)


load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = Flask(__name__)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_bootstrap_lock = threading.Lock()
_bootstrapped = False


def get_max_articles() -> int:
    raw_value = os.getenv("TECHCABAL_MAX_ARTICLES", "0").strip()
    try:
        return max(0, int(raw_value))
    except ValueError:
        logging.warning("Invalid TECHCABAL_MAX_ARTICLES=%r. Falling back to 0.", raw_value)
        return 0


def get_configured_feed_urls() -> List[str]:
    raw_value = os.getenv("TECHCABAL_FEED_URLS", "").strip()
    if not raw_value:
        return list(DEFAULT_FEED_URLS)
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def get_archive_backfill_enabled() -> bool:
    return os.getenv("TECHCABAL_ENABLE_ARCHIVE_BACKFILL", "true").strip().lower() in {"1", "true", "yes", "on"}


def get_sitemap_url() -> str:
    return os.getenv("TECHCABAL_SITEMAP_URL", TECHCABAL_SITEMAP_URL).strip() or TECHCABAL_SITEMAP_URL


def get_archive_db_path() -> str:
    return os.getenv("TECHCABAL_ARCHIVE_DB", "techcabal_archive.db").strip() or "techcabal_archive.db"


MAX_ARTICLES = get_max_articles()
FEED_URLS = get_configured_feed_urls()
ARCHIVE_BACKFILL_ENABLED = get_archive_backfill_enabled()
SITEMAP_URL = get_sitemap_url()
ARCHIVE_DB_PATH = get_archive_db_path()
store = ArchiveStore(db_path=ARCHIVE_DB_PATH, max_articles=MAX_ARTICLES)


def bootstrap_ingestion() -> None:
    global _bootstrapped

    with _bootstrap_lock:
        if _bootstrapped:
            return
        fetch_latest_articles(store=store, feed_urls=FEED_URLS)
        start_background_updater(store=store, feed_urls=FEED_URLS, interval_seconds=900)
        if ARCHIVE_BACKFILL_ENABLED:
            start_archive_backfill(store=store, sitemap_url=SITEMAP_URL)
        _bootstrapped = True


@app.before_request
def ensure_ingestion_started() -> None:
    bootstrap_ingestion()


@app.route("/", methods=["GET"])
def index():
    return render_template(
        "index.html",
        article_count=store.count_articles(),
        archive_limit=MAX_ARTICLES,
        feed_urls=FEED_URLS,
        feed_count=len(FEED_URLS),
        archive_backfill_enabled=ARCHIVE_BACKFILL_ENABLED,
        sitemap_url=SITEMAP_URL,
        archive_db_path=ARCHIVE_DB_PATH,
    )


@app.route("/ask", methods=["POST"])
def ask():
    payload = request.get_json(silent=True) or {}
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400

    articles = store.search_articles(question=question, limit=12)
    if not articles:
        return jsonify({"answer": "No TechCabal articles are loaded yet. Please try again shortly."})

    prompt = build_prompt(question=question, articles=articles)
    answer = ask_openai(prompt)
    return jsonify({"answer": answer})


def build_prompt(question: str, articles: List[dict]) -> str:
    article_sections = []
    for article in articles:
        excerpt = article["text"][:3000]
        article_sections.append(
            "\n".join(
                [
                    f"Title: {article['title']}",
                    f"Date: {article['date'] or 'Unknown'}",
                    f"URL: {article['url']}",
                    f"Feed: {article.get('feed_url', 'Unknown')}",
                    f"Content: {excerpt}",
                ]
            )
        )

    context = "\n\n---\n\n".join(article_sections)
    return (
        "You are analyzing African technology news from the TechCabal archive. "
        "Use only the supplied articles to answer the user's question. "
        "If the answer is not supported by the articles, say that clearly.\n\n"
        f"Articles:\n{context}\n\n"
        f"User question: {question}"
    )


def ask_openai(prompt: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key == "your_openai_api_key_here":
        return "OPENAI_API_KEY is not configured. Add it to your .env file and restart the server."

    try:
        response = client.responses.create(
            model="gpt-4o-mini",
            input=prompt,
        )
        return response.output_text.strip()
    except Exception as exc:
        logging.exception("OpenAI request failed: %s", exc)
        return "The AI request failed. Check your OPENAI_API_KEY and try again."


if __name__ == "__main__":
    bootstrap_ingestion()
    app.run(debug=True)

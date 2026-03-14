# Tech News AI

Tech News AI is a lightweight Flask prototype that ingests the latest African technology news from one or more TechCabal RSS feeds and can backfill a broader TechCabal archive from the public sitemap.

## Features

- Pulls article metadata from one or more TechCabal RSS feeds
- Can backfill article URLs from TechCabal's sitemap for broader archive coverage
- Downloads full article text with `newspaper3k`
- Stores TechCabal articles in a local SQLite archive for broader coverage
- Refreshes the feed every 15 minutes in a background thread
- Exposes a simple Flask interface for asking questions about recent coverage

## Project Structure

```text
tech_news_ai/
    app.py
    backfill_archive.py
    rss_ingest.py
    archive_store.py
    templates/
        index.html
    requirements.txt
    .env.example
    README.md
```

## Installation

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Copy `.env.example` to `.env` and set your OpenAI API key:

```env
OPENAI_API_KEY=your_openai_api_key_here
TECHCABAL_FEED_URLS=https://techcabal.com/feed/
TECHCABAL_MAX_ARTICLES=0
TECHCABAL_ENABLE_ARCHIVE_BACKFILL=true
TECHCABAL_SITEMAP_URL=https://techcabal.com/wp-sitemap.xml
TECHCABAL_ARCHIVE_DB=techcabal_archive.db
TECHCABAL_REQUEST_TIMEOUT=30
```

## Running The App

From the `tech_news_ai` directory:

```bash
python app.py
```

Then open `http://127.0.0.1:5000` in your browser.

To populate the archive first, run:

```bash
python backfill_archive.py
```

That command walks the sitemap, downloads article text, stores it in SQLite, and prints progress.

## How RSS Ingestion Works

1. The app reads the comma-separated feed list from `TECHCABAL_FEED_URLS`.
2. If `TECHCABAL_FEED_URLS` is unset, it defaults to `https://techcabal.com/feed/`, which is the main all-posts feed.
3. It extracts the latest article links and metadata from each feed.
4. If `TECHCABAL_ENABLE_ARCHIVE_BACKFILL=true`, it also walks the sitemap at `TECHCABAL_SITEMAP_URL` and discovers additional article URLs.
5. For each unseen article URL, it downloads and parses the article body with `newspaper3k`.
6. Articles are stored in the local archive with:
   - title
   - url
   - publication date
   - full text
   - source feed URL
7. By default, the app stores the full local archive in SQLite with no hard cap. If you want to cap it, set `TECHCABAL_MAX_ARTICLES` to a positive integer.
8. A background thread repeats the RSS check every 15 minutes and skips duplicates.

## Ask Endpoint

The frontend sends a `POST` request to `/ask` with JSON like:

```json
{
  "question": "Summarize the latest TechCabal stories"
}
```

The server retrieves relevant archived articles, sends them to `gpt-4o-mini`, and returns:

```json
{
  "answer": "..."
}
```

## Notes

- The archive is stored in SQLite.
- It uses the RSS feed for freshness and the sitemap for broader history.
- If no articles are loaded yet, the app returns a short waiting message until ingestion succeeds.

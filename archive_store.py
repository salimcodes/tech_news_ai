import re
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{3,}")


class ArchiveStore:
    def __init__(self, db_path: str, max_articles: int = 0) -> None:
        self.db_path = str(Path(db_path))
        self.max_articles = max_articles
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    url TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    date TEXT NOT NULL,
                    text TEXT NOT NULL,
                    feed_url TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_articles_date
                ON articles(date DESC)
                """
            )
            self._conn.commit()

    def has_url(self, url: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM articles WHERE url = ? LIMIT 1",
                (url,),
            ).fetchone()
        return row is not None

    def add_articles(self, articles: List[Dict[str, str]]) -> int:
        added = 0
        with self._lock:
            for article in articles:
                exists = self._conn.execute(
                    "SELECT 1 FROM articles WHERE url = ? LIMIT 1",
                    (article["url"],),
                ).fetchone()
                current_count = self._conn.execute("SELECT COUNT(*) AS count FROM articles").fetchone()["count"]
                if not exists and self.max_articles and current_count >= self.max_articles:
                    break
                cursor = self._conn.execute(
                    """
                    INSERT INTO articles (url, title, date, text, feed_url)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(url) DO UPDATE SET
                        title = excluded.title,
                        date = excluded.date,
                        text = excluded.text,
                        feed_url = excluded.feed_url,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        article["url"],
                        article.get("title", "") or "Untitled",
                        article.get("date", "") or "",
                        article.get("text", "") or "",
                        article.get("feed_url", "") or "",
                    ),
                )
                if cursor.rowcount:
                    added += 1
            self._conn.commit()
        return added

    def count_articles(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS count FROM articles").fetchone()
        return int(row["count"])

    def latest_articles(self, limit: int = 12) -> List[Dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT title, url, date, text, feed_url
                FROM articles
                ORDER BY date DESC, updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def search_articles(self, question: str, limit: int = 12) -> List[Dict[str, str]]:
        terms = _normalize_terms(question)
        if not terms:
            return self.latest_articles(limit=limit)

        score_clauses = []
        parameters: List[str | int] = []
        for term in terms:
            pattern = f"%{term}%"
            score_clauses.append("CASE WHEN lower(title) LIKE ? THEN 8 ELSE 0 END")
            parameters.append(pattern)
            score_clauses.append("CASE WHEN lower(text) LIKE ? THEN 3 ELSE 0 END")
            parameters.append(pattern)

        query = f"""
            SELECT
                title,
                url,
                date,
                text,
                feed_url,
                ({' + '.join(score_clauses)}) AS score
            FROM articles
            WHERE ({' OR '.join(['lower(title) LIKE ? OR lower(text) LIKE ?' for _ in terms])})
            ORDER BY score DESC, date DESC, updated_at DESC
            LIMIT ?
        """
        where_parameters: List[str] = []
        for term in terms:
            pattern = f"%{term}%"
            where_parameters.extend([pattern, pattern])

        with self._lock:
            rows = self._conn.execute(
                query,
                parameters + where_parameters + [limit],
            ).fetchall()

        if rows:
            return [dict(row) for row in rows]
        return self.latest_articles(limit=limit)


def _normalize_terms(text: str) -> List[str]:
    unique_terms = []
    seen = set()
    for match in TOKEN_PATTERN.findall(text.lower()):
        if match not in seen:
            unique_terms.append(match)
            seen.add(match)
    return unique_terms[:8]

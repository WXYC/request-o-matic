#!/usr/bin/env python3
"""
Export library catalog from MySQL to SQLite.

Usage:
    python scripts/export_to_sqlite.py

This connects to your local MySQL, reads LIBRARY_SEARCH, and creates
a SQLite database with FTS5 full-text search support.

Run this after importing a new MySQL dump, then rebuild/redeploy.
"""

import sqlite3
import sys
from pathlib import Path

import pymysql  # type: ignore[import-untyped]

# Configuration - adjust as needed
MYSQL_HOST = "localhost"
MYSQL_USER = "root"
MYSQL_PASSWORD = ""
MYSQL_DATABASE = "wxyc_library"

# Output path
OUTPUT_PATH = Path(__file__).parent.parent / "library.db"


def export():
    print(f"Connecting to MySQL ({MYSQL_DATABASE})...")
    mysql_conn = pymysql.connect(
        host=MYSQL_HOST,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DATABASE,
        cursorclass=pymysql.cursors.DictCursor,
    )

    # Remove existing SQLite file
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()
        print(f"Removed existing {OUTPUT_PATH}")

    print(f"Creating SQLite database at {OUTPUT_PATH}...")
    sqlite_conn = sqlite3.connect(OUTPUT_PATH)
    sqlite_cur = sqlite_conn.cursor()

    # Create main table
    sqlite_cur.execute("""
        CREATE TABLE library (
            id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            call_letters TEXT,
            artist_call_number INTEGER,
            release_call_number INTEGER,
            genre TEXT,
            format TEXT
        )
    """)

    # Create FTS5 virtual table for full-text search
    sqlite_cur.execute("""
        CREATE VIRTUAL TABLE library_fts USING fts5(
            title,
            artist,
            content='library',
            content_rowid='id'
        )
    """)

    # Read from MySQL source tables
    print("Reading from MySQL source tables...")
    with mysql_conn.cursor() as mysql_cur:
        mysql_cur.execute("""
            SELECT
                r.ID as id,
                r.TITLE as title,
                lc.PRESENTATION_NAME as artist,
                lc.CALL_LETTERS as call_letters,
                lc.CALL_NUMBERS as artist_call_number,
                r.CALL_NUMBERS as release_call_number,
                g.REFERENCE_NAME as genre,
                f.REFERENCE_NAME as format
            FROM LIBRARY_RELEASE r
            JOIN LIBRARY_CODE lc ON r.LIBRARY_CODE_ID = lc.ID
            JOIN FORMAT f ON r.FORMAT_ID = f.ID
            JOIN GENRE g ON lc.GENRE_ID = g.ID
        """)
        rows = mysql_cur.fetchall()

    print(f"Exporting {len(rows)} rows...")

    # Insert into SQLite
    for row in rows:
        sqlite_cur.execute(
            """
            INSERT INTO library (id, title, artist, call_letters, artist_call_number, release_call_number, genre, format)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["title"],
                row["artist"],
                row["call_letters"],
                row["artist_call_number"],
                row["release_call_number"],
                row["genre"],
                row["format"],
            ),
        )

    # Populate FTS index
    print("Building full-text search index...")
    sqlite_cur.execute("""
        INSERT INTO library_fts(rowid, title, artist)
        SELECT id, title, artist FROM library
    """)

    # Create additional indexes for filtered searches
    sqlite_cur.execute("CREATE INDEX idx_artist ON library(artist)")
    sqlite_cur.execute("CREATE INDEX idx_title ON library(title)")

    sqlite_conn.commit()

    # Verify
    sqlite_cur.execute("SELECT COUNT(*) FROM library")
    count = sqlite_cur.fetchone()[0]
    print(f"Exported {count} rows to {OUTPUT_PATH}")

    # Test FTS
    sqlite_cur.execute("""
        SELECT l.* FROM library l
        JOIN library_fts fts ON l.id = fts.rowid
        WHERE library_fts MATCH 'stone roses'
        LIMIT 3
    """)
    test_results = sqlite_cur.fetchall()
    print(f"FTS test query 'stone roses' returned {len(test_results)} results")

    # File size
    size_mb = OUTPUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Database size: {size_mb:.2f} MB")

    mysql_conn.close()
    sqlite_conn.close()
    print("Done!")


if __name__ == "__main__":
    export()

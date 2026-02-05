#!/usr/bin/env python3
"""Import Discogs CSV files into PostgreSQL with proper multiline handling."""

import csv
import logging
import sys
from pathlib import Path

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Table configs: (csv_filename, table_name, columns, required_columns)
# required_columns lists column names that must not be null
TABLES = [
    (
        "release.csv",
        "release",
        ["id", "title", "released", "country", "notes", "data_quality", "master_id", "status"],
        ["id", "title"],
    ),
    (
        "release_artist.csv",
        "release_artist",
        [
            "release_id",
            "artist_id",
            "artist_name",
            "extra",
            "anv",
            "position",
            "join_string",
            "role",
            "tracks",
        ],
        ["release_id", "artist_id"],
    ),
    (
        "release_track.csv",
        "release_track",
        ["release_id", "sequence", "position", "parent", "title", "duration", "track_id"],
        ["release_id", "title"],
    ),
    (
        "release_track_artist.csv",
        "release_track_artist",
        [
            "release_id",
            "track_sequence",
            "artist_id",
            "artist_name",
            "extra",
            "anv",
            "position",
            "join_string",
            "role",
        ],
        ["release_id", "track_sequence", "artist_id"],
    ),
    ("release_label.csv", "release_label", ["release_id", "label_name", "catno"], ["release_id"]),
    ("release_genre.csv", "release_genre", ["release_id", "genre"], ["release_id"]),
    ("release_style.csv", "release_style", ["release_id", "style"], ["release_id"]),
    ("artist.csv", "artist", ["id", "name", "realname", "data_quality", "profile"], ["id", "name"]),
]


def import_csv(
    conn, csv_path: Path, table: str, columns: list[str], required_columns: list[str]
) -> int:
    """Import a CSV file into a table, skipping rows with null required fields."""
    logger.info(f"Importing {csv_path.name} into {table}...")

    # Build the COPY command
    col_list = ", ".join(columns)

    # Get indices of required columns
    required_indices = [columns.index(col) for col in required_columns]

    with open(csv_path, encoding="utf-8", errors="replace") as f:
        # Skip header
        next(f)

        with conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({col_list}) FROM STDIN") as copy:
                reader = csv.reader(f)
                count = 0
                skipped = 0
                for row in reader:
                    # Pad row to match columns if short
                    while len(row) < len(columns):
                        row.append("")
                    # Truncate if too long
                    row = row[: len(columns)]

                    # Convert empty strings to None for integer columns
                    processed: list[str | None] = []
                    for val in row:
                        if val == "":
                            processed.append(None)
                        else:
                            processed.append(val)

                    # Skip rows where required columns are null
                    skip = False
                    for idx in required_indices:
                        if processed[idx] is None:
                            skip = True
                            break
                    if skip:
                        skipped += 1
                        continue

                    copy.write_row(processed)
                    count += 1

                    if count % 500000 == 0:
                        logger.info(f"  {count:,} rows...")

    conn.commit()
    if skipped > 0:
        logger.info(f"  Imported {count:,} rows (skipped {skipped:,} with null required fields)")
    else:
        logger.info(f"  Imported {count:,} rows")
    return count


def main():
    if len(sys.argv) < 2:
        print("Usage: import_csv.py <csv_directory> [database_url]")
        sys.exit(1)

    csv_dir = Path(sys.argv[1])
    db_url = sys.argv[2] if len(sys.argv) > 2 else "postgresql:///discogs"

    if not csv_dir.exists():
        logger.error(f"CSV directory not found: {csv_dir}")
        sys.exit(1)

    logger.info(f"Connecting to {db_url}")
    conn = psycopg.connect(db_url)

    total = 0
    for csv_file, table, columns, required_columns in TABLES:
        csv_path = csv_dir / csv_file
        if not csv_path.exists():
            logger.warning(f"Skipping {csv_file} (not found)")
            continue

        count = import_csv(conn, csv_path, table, columns, required_columns)
        total += count

    # Populate cache_metadata
    logger.info("Populating cache_metadata...")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO cache_metadata (release_id, source)
            SELECT id, 'bulk_import'
            FROM release
            ON CONFLICT (release_id) DO NOTHING
        """)
    conn.commit()

    logger.info(f"Total: {total:,} rows imported")
    conn.close()


if __name__ == "__main__":
    main()

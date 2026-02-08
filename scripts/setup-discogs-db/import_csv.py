#!/usr/bin/env python3
"""Import Discogs CSV files into PostgreSQL with proper multiline handling.

Imports only the columns needed by the optimized schema (see 04-create-database.sql).
Dropped tables (release_label, release_genre, release_style, artist) are skipped.
The release_image.csv is processed separately to populate artwork_url on release.
"""

import csv
import logging
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Table configs: (csv_filename, table_name, csv_columns, db_columns, required_columns, transforms)
#
# csv_columns: columns to read from the CSV (in CSV header order)
# db_columns: corresponding column names in the DB table
# required_columns: CSV column names that must not be null
# transforms: dict mapping csv_column -> callable for value transformation
#
# When csv_columns != db_columns, values are mapped positionally.

YEAR_RE = re.compile(r"^\d{4}")


def extract_year(released: str | None) -> str | None:
    """Extract 4-digit year from a Discogs 'released' text field."""
    if released and YEAR_RE.match(released):
        return released[:4]
    return None


class TableConfig(TypedDict):
    csv_file: str
    table: str
    csv_columns: list[str]
    db_columns: list[str]
    required: list[str]
    transforms: dict[str, Callable[[str | None], str | None]]


TABLES: list[TableConfig] = [
    {
        "csv_file": "release.csv",
        "table": "release",
        "csv_columns": ["id", "title", "released"],
        "db_columns": ["id", "title", "release_year"],
        "required": ["id", "title"],
        "transforms": {"released": extract_year},
    },
    {
        "csv_file": "release_artist.csv",
        "table": "release_artist",
        "csv_columns": ["release_id", "artist_name", "extra"],
        "db_columns": ["release_id", "artist_name", "extra"],
        "required": ["release_id"],
        "transforms": {},
    },
    {
        "csv_file": "release_track.csv",
        "table": "release_track",
        "csv_columns": ["release_id", "sequence", "position", "title", "duration"],
        "db_columns": ["release_id", "sequence", "position", "title", "duration"],
        "required": ["release_id", "title"],
        "transforms": {},
    },
    {
        "csv_file": "release_track_artist.csv",
        "table": "release_track_artist",
        "csv_columns": ["release_id", "track_sequence", "artist_name"],
        "db_columns": ["release_id", "track_sequence", "artist_name"],
        "required": ["release_id", "track_sequence"],
        "transforms": {},
    },
]


def import_csv(
    conn,
    csv_path: Path,
    table: str,
    csv_columns: list[str],
    db_columns: list[str],
    required_columns: list[str],
    transforms: dict,
) -> int:
    """Import a CSV file into a table, selecting only needed columns.

    Reads the CSV header to find column indices, extracts only the columns
    listed in csv_columns, applies any transforms, and writes to the DB
    using the corresponding db_columns names.
    """
    logger.info(f"Importing {csv_path.name} into {table}...")

    db_col_list = ", ".join(db_columns)

    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        if not header:
            logger.warning(f"  No header found in {csv_path.name}, skipping")
            return 0

        # Verify required CSV columns exist in header
        missing = [c for c in csv_columns if c not in header]
        if missing:
            logger.error(f"  Missing columns in {csv_path.name}: {missing}")
            return 0

        # Find indices of required columns for null checking
        required_set = set(required_columns)

        with conn.cursor() as cur:
            with cur.copy(f"COPY {table} ({db_col_list}) FROM STDIN") as copy:
                count = 0
                skipped = 0
                for row in reader:
                    # Extract only the columns we need
                    values: list[str | None] = []
                    skip = False
                    for csv_col in csv_columns:
                        val = row.get(csv_col, "")
                        if val == "":
                            val = None

                        # Apply transform if defined
                        if csv_col in transforms:
                            val = transforms[csv_col](val)

                        # Check required columns
                        if csv_col in required_set and val is None:
                            skip = True
                            break

                        values.append(val)

                    if skip:
                        skipped += 1
                        continue

                    copy.write_row(values)
                    count += 1

                    if count % 500000 == 0:
                        logger.info(f"  {count:,} rows...")

    conn.commit()
    if skipped > 0:
        logger.info(f"  Imported {count:,} rows (skipped {skipped:,} with null required fields)")
    else:
        logger.info(f"  Imported {count:,} rows")
    return count


def import_artwork(conn, csv_dir: Path) -> int:
    """Populate release.artwork_url from release_image.csv.

    Reads the release_image CSV and updates the release table with the URI
    of each release's primary image. Only 'primary' type images are used;
    if none exists, the first image is used as fallback.
    """
    csv_path = csv_dir / "release_image.csv"
    if not csv_path.exists():
        logger.warning("release_image.csv not found, skipping artwork import")
        return 0

    logger.info("Importing artwork URLs from release_image.csv...")

    # Collect primary image URIs (one per release)
    artwork: dict[int, str] = {}
    fallback: dict[int, str] = {}

    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                release_id = int(row["release_id"])
            except (ValueError, KeyError):
                continue

            uri = row.get("uri", "")
            if not uri:
                continue

            img_type = row.get("type", "")
            if img_type == "primary" and release_id not in artwork:
                artwork[release_id] = uri
            elif release_id not in fallback:
                fallback[release_id] = uri

    # Merge: prefer primary, fall back to first image
    for release_id, uri in fallback.items():
        if release_id not in artwork:
            artwork[release_id] = uri

    if not artwork:
        logger.info("  No artwork URLs found")
        return 0

    # Batch update using a temp table for efficiency
    logger.info(f"  Updating {len(artwork):,} releases with artwork URLs...")
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TEMP TABLE _artwork (
                release_id integer PRIMARY KEY,
                artwork_url text NOT NULL
            )
        """)

        with cur.copy("COPY _artwork (release_id, artwork_url) FROM STDIN") as copy:
            for release_id, uri in artwork.items():
                copy.write_row((release_id, uri))

        cur.execute("""
            UPDATE release r
            SET artwork_url = a.artwork_url
            FROM _artwork a
            WHERE r.id = a.release_id
        """)

        cur.execute("DROP TABLE _artwork")

    conn.commit()
    logger.info(f"  Updated {len(artwork):,} releases with artwork URLs")
    return len(artwork)


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
    for table_config in TABLES:
        csv_path = csv_dir / table_config["csv_file"]
        if not csv_path.exists():
            logger.warning(f"Skipping {table_config['csv_file']} (not found)")
            continue

        count = import_csv(
            conn,
            csv_path,
            table_config["table"],
            table_config["csv_columns"],
            table_config["db_columns"],
            table_config["required"],
            table_config["transforms"],
        )
        total += count

    # Import artwork from release_image.csv
    import_artwork(conn, csv_dir)

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

#!/bin/bash
# Import filtered Discogs CSV files into PostgreSQL
# Usage: ./06-import-csv.sh /path/to/filtered_csv_output
#
# Prerequisites:
# - PostgreSQL database 'discogs' created
# - Schema from 04-create-database.sql applied
# - User has COPY permissions

set -e

CSV_DIR="${1:-./filtered_csv}"
DB_NAME="${2:-discogs}"
DB_USER="${3:-discogs}"

if [ ! -d "$CSV_DIR" ]; then
    echo "Error: CSV directory not found: $CSV_DIR"
    exit 1
fi

echo "Importing CSVs from: $CSV_DIR"
echo "Database: $DB_NAME"
echo "User: $DB_USER"
echo ""

# Function to import a CSV file
import_csv() {
    local table=$1
    local file="$CSV_DIR/${table}.csv"

    if [ ! -f "$file" ]; then
        echo "Skipping $table (file not found)"
        return
    fi

    echo -n "Importing $table... "

    # Get row count for progress
    local rows=$(wc -l < "$file")
    rows=$((rows - 1))  # Subtract header

    # Use COPY with CSV header option
    psql -U "$DB_USER" -d "$DB_NAME" -c "\COPY $table FROM '$file' WITH (FORMAT csv, HEADER true, ENCODING 'UTF8')"

    echo "done ($rows rows)"
}

# Truncate existing data (optional, comment out if appending)
echo "Truncating existing data..."
psql -U "$DB_USER" -d "$DB_NAME" << 'EOF'
TRUNCATE release CASCADE;
TRUNCATE artist CASCADE;
TRUNCATE cache_metadata;
EOF
echo ""

# Import in order (respecting dependencies)
echo "=== Importing release data ==="
import_csv "release"
import_csv "release_artist"
import_csv "release_track"
import_csv "release_track_artist"
import_csv "release_label"
import_csv "release_genre"
import_csv "release_style"

echo ""
echo "=== Importing artist data ==="
import_csv "artist"

echo ""
echo "=== Populating cache_metadata ==="
psql -U "$DB_USER" -d "$DB_NAME" << 'EOF'
INSERT INTO cache_metadata (release_id, source)
SELECT id, 'bulk_import'
FROM release
ON CONFLICT (release_id) DO NOTHING;
EOF
echo "done"

echo ""
echo "=== Analyzing tables for query optimization ==="
psql -U "$DB_USER" -d "$DB_NAME" -c "ANALYZE;"
echo "done"

echo ""
echo "=== Import complete ==="
psql -U "$DB_USER" -d "$DB_NAME" << 'EOF'
SELECT 'release' as table_name, count(*) as rows FROM release
UNION ALL
SELECT 'release_artist', count(*) FROM release_artist
UNION ALL
SELECT 'release_track', count(*) FROM release_track
UNION ALL
SELECT 'artist', count(*) FROM artist
ORDER BY table_name;
EOF

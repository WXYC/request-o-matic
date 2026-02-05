#!/usr/bin/env python3
"""Convert CSV to tab-separated format suitable for PostgreSQL COPY."""

import csv
import sys
from pathlib import Path


def convert(input_path: Path, output_path: Path) -> int:
    """Convert CSV to TSV with proper escaping."""
    count = 0
    with open(input_path, encoding="utf-8", errors="replace") as infile:
        reader = csv.reader(infile)
        header = next(reader)

        with open(output_path, "w", encoding="utf-8") as outfile:
            # Write header
            outfile.write("\t".join(header) + "\n")

            for row in reader:
                # Escape special characters for PostgreSQL
                escaped = []
                for val in row:
                    if val == "":
                        escaped.append("\\N")  # NULL
                    else:
                        # Escape backslash, tab, newline
                        val = val.replace("\\", "\\\\")
                        val = val.replace("\t", "\\t")
                        val = val.replace("\n", "\\n")
                        val = val.replace("\r", "")
                        escaped.append(val)
                outfile.write("\t".join(escaped) + "\n")
                count += 1

                if count % 500000 == 0:
                    print(f"  {count:,} rows...", file=sys.stderr)

    return count


def main():
    if len(sys.argv) != 3:
        print("Usage: csv_to_tsv.py <input.csv> <output.tsv>")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    print(f"Converting {input_path.name}...", file=sys.stderr)
    count = convert(input_path, output_path)
    print(f"Done: {count:,} rows", file=sys.stderr)


if __name__ == "__main__":
    main()

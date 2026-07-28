#!/usr/bin/env python3
"""
COBOL Copybook Expander
=======================
Iterates through COBOL source programs in a given directory, expands
COPY statements inline using copybooks from another directory, comments
out the original COPY statements, and writes the expanded sources to an
output directory.

Also produces a report of copybooks referenced but not found.

Usage:
    python expand_copybooks.py --src ./sources --cpy ./copybooks --out ./expanded

Assumptions:
    - Source files are text files (one statement per line).
    - Standard COBOL fixed-format rules apply (indicator in column 7,
      code area in columns 8-72).
    - Copybook names in COPY statements map directly to filenames in the
      copybook directory (case-insensitive on Windows).
"""

import os
import re
import sys
import argparse
from pathlib import Path
from typing import List, Set, Dict, Tuple


# ============================================================================
# Configuration
# ============================================================================
MAX_EXPANSION_ITERATIONS = 10   # Guard against circular COPY references
DEFAULT_COPY_EXTENSIONS = ['', '.cpy', '.CPY', '.cob', '.COB', '.txt', '.TXT']


# ============================================================================
# Core Functions
# ============================================================================

def is_copy_statement(line: str) -> bool:
    """
    Determine if a line contains a COBOL COPY statement.

    Rules:
        - Line must be at least 8 characters long.
        - Column 7 (index 6) must NOT be a comment/debug/continuation indicator.
        - The word COPY must appear in the code area (columns 8-72).
    """
    if len(line) < 8:
        return False

    indicator = line[6] if len(line) > 6 else ' '
    if indicator in ('*', '/', '$', 'D', 'd', '-'):
        return False

    # Extract code area (columns 8-72, zero-indexed 7:72)
    code_area = line[7:72]
    upper_code = code_area.upper()

    # Look for COPY as a whole word followed by space or end
    match = re.search(r'\bCOPY\b', upper_code)
    return bool(match)


def extract_copybook_name(line: str) -> str:
    """
    Extract the copybook name from a COPY statement line.

    Expected format in code area:
        COPY copybook-name.
        COPY copybook-name
        COPY copybook-name REPLACING ...

    Returns:
        The copybook name, or empty string if unparseable.
    """
    code_area = line[7:72]
    upper_code = code_area.upper()

    # Find the COPY keyword
    match = re.search(r'\bCOPY\b', upper_code)
    if not match:
        return ''

    # Everything after COPY
    after_copy = code_area[match.end():].strip()

    if not after_copy:
        return ''

    # First token is the copybook name; strip trailing period
    name = after_copy.split()[0].rstrip('.')
    return name


def comment_line(line: str) -> str:
    """
    Comment out a COBOL line by placing '*' in column 7.

    Preserves sequence numbers (columns 1-6) if present.
    """
    if len(line) < 7:
        # Pad to 6 chars then add comment indicator
        seq = line[:6].ljust(6)
        return seq + '* ' + line[6:]

    seq = line[:6]
    rest = line[7:]
    return seq + '*' + rest


def find_copybook_file(copybook_name: str, copybook_dir: Path, 
                       extensions: List[str]) -> Path:
    """
    Locate a copybook file in the copybook directory.

    Tries the exact name first, then appends common extensions.
    Windows paths are case-insensitive by default.

    Returns:
        Path to the copybook file, or None if not found.
    """
    for ext in extensions:
        candidate = copybook_dir / (copybook_name + ext)
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def read_lines(filepath: Path) -> List[str]:
    """Read a text file into a list of lines (newlines stripped)."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            return [line.rstrip('\n\r') for line in f]
    except Exception as e:
        print(f"  ERROR reading {filepath}: {e}")
        return []


def write_lines(filepath: Path, lines: List[str]) -> None:
    """Write a list of lines to a text file."""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            for line in lines:
                f.write(line + '\n')
    except Exception as e:
        print(f"  ERROR writing {filepath}: {e}")


def expand_copybooks(lines: List[str], copybook_dir: Path,
                     missing_log: Set[str],
                     extensions: List[str]) -> List[str]:
    """
    Iteratively expand all COPY statements in the given lines.

    Handles nested copybooks by re-scanning the buffer after each pass.
    Guards against runaway expansion with MAX_EXPANSION_ITERATIONS.

    Args:
        lines: Source lines to process.
        copybook_dir: Directory containing copybook files.
        missing_log: Set to collect names of missing copybooks.
        extensions: List of extensions to try when locating copybooks.

    Returns:
        A new list of lines with copybooks expanded inline.
    """
    working = lines[:]

    for iteration in range(1, MAX_EXPANSION_ITERATIONS + 1):
        changed = False
        new_lines = []

        for line in working:
            if is_copy_statement(line):
                cpy_name = extract_copybook_name(line)

                if not cpy_name:
                    # Unparseable COPY line — comment it out and move on
                    new_lines.append(comment_line(line))
                    continue

                cpy_path = find_copybook_file(cpy_name, copybook_dir, extensions)

                if cpy_path:
                    cpy_lines = read_lines(cpy_path)
                    if cpy_lines:
                        changed = True
                        # Comment out the original COPY statement
                        new_lines.append(comment_line(line))
                        # Insert copybook content
                        new_lines.extend(cpy_lines)
                    else:
                        # Empty copybook — just comment out COPY
                        new_lines.append(comment_line(line))
                else:
                    # Copybook not found — report and comment out
                    missing_log.add(cpy_name)
                    print(f"    MISSING Copybook: {cpy_name}")
                    new_lines.append(comment_line(line))
            else:
                new_lines.append(line)

        if not changed:
            break

        working = new_lines
    else:
        # Loop completed without a 'break' — max iterations reached
        print("  Warning: Max expansion iterations reached — possible circular COPY reference.")

    return working


def process_source_file(src_file: Path, copybook_dir: Path, output_dir: Path,
                        extensions: List[str]) -> Set[str]:
    """
    Process a single source file: expand copybooks and write to output.

    Returns:
        A set of missing copybook names for this source file.
    """
    print(f"Processing: {src_file.name}")
    lines = read_lines(src_file)
    if not lines:
        print(f"  Warning: File is empty or unreadable: {src_file.name}")
        return set()

    missing_for_file = set()
    expanded = expand_copybooks(lines, copybook_dir, missing_for_file, extensions)

    out_file = output_dir / src_file.name
    write_lines(out_file, expanded)
    return missing_for_file


def print_report(processed: int, missing_map: Dict[str, List[str]],
                 total_missing: int):
    """Print a formatted summary report to stdout."""
    print()
    print("=" * 70)
    print("              COPYBOOK EXPANSION SUMMARY REPORT")
    print("=" * 70)
    print(f"Programs Processed : {processed}")
    print(f"Missing Copybooks  : {total_missing}")
    print()

    if total_missing > 0:
        print("MISSING COPYBOOKS (not found in copybook library):")
        print("-" * 70)
        for cpy_name in sorted(missing_map.keys()):
            programs = ", ".join(missing_map[cpy_name])
            print(f"  {cpy_name:<30} referenced in: {programs}")
        print()
    else:
        print("All referenced copybooks were found in the library.")

    print("=" * 70)


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Expand COBOL COPY statements inline from a copybook library."
    )
    parser.add_argument(
        "--src", "-s",
        required=True,
        help="Directory containing COBOL source program files."
    )
    parser.add_argument(
        "--cpy", "-c",
        required=True,
        help="Directory containing copybook files."
    )
    parser.add_argument(
        "--out", "-o",
        required=True,
        help="Directory where expanded programs will be written."
    )
    parser.add_argument(
        "--ext", "-e",
        nargs="+",
        default=None,
        help=("Additional copybook file extensions to try (e.g. .cpy .cob). "
              "Defaults to common COBOL extensions.")
    )

    args = parser.parse_args()

    src_dir = Path(args.src).resolve()
    cpy_dir = Path(args.cpy).resolve()
    out_dir = Path(args.out).resolve()
    extensions = args.ext if args.ext else DEFAULT_COPY_EXTENSIONS

    # Validate directories
    if not src_dir.is_dir():
        print(f"ERROR: Source directory does not exist: {src_dir}")
        sys.exit(1)
    if not cpy_dir.is_dir():
        print(f"ERROR: Copybook directory does not exist: {cpy_dir}")
        sys.exit(1)

    # Create output directory if it doesn't exist
    out_dir.mkdir(parents=True, exist_ok=True)

    # Gather source files (skip directories)
    src_files = [f for f in src_dir.iterdir() if f.is_file()]
    if not src_files:
        print(f"No files found in source directory: {src_dir}")
        sys.exit(0)

    # Process each source file
    overall_missing: Dict[str, List[str]] = {}  # cpy_name -> [program names]
    processed_count = 0

    for src_file in sorted(src_files):
        missing = process_source_file(src_file, cpy_dir, out_dir, extensions)
        processed_count += 1
        for cpy_name in missing:
            if cpy_name not in overall_missing:
                overall_missing[cpy_name] = []
            overall_missing[cpy_name].append(src_file.name)

    # Final report
    print_report(
        processed=processed_count,
        missing_map=overall_missing,
        total_missing=len(overall_missing)
    )


if __name__ == "__main__":
    main()

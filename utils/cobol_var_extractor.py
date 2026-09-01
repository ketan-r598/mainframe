"""
COBOL Variable Value Extractor
==============================
Scans a directory of COBOL source programs, finds variables ending with
-FIL or -DB-ID, and records all literal values assigned to them via:
  - VALUE clauses in WORKING-STORAGE SECTION
  - MOVE literal TO variable in PROCEDURE DIVISION

Usage:
    python cobol_var_extractor.py --src ./cobol_sources
    python cobol_var_extractor.py --src ./cobol_sources --out values.txt

Output format:
    PGM-NAME    VAR-NAME     VALUE
"""

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple


# ============================================================================
# Configuration
# ============================================================================

INDICATOR_COL = 6
CODE_START = 7
CODE_END = 72


# ============================================================================
# Data Structures
# ============================================================================

class VarValue(NamedTuple):
    """A single value assignment found for a target variable."""
    program: str
    var_name: str
    value: str
    source: str          # 'VALUE' or 'MOVE'


# ============================================================================
# Line Processing
# ============================================================================

def normalize_line(line: str) -> str:
    return line.rstrip('\n\r')


def get_indicator(line: str) -> str:
    return line[INDICATOR_COL] if len(line) > INDICATOR_COL else ' '


def is_comment_or_skip_line(line: str) -> bool:
    return get_indicator(line) in ('*', '/', 'D', 'd')


def is_continuation_line(line: str) -> bool:
    return get_indicator(line) == '-'


def get_code_area(line: str) -> str:
    return line[CODE_START:CODE_END]


def join_continuation_lines(lines: List[str]) -> List[str]:
    """Join COBOL continuation lines into single logical lines."""
    result = []
    i = 0
    while i < len(lines):
        current = normalize_line(lines[i])
        if is_comment_or_skip_line(current):
            result.append(current)
            i += 1
            continue

        buffer = current
        while (i + 1 < len(lines) and
               is_continuation_line(normalize_line(lines[i + 1]))):
            i += 1
            cont_line = normalize_line(lines[i])
            buffer += get_code_area(cont_line)
        result.append(buffer)
        i += 1
    return result


# ============================================================================
# Variable Matching
# ============================================================================

def is_target_variable(name: str) -> bool:
    """Check if variable name ends with -FIL or -DB-ID."""
    return name.upper().endswith('-FIL') or name.upper().endswith('-DB-ID')


def is_literal(val: str) -> bool:
    """
    Determine if a token is a real literal value (not a variable name).

    Accepts:
        - Quoted strings:   'ABC', "123"
        - Numeric literals: 123, 456.78, +99, -10
    Rejects:
        - Variable names, figurative constants (ZERO, SPACES, etc.)
    """
    v = val.strip()
    if not v:
        return False

    # Quoted string = literal
    if (v.startswith("'") and v.endswith("'")) or \
       (v.startswith('"') and v.endswith('"')):
        return True

    # Numeric literal = literal
    if re.match(r'^[+-]?\d+(\.\d+)?$', v):
        return True

    return False


def clean_literal(val: str) -> str:
    """Strip quotes from a literal value."""
    v = val.strip()
    if (v.startswith("'") and v.endswith("'")) or \
       (v.startswith('"') and v.endswith('"')):
        return v[1:-1]
    return v


# ============================================================================
# Extraction Logic
# ============================================================================

def extract_value_clause_vars(lines: List[str]) -> List[VarValue]:
    """
    Scan WORKING-STORAGE SECTION for target variables with VALUE clauses.

    Captures:
        77  WS-FIL     PIC X(6)  VALUE 'ABC123'.
        05  WS-DB-ID   PIC 9(4)  VALUE 1234.
    """
    results: List[VarValue] = []
    in_working_storage = False
    program_id = extract_program_id(lines) or 'UNKNOWN'

    for line in lines:
        if is_comment_or_skip_line(line):
            continue

        code = get_code_area(line).upper()

        if 'WORKING-STORAGE' in code and 'SECTION' in code:
            in_working_storage = True
            continue

        if in_working_storage:
            if re.search(r'^(LINKAGE|FILE|COMMUNICATION|REPORT|SCREEN)\s+SECTION', code):
                in_working_storage = False
                continue

            # Match: level name [PIC ...] VALUE [IS] literal
            match = re.search(
                r'(?:0[1-9]|[1-4][0-9]|66|77|88)\s+'
                r'([A-Za-z0-9#@$][A-Za-z0-9#@$-]*)\s+'
                r'(?:PIC\s+[^.]+?\s+)?'
                r'VALUE(?:\s+IS)?\s+'
                r'("[^"]*"|\'[^\']*\'|[+-]?\d+(?:\.\d+)?)',
                code
            )
            if match:
                var_name = match.group(1)
                val_token = match.group(2)
                if is_target_variable(var_name) and is_literal(val_token):
                    results.append(VarValue(
                        program=program_id,
                        var_name=var_name,
                        value=clean_literal(val_token),
                        source='VALUE'
                    ))

    return results


def extract_move_assignments(lines: List[str]) -> List[VarValue]:
    """
    Scan PROCEDURE DIVISION for MOVE literal TO target-variable.

    Captures:
        MOVE 'ABC123' TO WS-FIL.
        MOVE 12345    TO WS-DB-ID.

    Skips:
        MOVE WS-VAR   TO WS-FIL.      (variable-to-variable)
        MOVE ZERO     TO WS-DB-ID.    (figurative constant)
    """
    results: List[VarValue] = []
    in_procedure = False
    program_id = extract_program_id(lines) or 'UNKNOWN'

    for line in lines:
        if is_comment_or_skip_line(line):
            continue

        code = get_code_area(line).upper()

        if 'PROCEDURE' in code and 'DIVISION' in code:
            in_procedure = True
            continue

        if not in_procedure:
            continue

        # Match MOVE literal TO target
        move_match = re.search(
            r'MOVE\s+'
            r'("[^"]*"|\'[^\']*\'|[+-]?\d+(?:\.\d+)?)\s+'
            r'TO\s+'
            r'([A-Za-z0-9#@$][A-Za-z0-9#@$-]*'
            r'(?:\s+OF\s+[A-Za-z0-9#@$][A-Za-z0-9#@$-]*)?)',
            code
        )
        if move_match:
            val_token = move_match.group(1)
            target = move_match.group(2).strip()

            if is_target_variable(target) and is_literal(val_token):
                results.append(VarValue(
                    program=program_id,
                    var_name=target,
                    value=clean_literal(val_token),
                    source='MOVE'
                ))

    return results


# ============================================================================
# COBOL Parsing Helpers
# ============================================================================

def extract_program_id(lines: List[str]) -> Optional[str]:
    """Extract PROGRAM-ID from COBOL source."""
    for i, line in enumerate(lines):
        if is_comment_or_skip_line(line):
            continue
        code = get_code_area(line).upper()

        match = re.search(r'PROGRAM-ID\s*\.\s*([A-Za-z0-9#@$]+)', code)
        if match:
            return match.group(1)

        if re.search(r'PROGRAM-ID\s*\.\s*$', code):
            if i + 1 < len(lines):
                next_code = get_code_area(lines[i + 1]).upper()
                match2 = re.search(r'^\s*([A-Za-z0-9#@$]+)', next_code)
                if match2:
                    return match2.group(1)

    return None


def read_cobol_file(filepath: Path) -> List[str]:
    """Read a COBOL source file."""
    try:
        with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
            return [normalize_line(line) for line in f]
    except Exception as e:
        print(f"  ERROR reading {filepath}: {e}")
        return []


def looks_like_cobol(lines: List[str]) -> bool:
    """Quick check for COBOL keywords."""
    for line in lines[:50]:
        if is_comment_or_skip_line(line):
            continue
        code = get_code_area(line).upper()
        if 'IDENTIFICATION' in code and 'DIVISION' in code:
            return True
        if 'PROGRAM-ID' in code:
            return True
    return False


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract literal values assigned to *-FIL and *-DB-ID variables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python cobol_var_extractor.py --src ./cobol_sources
  python cobol_var_extractor.py --src ./cobol_sources --out values.txt
        """
    )
    parser.add_argument('--src', '-s', required=True,
                        help='Directory containing COBOL source files.')
    parser.add_argument('--out', '-o', default=None,
                        help='Optional: path to save output as a text file.')
    parser.add_argument('--recursive', '-r', action='store_true',
                        help='Scan subdirectories recursively.')

    args = parser.parse_args()

    src_dir = Path(args.src).resolve()
    if not src_dir.is_dir():
        print(f"ERROR: Source directory does not exist: {src_dir}")
        sys.exit(1)

    if args.recursive:
        src_files = [f for f in src_dir.rglob('*') if f.is_file()]
    else:
        src_files = [f for f in src_dir.iterdir() if f.is_file()]

    if not src_files:
        print(f"No files found in source directory: {src_dir}")
        sys.exit(0)

    all_results: List[VarValue] = []

    for src_file in sorted(src_files):
        lines = read_cobol_file(src_file)
        if not lines or not looks_like_cobol(lines):
            continue

        joined = join_continuation_lines(lines)
        prog_id = extract_program_id(joined) or src_file.stem

        value_results = extract_value_clause_vars(joined)
        move_results = extract_move_assignments(joined)

        for r in value_results + move_results:
            all_results.append(VarValue(
                program=prog_id,
                var_name=r.var_name,
                value=r.value,
                source=r.source
            ))

    if not all_results:
        print("No *-FIL or *-DB-ID variables with literal values found.")
        sys.exit(0)

    # Build output lines
    output_lines = []
    output_lines.append(f"{'PGM-NAME':<20} {'VAR-NAME':<25} {'VALUE':<30} {'SOURCE':<8}")
    output_lines.append("=" * 85)

    for r in all_results:
        output_lines.append(f"{r.program:<20} {r.var_name:<25} {r.value:<30} {r.source:<8}")

    # Print to stdout
    print()
    for line in output_lines:
        print(line)
    print()

    # Optional file export
    if args.out:
        try:
            with open(args.out, 'w', encoding='utf-8') as f:
                for line in output_lines:
                    f.write(line + "\n")
            print(f"Output saved to: {args.out}")
        except Exception as e:
            print(f"ERROR writing output file: {e}")


if __name__ == '__main__':
    main()
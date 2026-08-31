#!/usr/bin/env python3
"""
COBOL Call Tree Analyzer
========================
Scans a directory of COBOL source programs, identifies subprogram calls
(static and dynamic), traces dynamic call variables to resolve program names,
and builds a visual dependency tree.

Usage:
    python cobol_call_tree_analyzer.py --src ./cobol_sources
    python cobol_call_tree_analyzer.py --src ./cobol_sources --out tree.txt --csv calls.csv

Output:
    - Text-based tree showing call hierarchy
    - Summary report with statistics
    - Optional CSV and text file exports

Features:
    - Detects static calls:  CALL 'PROGNAME'
    - Detects dynamic calls: CALL WS-VARIABLE
    - Attempts to resolve dynamic variables by tracing:
        * VALUE clauses in WORKING-STORAGE
        * MOVE literal TO variable in PROCEDURE DIVISION
        * Transitive variable chains (A -> B -> C -> 'literal')
    - Detects CICS LINK / XCTL program transfers (including multi-line blocks)
    - Identifies root programs (not called by any other program in the set)
    - Handles missing/unresolved calls gracefully
    - Warns about circular references

Limitations:
    - Assumes one PROGRAM-ID per file (nested programs use first PROGRAM-ID)
    - Dynamic variable resolution is heuristic (VALUE and MOVE only)
    - Complex initializations (STRING, PERFORM paragraphs, table walks) are not traced
    - COPY statements should be expanded first for complete analysis
"""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Set, Tuple


# ============================================================================
# Configuration
# ============================================================================

# COBOL fixed-format column indices (0-based)
INDICATOR_COL = 6          # Column 7
CODE_START = 7             # Column 8
CODE_END = 72              # Column 72 (exclusive)


# ============================================================================
# Data Structures
# ============================================================================

class CallInfo(NamedTuple):
    """Represents a single subprogram call."""
    callee: str              # Resolved or best-guess program name
    call_type: str           # 'STATIC', 'DYNAMIC', 'CICS-LINK', 'CICS-XCTL'
    resolved_name: Optional[str] = None   # For dynamic: the resolved program name
    raw_value: Optional[str] = None       # Original literal or variable name


class ProgramInfo:
    """Holds metadata about a single COBOL program."""
    def __init__(self, name: str, filepath: Path):
        self.name = name
        self.filepath = filepath
        self.calls: List[CallInfo] = []
        self.called_by: Set[str] = set()
        self.is_main = False   # True if not called by any program in the analyzed set


class TreeNode:
    """Node in the rendered call tree."""
    def __init__(self, program_name: str, call_type: str = 'ROOT',
                 resolved_name: Optional[str] = None):
        self.program_name = program_name
        self.call_type = call_type
        self.resolved_name = resolved_name
        self.children: List['TreeNode'] = []
        self.is_leaf = False


# ============================================================================
# Line Processing
# ============================================================================

def normalize_line(line: str) -> str:
    """Strip newline and carriage return characters."""
    return line.rstrip('\n\r')


def get_indicator(line: str) -> str:
    """Return the indicator character in column 7 (index 6)."""
    return line[INDICATOR_COL] if len(line) > INDICATOR_COL else ' '


def is_comment_or_skip_line(line: str) -> bool:
    """Check if line is a comment, debug, or page-eject line."""
    return get_indicator(line) in ('*', '/', 'D', 'd')


def is_continuation_line(line: str) -> bool:
    """Check if line is a continuation (column 7 is '-')."""
    return get_indicator(line) == '-'


def get_code_area(line: str) -> str:
    """Extract the code area (columns 8-72, zero-indexed 7:72)."""
    return line[CODE_START:CODE_END]


def join_continuation_lines(lines: List[str]) -> List[str]:
    """
    Join COBOL continuation lines into single logical lines.

    A line with '-' in column 7 continues the previous line.
    The code area (columns 8-72) of the continuation is appended
    to the previous logical line.
    """
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


def normalize_cics_blocks(lines: List[str]) -> List[str]:
    """
    Detect multi-line EXEC CICS ... END-EXEC blocks and collapse each
    block into a single normalized line.

    Example input:
        EXEC CICS
            LINK PROGRAM('PROGNAME')
            RESP(WS-RESP)
        END-EXEC.

    Output:
        EXEC CICS LINK PROGRAM('PROGNAME') RESP(WS-RESP) END-EXEC.

    This ensures the per-line regex in extract_call_statements() can
    match CICS commands regardless of how many lines they span.
    """
    result: List[str] = []
    i = 0
    while i < len(lines):
        line = normalize_line(lines[i])

        if is_comment_or_skip_line(line):
            result.append(line)
            i += 1
            continue

        code = get_code_area(line).upper()

        # Detect start of an EXEC CICS block
        if re.search(r'EXEC\s+CICS', code):
            block_parts = [code.strip()]
            i += 1
            # Collect all lines until END-EXEC
            while i < len(lines):
                next_line = normalize_line(lines[i])
                if is_comment_or_skip_line(next_line):
                    i += 1
                    continue
                next_code = get_code_area(next_line).upper().strip()
                block_parts.append(next_code)
                if 'END-EXEC' in next_code:
                    i += 1
                    break
                i += 1
            # Collapse block into one logical line
            collapsed = ' '.join(block_parts)
            # Reconstruct a fixed-format line with the collapsed content
            # Preserve sequence area (columns 1-6) from the original EXEC CICS line
            seq = line[:6] if len(line) >= 6 else line.ljust(6)
            # Place a space in indicator column so it is not treated as comment
            new_line = seq + ' ' + collapsed
            result.append(new_line)
            continue

        result.append(line)
        i += 1

    return result


# ============================================================================
# COBOL Parsing Helpers
# ============================================================================

def looks_like_cobol(lines: List[str]) -> bool:
    """Quick check: does the file contain COBOL keywords in first 50 lines?"""
    for line in lines[:50]:
        if is_comment_or_skip_line(line):
            continue
        code = get_code_area(line).upper()
        if 'IDENTIFICATION' in code and 'DIVISION' in code:
            return True
        if 'PROGRAM-ID' in code:
            return True
    return False


def extract_program_id(lines: List[str]) -> Optional[str]:
    """
    Extract the PROGRAM-ID from COBOL source.

    Handles both:
        PROGRAM-ID. PROGNAME.
        PROGRAM-ID.
            PROGNAME.
    """
    for i, line in enumerate(lines):
        if is_comment_or_skip_line(line):
            continue
        code = get_code_area(line).upper()

        # Same-line format
        match = re.search(r'PROGRAM-ID\s*\.\s*([A-Za-z0-9#@$]+)', code)
        if match:
            return match.group(1)

        # Split format: PROGRAM-ID. on one line, name on next
        if re.search(r'PROGRAM-ID\s*\.\s*$', code):
            if i + 1 < len(lines):
                next_code = get_code_area(lines[i + 1]).upper()
                match2 = re.search(r'^\s*([A-Za-z0-9#@$]+)', next_code)
                if match2:
                    return match2.group(1)

    return None


def extract_working_storage_values(lines: List[str]) -> Dict[str, str]:
    """
    Scan WORKING-STORAGE SECTION for variable declarations with VALUE clauses.

    Captures:
        77  WS-PROG  PIC X(8)  VALUE 'PROGNAME'.
        05  WS-PROG  PIC X(8)  VALUE "PROGNAME".
        05  WS-PROG  VALUE 'PROGNAME'.
        05  WS-PROG  PIC X(8)  VALUE IS 'PROGNAME'.

    Returns a dict: variable_name -> literal_value
    """
    values: Dict[str, str] = {}
    in_working_storage = False

    for line in lines:
        if is_comment_or_skip_line(line):
            continue

        code = get_code_area(line).upper()

        # Enter WORKING-STORAGE SECTION
        if 'WORKING-STORAGE' in code and 'SECTION' in code:
            in_working_storage = True
            continue

        if in_working_storage:
            # Exit on next section
            if re.search(r'^(LINKAGE|FILE|COMMUNICATION|REPORT|SCREEN)\s+SECTION', code):
                in_working_storage = False
                continue

            # Match: level name [PIC ...] VALUE [IS] literal
            match = re.search(
                r'(?:0[1-9]|[1-4][0-9]|66|77|88)\s+'
                r'([A-Za-z0-9#@$][A-Za-z0-9#@$-]*)\s+'
                r'(?:PIC\s+[^.]+?\s+)?'
                r'VALUE(?:\s+IS)?\s+'
                r'(?:"([^"]*)"'
                r"|'([^']*)'"
                r'|([A-Za-z0-9#@$][A-Za-z0-9#@$-]*))',
                code
            )
            if match:
                var_name = match.group(1)
                val = match.group(2) or match.group(3) or match.group(4)
                if val is not None:
                    values[var_name] = val.strip()

    return values


def extract_procedure_assignments(lines: List[str]) -> Dict[str, List[str]]:
    """
    Scan PROCEDURE DIVISION for MOVE statements that assign literals to variables.

    Captures:
        MOVE 'literal' TO variable
        MOVE "literal" TO variable
        MOVE figurative-constant TO variable

    Returns dict: variable_name -> list of assigned values
    """
    assignments: Dict[str, List[str]] = defaultdict(list)
    in_procedure = False

    for line in lines:
        if is_comment_or_skip_line(line):
            continue

        code = get_code_area(line).upper()

        # Enter PROCEDURE DIVISION
        if 'PROCEDURE' in code and 'DIVISION' in code:
            in_procedure = True
            continue

        if not in_procedure:
            continue

        # MOVE literal TO target
        move_match = re.search(
            r'MOVE\s+'
            r'(?:"([^"]*)"'
            r"|'([^']*)'"
            r'|([A-Za-z0-9#@$][A-Za-z0-9#@$-]*))\s+'
            r'TO\s+'
            r'([A-Za-z0-9#@$][A-Za-z0-9#@$-]*'
            r'(?:\s+OF\s+[A-Za-z0-9#@$][A-Za-z0-9#@$-]*)?)',
            code
        )
        if move_match:
            literal = move_match.group(1) or move_match.group(2) or move_match.group(3)
            target = move_match.group(4).strip()
            if literal is not None:
                assignments[target].append(literal.strip())
            continue

        # MOVE figurative constant TO target
        fig_match = re.search(
            r'MOVE\s+(SPACES?|ZERO|ZEROES|ZEROS|LOW-VALUES?|HIGH-VALUES?)\s+'
            r'TO\s+'
            r'([A-Za-z0-9#@$][A-Za-z0-9#@$-]*'
            r'(?:\s+OF\s+[A-Za-z0-9#@$][A-Za-z0-9#@$-]*)?)',
            code
        )
        if fig_match:
            literal = fig_match.group(1)
            target = fig_match.group(2).strip()
            assignments[target].append(literal)

    return dict(assignments)


def resolve_dynamic_variable(var_name: str,
                             ws_values: Dict[str, str],
                             proc_assignments: Dict[str, List[str]],
                             visited: Optional[Set[str]] = None) -> Optional[str]:
    """
    Try to resolve a dynamic CALL variable to a program name.
    Follows transitive variable assignments (A -> B -> C -> 'literal').

    Checks:
    1. WORKING-STORAGE VALUE clause
    2. PROCEDURE DIVISION MOVE statements (with transitive resolution)

    Returns the resolved name if found, None otherwise.
    """
    figurative = {
        'SPACES', 'SPACE', 'ZERO', 'ZEROES', 'ZEROS',
        'LOW-VALUE', 'LOW-VALUES', 'HIGH-VALUE', 'HIGH-VALUES'
    }

    if visited is None:
        visited = set()

    # Prevent infinite recursion on circular assignments (A -> B -> A)
    if var_name in visited:
        return None
    visited.add(var_name)

    def _try_resolve(val: str) -> Optional[str]:
        """Try to resolve a single value string."""
        v = val.strip().strip('"\'')
        if not v or v.upper() in figurative:
            return None

        # If this value is itself a known variable, recurse transitively
        if v in ws_values or v in proc_assignments:
            return resolve_dynamic_variable(v, ws_values, proc_assignments, visited)

        # Otherwise, treat as a literal program name
        return v

    # Check WORKING-STORAGE
    if var_name in ws_values:
        result = _try_resolve(ws_values[var_name])
        if result:
            return result

    # Check PROCEDURE assignments
    if var_name in proc_assignments:
        for val in proc_assignments[var_name]:
            result = _try_resolve(val)
            if result:
                return result

    return None


# ============================================================================
# CALL Statement Detection
# ============================================================================

def _strip_quoted_strings(text: str) -> str:
    """
    Replace quoted substrings with spaces of equal length.
    Prevents keywords inside literals (e.g. 'TYPE INVALID CALL I.T.S')
    from being matched as real statements.
    """
    text = re.sub(r'"[^"]*"', lambda m: ' ' * len(m.group(0)), text)
    text = re.sub(r"'[^']*'", lambda m: ' ' * len(m.group(0)), text)
    return text


def extract_call_statements(lines: List[str]) -> List[CallInfo]:
    """
    Extract all subprogram calls from COBOL source lines.

    Detects:
    - Static COBOL CALL:  CALL 'PROGNAME', CALL "PROGNAME"
    - Dynamic COBOL CALL: CALL WS-VAR, CALL WS-VAR USING ...
    - CICS LINK:          EXEC CICS LINK PROGRAM('PROGNAME')
    - CICS XCTL:          EXEC CICS XCTL PROGRAM('PROGNAME')

    Returns a deduplicated list of CallInfo objects.
    """
    calls: List[CallInfo] = []
    ws_values = extract_working_storage_values(lines)
    proc_assignments = extract_procedure_assignments(lines)

    for line in lines:
        if is_comment_or_skip_line(line):
            continue

        # Strip quoted strings from the FULL line first (including cols 73+)
        # because long literals may extend into the identification area.
        # Then extract the code area for keyword searching.
        full_clean = _strip_quoted_strings(line.upper())
        code = get_code_area(line).upper()
        code_clean = get_code_area(full_clean)

        # Skip EXEC SQL lines (stored procedure calls, not subprogram calls)
        if re.search(r'(?<![A-Za-z0-9#@$-])EXEC\s+SQL', code_clean):
            continue

        # --- CICS LINK PROGRAM(...) ---
        for m in re.finditer(
            r'(?<![A-Za-z0-9#@$-])EXEC\s+CICS\s+LINK\s+PROGRAM\s*\(\s*',
            code_clean
        ):
            pos = m.end()
            remainder = code[pos:]
            val_match = re.match(
                r'(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9#@$][A-Za-z0-9#@$-]*))\s*\)',
                remainder
            )
            if val_match:
                raw = val_match.group(1) or val_match.group(2) or val_match.group(3)
                prog = raw.strip('"\'') if raw else ''
                resolved = None
                if prog and not (val_match.group(1) or val_match.group(2)):
                    resolved = resolve_dynamic_variable(prog, ws_values, proc_assignments)
                    if resolved:
                        prog = resolved
                if prog:
                    calls.append(CallInfo(
                        callee=prog,
                        call_type='CICS-LINK',
                        resolved_name=resolved,
                        raw_value=raw
                    ))

        # --- CICS XCTL PROGRAM(...) ---
        for m in re.finditer(
            r'(?<![A-Za-z0-9#@$-])EXEC\s+CICS\s+XCTL\s+PROGRAM\s*\(\s*',
            code_clean
        ):
            pos = m.end()
            remainder = code[pos:]
            val_match = re.match(
                r'(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9#@$][A-Za-z0-9#@$-]*))\s*\)',
                remainder
            )
            if val_match:
                raw = val_match.group(1) or val_match.group(2) or val_match.group(3)
                prog = raw.strip('"\'') if raw else ''
                resolved = None
                if prog and not (val_match.group(1) or val_match.group(2)):
                    resolved = resolve_dynamic_variable(prog, ws_values, proc_assignments)
                    if resolved:
                        prog = resolved
                if prog:
                    calls.append(CallInfo(
                        callee=prog,
                        call_type='CICS-XCTL',
                        resolved_name=resolved,
                        raw_value=raw
                    ))

        # --- COBOL CALL ---
        for m in re.finditer(
            r'(?<![A-Za-z0-9#@$-])CALL\s+',
            code_clean
        ):
            pos = m.end()
            remainder = code[pos:]
            call_match = re.match(
                r'(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9#@$][A-Za-z0-9#@$-]*))',
                remainder
            )
            if not call_match:
                continue

            raw = call_match.group(1) or call_match.group(2) or call_match.group(3)
            if not raw:
                continue

            # Quoted literal = STATIC call
            if call_match.group(1) or call_match.group(2):
                prog_name = raw.strip('"\'').strip()
                if prog_name:
                    calls.append(CallInfo(
                        callee=prog_name,
                        call_type='STATIC',
                        resolved_name=prog_name,
                        raw_value=raw
                    ))
            else:
                # Unquoted identifier = DYNAMIC call
                resolved = resolve_dynamic_variable(raw, ws_values, proc_assignments)
                callee_name = resolved if resolved else f"UNRESOLVED:{raw}"
                calls.append(CallInfo(
                    callee=callee_name,
                    call_type='DYNAMIC',
                    resolved_name=resolved,
                    raw_value=raw
                ))

    # Deduplicate while preserving order
    seen: Set[Tuple[str, str, Optional[str]]] = set()
    unique_calls: List[CallInfo] = []
    for c in calls:
        key = (c.callee, c.call_type, c.raw_value)
        if key not in seen:
            seen.add(key)
            unique_calls.append(c)

    return unique_calls


# ============================================================================
# File Processing
# ============================================================================

def read_cobol_file(filepath: Path) -> List[str]:
    """Read a COBOL source file and return normalized lines."""
    try:
        with open(filepath, 'r', encoding='utf-8-sig', errors='replace') as f:
            return [normalize_line(line) for line in f]
    except Exception as e:
        print(f"  ERROR reading {filepath}: {e}")
        return []


def has_copy_statements(lines: List[str]) -> bool:
    """Check if file contains unexpanded COPY statements."""
    for line in lines:
        if is_comment_or_skip_line(line):
            continue
        code = get_code_area(line).upper()
        if re.search(r'^\s*COPY\s+', code):
            return True
    return False


def process_source_file(filepath: Path) -> Tuple[Optional[ProgramInfo], bool]:
    """
    Process a single COBOL file.

    Returns:
        (ProgramInfo or None, has_copy_statements)
    """
    lines = read_cobol_file(filepath)
    if not lines or not looks_like_cobol(lines):
        return None, False

    # Step 1: Join continuation lines
    joined_lines = join_continuation_lines(lines)
    # Step 2: Normalize multi-line CICS blocks into single lines
    normalized_lines = normalize_cics_blocks(joined_lines)

    prog_name = extract_program_id(normalized_lines)
    if not prog_name:
        return None, False

    has_copy = has_copy_statements(normalized_lines)
    calls = extract_call_statements(normalized_lines)

    info = ProgramInfo(name=prog_name, filepath=filepath)
    info.calls = calls
    return info, has_copy


# ============================================================================
# Call Graph & Tree Building
# ============================================================================

def _build_name_map(programs: Dict[str, ProgramInfo]) -> Dict[str, str]:
    """
    Build an uppercase -> original name mapping for case-insensitive lookups.
    COBOL program names are case-insensitive on mainframes.
    """
    return {name.upper(): name for name in programs.keys()}


def build_call_graph(programs: Dict[str, ProgramInfo]) -> Dict[str, ProgramInfo]:
    """
n    Build the complete call graph including 'called_by' relationships.

    A program is marked as 'main' (root) if no other program in the
    analyzed set calls it. Self-calls are excluded from this check.
    Matching is case-insensitive (COBOL standard).
    """
    name_map = _build_name_map(programs)
    all_names_upper = set(name_map.keys())

    # Build called_by relationships (case-insensitive)
    for prog_name, info in programs.items():
        for call in info.calls:
            callee_upper = call.callee.upper()
            if callee_upper in all_names_upper:
                actual_name = name_map[callee_upper]
                programs[actual_name].called_by.add(prog_name)

    # Identify main programs (exclude self-calls from the check)
    for prog_name, info in programs.items():
        info.is_main = not (info.called_by - {prog_name})

    return programs


def build_call_trees(programs: Dict[str, ProgramInfo]) -> List[TreeNode]:
    """
    Build a forest of call trees. Each root is a main program.

    A program called by multiple parents may appear in multiple trees
    or multiple branches. Cycles are detected and marked.
    Matching is case-insensitive (COBOL standard).
    """
    name_map = _build_name_map(programs)
    all_names_upper = set(name_map.keys())
    roots = sorted(name for name, info in programs.items() if info.is_main)

    trees: List[TreeNode] = []

    def build_subtree(prog_name: str, visited: Set[str]) -> Optional[TreeNode]:
        """Recursively build a subtree starting from prog_name."""
        if prog_name in visited:
            # Circular reference detected
            return TreeNode(prog_name, call_type='CIRCULAR')

        if prog_name not in programs:
            # External program not in our directory
            return None

        info = programs[prog_name]
        node = TreeNode(prog_name,
                        call_type='ROOT' if info.is_main else 'NODE')

        if not info.calls:
            node.is_leaf = True

        new_visited = visited | {prog_name}

        for call in info.calls:
            if call.callee.startswith('UNRESOLVED:'):
                # Unresolved dynamic call — show variable name as leaf
                child = TreeNode(
                    program_name=call.raw_value or call.callee,
                    call_type='DYNAMIC'
                )
                child.is_leaf = True
                node.children.append(child)

            else:
                callee_upper = call.callee.upper()
                if callee_upper in all_names_upper:
                    # Known program in our directory — recurse transitively
                    actual_name = name_map[callee_upper]
                    child = build_subtree(actual_name, new_visited)
                    if child:
                        child.call_type = call.call_type
                        child.resolved_name = call.resolved_name
                        node.children.append(child)
                else:
                    # External program (not in directory)
                    child = TreeNode(
                        program_name=call.callee,
                        call_type=call.call_type,
                        resolved_name=call.resolved_name
                    )
                    child.is_leaf = True
                    node.children.append(child)

        return node

    for root_name in roots:
        tree = build_subtree(root_name, set())
        if tree:
            trees.append(tree)

    return trees


# ============================================================================
# Tree Rendering
# ============================================================================

def render_tree(node: TreeNode, prefix: str = "", is_last: bool = True,
                is_root: bool = True) -> List[str]:
    """
    Render a tree node and its children as text lines with Unicode box-drawing.

    Example output:
        MAINPROG
        ├── SUBPROG1 [STATIC]
        │   └── SUBSUB1 [STATIC]
        └── SUBPROG2 [DYNAMIC -> PROG3]
    """
    lines: List[str] = []

    if is_root:
        lines.append(node.program_name)
    else:
        branch = "└── " if is_last else "├── "
        label = format_node_label(node)
        lines.append(prefix + branch + label)

    child_prefix = prefix + ("    " if is_last else "│   ")

    for i, child in enumerate(node.children):
        last_child = (i == len(node.children) - 1)
        lines.extend(render_tree(child, child_prefix, last_child, is_root=False))

    return lines


def format_node_label(node: TreeNode) -> str:
    """Format a tree node label with program name and call type annotation."""
    if node.call_type == 'CIRCULAR':
        return f"{node.program_name} [↻ CIRCULAR REFERENCE]"

    if node.call_type == 'ROOT':
        return node.program_name

    if node.call_type == 'STATIC':
        return f"{node.program_name} [STATIC]"

    if node.call_type == 'DYNAMIC':
        if node.resolved_name:
            return f"{node.program_name} [DYNAMIC]"
        else:
            return f"{node.program_name} [DYNAMIC — unresolved]"

    if node.call_type == 'CICS-LINK':
        return f"{node.program_name} [CICS-LINK]"

    if node.call_type == 'CICS-XCTL':
        return f"{node.program_name} [CICS-XCTL]"

    return node.program_name


# ============================================================================
# Reporting & Export
# ============================================================================

def print_tree_output(trees: List[TreeNode], programs: Dict[str, ProgramInfo]) -> None:
    """Print the complete call tree output to stdout."""
    print()
    print("=" * 78)
    print("                    C O B O L   C A L L   T R E E   A N A L Y S I S")
    print("=" * 78)
    print()

    if not trees:
        print("No main programs (roots) found.")
        print()
        print("All programs in the directory are called by external entities")
        print("(batch JCL, CICS transactions, or programs outside this directory).")
        print()
        print("Standalone programs in the directory:")
        for name in sorted(programs.keys()):
            print(f"  {name}")
        print()
        return

    for i, tree in enumerate(trees):
        if i > 0:
            print()  # Blank line between trees
        for line in render_tree(tree):
            print(line)

    print()
    print("=" * 78)


def print_summary_report(programs: Dict[str, ProgramInfo]) -> None:
    """Print a summary report with statistics."""
    total_programs = len(programs)
    main_programs = sum(1 for p in programs.values() if p.is_main)
    sub_programs = total_programs - main_programs

    stats: Dict[str, int] = defaultdict(int)
    unresolved: List[Tuple[str, str]] = []
    external_calls = 0
    all_callees: Set[str] = set()

    name_map = _build_name_map(programs)
    all_names_upper = set(name_map.keys())

    for info in programs.values():
        for call in info.calls:
            all_callees.add(call.callee)
            stats[call.call_type] += 1
            if call.call_type == 'DYNAMIC' and not call.resolved_name:
                unresolved.append((info.name, call.raw_value or 'unknown'))
            if call.callee.upper() not in all_names_upper and not call.callee.startswith('UNRESOLVED:'):
                external_calls += 1

    print("=" * 78)
    print("                         S U M M A R Y   R E P O R T")
    print("=" * 78)
    print(f"  Total Programs Analyzed      : {total_programs}")
    print(f"  Main Programs (roots)        : {main_programs}")
    print(f"  Sub-Programs (non-roots)     : {sub_programs}")
    print(f"  {'─' * 74}")
    print(f"  Static CALLs                 : {stats['STATIC']}")
    print(f"  Dynamic CALLs (resolved)     : {stats.get('DYNAMIC', 0) - len(unresolved)}")
    print(f"  Dynamic CALLs (unresolved)   : {len(unresolved)}")
    print(f"  CICS LINK calls              : {stats['CICS-LINK']}")
    print(f"  CICS XCTL calls              : {stats['CICS-XCTL']}")
    print(f"  Calls to External Programs   : {external_calls}")
    print(f"  {'─' * 74}")
    print(f"  Unique Programs Called       : {len(all_callees)}")
    print("=" * 78)
    print()

    if unresolved:
        print("UNRESOLVED DYNAMIC CALLS (manual review needed):")
        print("─" * 78)
        for caller, var in sorted(unresolved):
            print(f"  {caller:<30} calls variable: {var}")
        print()
        print("=" * 78)
        print()


def export_csv(programs: Dict[str, ProgramInfo], output_path: Path) -> None:
    """Export call relationships to a CSV file."""
    try:
        with open(output_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Caller Program', 'Callee Program', 'Call Type',
                'Resolved Name', 'Raw Value', 'Callee in Directory'
            ])
            name_map = _build_name_map(programs)
            all_names_upper = set(name_map.keys())
            for info in programs.values():
                for call in info.calls:
                    in_dir = 'Yes' if call.callee.upper() in all_names_upper else 'No'
                    writer.writerow([
                        info.name,
                        call.callee,
                        call.call_type,
                        call.resolved_name or '',
                        call.raw_value or '',
                        in_dir
                    ])
        print(f"CSV export saved to: {output_path}")
    except Exception as e:
        print(f"ERROR writing CSV: {e}")


def export_text_tree(trees: List[TreeNode], output_path: Path) -> None:
    """Export the rendered tree to a text file."""
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            for i, tree in enumerate(trees):
                if i > 0:
                    f.write("\n")
                for line in render_tree(tree):
                    f.write(line + "\n")
        print(f"Tree text saved to: {output_path}")
    except Exception as e:
        print(f"ERROR writing tree text: {e}")


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Analyze COBOL programs and build call dependency trees.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python %(prog)s --src ./cobol_sources
  python %(prog)s --src ./cobol_sources --out call_tree.txt
  python %(prog)s --src ./cobol_sources --csv calls.csv --recursive
        """
    )
    parser.add_argument(
        '--src', '-s',
        required=True,
        help='Directory containing COBOL source program files.'
    )
    parser.add_argument(
        '--out', '-o',
        default=None,
        help='Optional: path to save the tree output as a text file.'
    )
    parser.add_argument(
        '--csv', '-c',
        default=None,
        help='Optional: path to export call relationships as CSV.'
    )
    parser.add_argument(
        '--recursive', '-r',
        action='store_true',
        help='Scan subdirectories recursively.'
    )

    args = parser.parse_args()

    src_dir = Path(args.src).resolve()
    if not src_dir.is_dir():
        print(f"ERROR: Source directory does not exist: {src_dir}")
        sys.exit(1)

    # Gather source files
    if args.recursive:
        src_files = [f for f in src_dir.rglob('*') if f.is_file()]
    else:
        src_files = [f for f in src_dir.iterdir() if f.is_file()]

    if not src_files:
        print(f"No files found in source directory: {src_dir}")
        sys.exit(0)

    print(f"Scanning {len(src_files)} files from: {src_dir}")
    print()

    # Process each file
    programs: Dict[str, ProgramInfo] = {}
    copybook_warning = False

    for src_file in sorted(src_files):
        info, has_copy = process_source_file(src_file)
        if has_copy:
            copybook_warning = True
        if info:
            if info.name in programs:
                print(f"  WARNING: Duplicate PROGRAM-ID '{info.name}' — "
                      f"{src_file.name} conflicts with {programs[info.name].filepath.name}")
            else:
                programs[info.name] = info
                print(f"  OK  {info.name:<20} ({src_file.name})")
        else:
            # Silently skip non-COBOL files; warn if it looked like COBOL
            lines = read_cobol_file(src_file)
            if lines and looks_like_cobol(lines):
                joined = join_continuation_lines(lines)
                normalized = normalize_cics_blocks(joined)
                if not extract_program_id(normalized):
                    print(f"  SKIP {src_file.name:<20} (COBOL-like but no PROGRAM-ID)")

    if not programs:
        print("\nNo valid COBOL programs found in the directory.")
        sys.exit(0)

    if copybook_warning:
        print("\n  NOTE: Some files contain unexpanded COPY statements.")
        print("  For complete analysis, expand copybooks first using the copybook expander.")

    print(f"\nFound {len(programs)} valid COBOL programs.")

    # Build call graph and trees
    programs = build_call_graph(programs)
    trees = build_call_trees(programs)

    # Output
    print_tree_output(trees, programs)
    print_summary_report(programs)

    # Optional exports
    if args.out:
        export_text_tree(trees, Path(args.out))
    if args.csv:
        export_csv(programs, Path(args.csv))


if __name__ == '__main__':
    main()
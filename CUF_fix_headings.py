#!/usr/bin/env python3
"""
fix_headings.py

Fixes heading levels in MinerU-generated Markdown files using a reference
hierarchy file that contains the correct nesting depth for each heading.

Handles three cases:
  1. Lines that already have # but the wrong level  → fix the level.
  2. Plain-text lines that match a hierarchy entry   → add the correct # level.
  3. Lines where heading text is merged with body    → split + add heading level.
     (e.g. "Artículo 1.- body text..." becomes "##### Artículo 1\\n\\nbody text...")

Also recognises abbreviated forms:
  - "I.", "III bis." etc. match "Fracción I", "Fracción III bis" in the hierarchy.
  - "a)", "b)" etc. match "Inciso a)", "Inciso b)" in the hierarchy.

Algorithm: two-pointer sequential matching.  Both files follow the same
document order, so we walk through the .md file and advance a pointer through
the hierarchy file, matching headings by normalised text (exact first, then
fuzzy fallback for # headings only).

Usage:
    python fix_headings.py CUF.md --hierarchy CUF_hierarchy.md
    python fix_headings.py CUF.md                        # auto-discover hierarchy
    python fix_headings.py some_folder/                   # batch mode
    python fix_headings.py CUF.md --hierarchy h.md --inplace
    python fix_headings.py CUF.md --hierarchy h.md --threshold 0.90
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_QUOTE_MAP = str.maketrans({
    "\u201c": '"',   # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',   # RIGHT DOUBLE QUOTATION MARK
    "\u2018": "'",   # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",   # RIGHT SINGLE QUOTATION MARK
    "\u00ab": '"',   # LEFT-POINTING DOUBLE ANGLE QUOTATION MARK
    "\u00bb": '"',   # RIGHT-POINTING DOUBLE ANGLE QUOTATION MARK
})


def normalize(text: str) -> str:
    """Lowercase, strip accents, normalise quotes, collapse whitespace,
    strip trailing punctuation."""
    text = text.lower()
    # NFKD decomposition then drop combining marks (accents)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # strip all quote characters so 'ANEXO "C"' and 'ANEXO C' match
    text = text.translate(_QUOTE_MAP)
    text = re.sub(r"""[\"'`]""", "", text)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # strip trailing punctuation like .- or .
    text = re.sub(r"[\.\-]+$", "", text).strip()
    return text


_HEADING_RE = re.compile(r"^(#{1,7})\s*(.*)")


def parse_heading(line: str) -> Optional[Tuple[int, str]]:
    """Return (level, text) if *line* is a markdown heading, else None."""
    m = _HEADING_RE.match(line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Hierarchy parsing
# ---------------------------------------------------------------------------

# Each hierarchy entry: (level, raw_text, norm_text, abbrev_norm)
# abbrev_norm is the short form for Fracción/Inciso entries, else None.
HEntry = Tuple[int, str, str, Optional[str]]


def _compute_abbreviation(norm_text: str) -> Optional[str]:
    """Extract abbreviated form: 'fraccion i' -> 'i', 'inciso a)' -> 'a)'."""
    for prefix in ("fraccion ", "inciso "):
        if norm_text.startswith(prefix):
            return norm_text[len(prefix):]
    return None


def load_hierarchy(path: Path) -> List[HEntry]:
    """Read hierarchy file → [(level, raw_text, norm_text, abbrev_norm), ...]."""
    entries: List[HEntry] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parsed = parse_heading(line.rstrip("\n"))
            if parsed:
                level, text = parsed
                if text:
                    norm = normalize(text)
                    abbrev = _compute_abbreviation(norm)
                    entries.append((level, text, norm, abbrev))
    return entries


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

_SHORT_TEXT_LEN = 15  # short texts use tighter fuzzy threshold

# Separator pattern: "prefix .- body"  (Artículo N.- text...)
_ARTICLE_SEP_RE = re.compile(r"^(.+?)\s*\.\s*[-\u2013\u2014]\s*(.+)$")

# Roman numeral fraction: standalone like "I.", "III bis.", "XXIX."
# Requires the trailing period so random words starting with I/V/X etc. don't match.
_ROMAN_STANDALONE_RE = re.compile(
    r"^([IVXLCDM]+(?:\s+(?:bis|ter|qu[a\u00e1]ter|quinquies|sexies|septies))?)\s*\.\s*$",
    re.IGNORECASE,
)

# Inciso pattern: single letter followed by ) -- standalone only  (e.g. "a)" alone)
_INCISO_STANDALONE_RE = re.compile(r"^([a-zA-Z]\))\s*$")


def _try_match(
    md_norm: str,
    hierarchy: List[HEntry],
    start: int,
    threshold: float,
    *,
    check_abbrev: bool = False,
    allow_fuzzy: bool = True,
) -> Optional[Tuple[int, str, float]]:
    """Scan hierarchy from *start* for a match against *md_norm*.

    Pass 1 — exact normalised match.
    Pass 1b — exact abbreviated match (if check_abbrev).
    Pass 2 — fuzzy match (if allow_fuzzy).

    Returns (index, match_type, ratio) or None.
    """
    h_len = len(hierarchy)

    # --- pass 1: exact on norm_text ---
    for i in range(start, h_len):
        if md_norm == hierarchy[i][2]:
            return i, "exact", 1.0

    # --- pass 1b: exact on abbreviated form ---
    if check_abbrev:
        for i in range(start, h_len):
            abbr = hierarchy[i][3]
            if abbr is not None and md_norm == abbr:
                return i, "exact-abbrev", 1.0

    # --- pass 2: fuzzy on norm_text ---
    if allow_fuzzy:
        eff_threshold = threshold
        if len(md_norm) < _SHORT_TEXT_LEN:
            eff_threshold = max(threshold, 0.95)
        for i in range(start, h_len):
            ratio = fuzzy_ratio(md_norm, hierarchy[i][2])
            if ratio >= eff_threshold:
                return i, "fuzzy", ratio

    return None


def _try_plain_text_match(
    stripped: str,
    hierarchy: List[HEntry],
    h_idx: int,
    threshold: float,
) -> Optional[Tuple[int, str, float, str, Optional[str]]]:
    """Try to match a plain-text line (no #) against the hierarchy.

    Strategies tried in order:
      1. Full standalone match  — for "TÍTULO XIV", "Capítulo I", etc.
      2. Separator split        — for "Artículo 1.- body text..."
      3. Roman numeral fraction — for "I.", "III bis.", "I. text"
      4. Inciso                 — for "a)", "a) text"

    Returns (h_index, match_type, ratio, heading_text, body_text) or None.
    body_text is None when no split is needed.
    Plain-text matching uses exact + abbreviated only (no fuzzy) to
    avoid false positives on regular body lines.
    """
    norm = normalize(stripped)
    if not norm:
        return None

    # --- Strategy 1: full standalone match (short lines) ---
    if len(stripped) < 200:
        result = _try_match(
            norm, hierarchy, h_idx, threshold,
            check_abbrev=True, allow_fuzzy=False,
        )
        if result:
            return (*result, stripped, None)

    # --- Strategy 2: separator split  (prefix .- body) ---
    # Handles "Artículo 1.- text", "I.- text", "a).- text", etc.
    m = _ARTICLE_SEP_RE.match(stripped)
    if m:
        prefix = m.group(1).strip()
        body = m.group(2).strip()
        if prefix and body:
            prefix_norm = normalize(prefix)
            if prefix_norm:
                result = _try_match(
                    prefix_norm, hierarchy, h_idx, threshold,
                    check_abbrev=True, allow_fuzzy=False,
                )
                if result:
                    return (*result, prefix, body)

    # --- Strategy 3: standalone Roman numeral fraction ---
    # Only matches lines that are JUST a numeral + period: "I.", "III bis.", "XXIX."
    m = _ROMAN_STANDALONE_RE.match(stripped)
    if m:
        numeral = m.group(1).strip()
        numeral_norm = normalize(numeral)
        if numeral_norm:
            result = _try_match(
                numeral_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
            )
            if result:
                heading = numeral + "."
                return (*result, heading, None)

    # --- Strategy 4: standalone Inciso ---
    # Only matches lines that are JUST "a)", "b)", etc.
    m = _INCISO_STANDALONE_RE.match(stripped)
    if m:
        letter = m.group(1).strip()
        letter_norm = normalize(letter)
        if letter_norm:
            result = _try_match(
                letter_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
            )
            if result:
                return (*result, letter, None)

    return None


# ---------------------------------------------------------------------------
# Core: fix one file
# ---------------------------------------------------------------------------

class MatchRecord:
    """One matched heading."""
    __slots__ = ("line_no", "old_line", "new_line", "match_type", "ratio",
                 "source", "body_text")

    def __init__(
        self,
        line_no: int,
        old_line: str,
        new_line: str,
        match_type: str,
        ratio: float,
        source: str = "heading",
        body_text: Optional[str] = None,
    ):
        self.line_no = line_no
        self.old_line = old_line
        self.new_line = new_line
        self.match_type = match_type   # "exact" | "exact-abbrev" | "fuzzy"
        self.ratio = ratio
        self.source = source           # "heading" | "plain" | "split"
        self.body_text = body_text     # remaining text after split, or None


class UnmatchedHeading:
    __slots__ = ("line_no", "line")

    def __init__(self, line_no: int, line: str):
        self.line_no = line_no
        self.line = line


def fix_headings(
    md_path: Path,
    hierarchy: List[HEntry],
    threshold: float = 0.85,
) -> Tuple[List[str], List[MatchRecord], List[UnmatchedHeading], List[Tuple[int, str]]]:
    """Return (output_lines, matches, unmatched_md_headings, skipped_hierarchy)."""

    with open(md_path, encoding="utf-8") as fh:
        md_lines = fh.read().splitlines()

    h_idx = 0
    h_len = len(hierarchy)
    matched_h: set[int] = set()

    title_norm = hierarchy[0][2] if hierarchy else ""
    title_seen = False

    output: List[str] = []
    matches: List[MatchRecord] = []
    unmatched: List[UnmatchedHeading] = []

    for line_no_0, raw_line in enumerate(md_lines):
        line_no = line_no_0 + 1
        parsed = parse_heading(raw_line)

        # ── Case A: heading line with non-empty text ──────────────────────
        if parsed is not None and parsed[1]:
            md_text = parsed[1]
            md_norm = normalize(md_text)

            # TOC/body reset
            if title_norm and title_seen and h_idx > 0:
                if md_norm == title_norm or fuzzy_ratio(md_norm, title_norm) >= threshold:
                    h_idx = 0

            result = _try_match(
                md_norm, hierarchy, h_idx, threshold,
                check_abbrev=False, allow_fuzzy=True,
            )
            if result:
                i, match_type, ratio = result
                new_line = "#" * hierarchy[i][0] + " " + md_text
                matches.append(MatchRecord(
                    line_no, raw_line, new_line, match_type, ratio, "heading",
                ))
                matched_h.add(i)
                output.append(new_line)
                h_idx = i + 1
                if i == 0:
                    title_seen = True
            else:
                output.append(raw_line)
                unmatched.append(UnmatchedHeading(line_no, raw_line))
            continue

        # ── Case B: bare # with empty text ────────────────────────────────
        if parsed is not None:
            output.append(raw_line)
            unmatched.append(UnmatchedHeading(line_no, raw_line))
            continue

        # ── Case C: plain text (no #) ─────────────────────────────────────
        stripped = raw_line.strip()
        if not stripped:
            output.append(raw_line)
            continue

        # TOC/body reset on plain-text title
        pt_norm = normalize(stripped)
        if title_norm and title_seen and h_idx > 0 and pt_norm:
            if pt_norm == title_norm or fuzzy_ratio(pt_norm, title_norm) >= threshold:
                h_idx = 0

        pt_result = _try_plain_text_match(stripped, hierarchy, h_idx, threshold)
        if pt_result:
            i, match_type, ratio, heading_text, body_text = pt_result
            h_level = hierarchy[i][0]
            new_line = "#" * h_level + " " + heading_text
            source = "split" if body_text else "plain"
            matches.append(MatchRecord(
                line_no, raw_line, new_line, match_type, ratio, source, body_text,
            ))
            matched_h.add(i)
            output.append(new_line)
            if body_text:
                output.append("")
                output.append(body_text)
            h_idx = i + 1
            if i == 0:
                title_seen = True
        else:
            output.append(raw_line)

    # Skipped hierarchy entries (not matched in any section)
    skipped = [
        (hierarchy[i][0], hierarchy[i][1])
        for i in range(h_len)
        if i not in matched_h
    ]

    return output, matches, unmatched, skipped


# ---------------------------------------------------------------------------
# Verification report
# ---------------------------------------------------------------------------

def print_report(
    md_name: str,
    total_headings: int,
    matches: List[MatchRecord],
    unmatched: List[UnmatchedHeading],
    skipped: List[Tuple[int, str]],
    total_hierarchy: int,
) -> None:
    """Print a human-readable verification report to stdout."""

    print(f"\n{'=' * 60}")
    print(f"  {md_name}")
    print(f"{'=' * 60}")

    heading_matches = [m for m in matches if m.source == "heading"]
    plain_matches   = [m for m in matches if m.source == "plain"]
    split_matches   = [m for m in matches if m.source == "split"]
    fuzzy_matches   = [m for m in matches if m.match_type == "fuzzy"]
    abbrev_matches  = [m for m in matches if m.match_type == "exact-abbrev"]

    # --- Summary ---
    print(f"\nTOTAL MATCHED: {len(matches)}")
    print(f"  # heading level fixes : {len(heading_matches)}")
    print(f"  plain-text -> heading  : {len(plain_matches)}")
    print(f"  split (heading + body): {len(split_matches)}")

    # --- All matches ---
    print(f"\nMATCHED DETAILS ({len(matches)}):")
    for m in matches:
        old_short = m.old_line[:55].ljust(55)
        new_short = m.new_line[:55]
        tag_parts = [m.match_type]
        if m.source != "heading":
            tag_parts.append(m.source)
        if m.match_type == "fuzzy":
            tag_parts.append(f"{m.ratio:.2f}")
        tag = ", ".join(tag_parts)
        print(f"  Line {m.line_no:5d}: {old_short} -> {new_short}  ({tag})")

    # --- Fuzzy detail ---
    if fuzzy_matches:
        print(f"\nFUZZY MATCHES ({len(fuzzy_matches)}) — review these:")
        for m in fuzzy_matches:
            print(f"  Line {m.line_no:5d} (ratio {m.ratio:.2f}):")
            print(f"    md   : {m.old_line}")
            print(f"    fixed: {m.new_line}")

    # --- Abbreviated detail ---
    if abbrev_matches:
        print(f"\nABBREVIATED MATCHES ({len(abbrev_matches)}):")
        for m in abbrev_matches:
            src = f", {m.source}" if m.source != "heading" else ""
            print(f"  Line {m.line_no:5d}: {m.old_line.strip()!r} -> {m.new_line!r}{src}")

    # --- Split detail ---
    if split_matches:
        print(f"\nSPLIT MATCHES ({len(split_matches)}):")
        for m in split_matches:
            print(f"  Line {m.line_no:5d}:")
            print(f"    original: {m.old_line[:100]}{'...' if len(m.old_line) > 100 else ''}")
            print(f"    heading : {m.new_line}")
            if m.body_text:
                print(f"    body    : {m.body_text[:80]}{'...' if len(m.body_text) > 80 else ''}")

    # --- Unmatched ---
    print(f"\nUNMATCHED # HEADINGS ({len(unmatched)}/{total_headings}):")
    if unmatched:
        for u in unmatched:
            text = u.line.strip() if u.line.strip() else "(bare #)"
            print(f"  Line {u.line_no:5d}: {text}")
    else:
        print("  (none)")

    # --- Skipped hierarchy ---
    print(f"\nSKIPPED HIERARCHY ENTRIES ({len(skipped)}/{total_hierarchy}):")
    MAX_SHOW = 40
    for level, text in skipped[:MAX_SHOW]:
        print(f"  {'#' * level} {text}")
    if len(skipped) > MAX_SHOW:
        print(f"  ... and {len(skipped) - MAX_SHOW} more")

    print()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_hierarchy(md_path: Path, explicit: Optional[Path] = None) -> Optional[Path]:
    if explicit is not None:
        p = Path(explicit)
        if p.is_file():
            return p
        print(f"WARNING: explicit hierarchy file not found: {p}", file=sys.stderr)
        return None
    parent = md_path.parent
    stem = md_path.stem
    if stem.endswith("_fixed"):
        stem = stem[: -len("_fixed")]
    for ext in (".md", ".txt"):
        candidate = parent / f"{stem}_hierarchy{ext}"
        if candidate.is_file():
            return candidate
    return None


def collect_pairs(
    target: Path, hierarchy_arg: Optional[Path]
) -> List[Tuple[Path, Path]]:
    pairs: List[Tuple[Path, Path]] = []
    if target.is_file():
        h = find_hierarchy(target, hierarchy_arg)
        if h is None:
            print(f"ERROR: no hierarchy file found for {target}", file=sys.stderr)
        else:
            pairs.append((target, h))
    elif target.is_dir():
        for md in sorted(target.glob("*.md")):
            if "_hierarchy" in md.stem or md.stem.endswith("_fixed"):
                continue
            h = find_hierarchy(md, None)
            if h is not None:
                pairs.append((md, h))
            else:
                print(f"SKIP: no hierarchy file for {md.name}", file=sys.stderr)
    else:
        print(f"ERROR: {target} is neither a file nor a directory", file=sys.stderr)
    return pairs


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fix heading levels in MinerU-generated Markdown using a hierarchy reference."
    )
    parser.add_argument(
        "target", type=Path,
        help="Path to a .md file or a directory containing .md files.",
    )
    parser.add_argument(
        "--hierarchy", type=Path, default=None,
        help="Explicit path to the hierarchy file (auto-discovered if omitted).",
    )
    parser.add_argument(
        "--inplace", action="store_true",
        help="Overwrite the original .md file instead of writing *_fixed.md.",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.85,
        help="Fuzzy-match threshold (0-1). Default 0.85.",
    )
    args = parser.parse_args()

    pairs = collect_pairs(args.target, args.hierarchy)
    if not pairs:
        print("Nothing to process.", file=sys.stderr)
        sys.exit(1)

    for md_path, h_path in pairs:
        hierarchy = load_hierarchy(h_path)
        output_lines, matches, unmatched, skipped = fix_headings(
            md_path, hierarchy, threshold=args.threshold,
        )

        total_headings = len(matches) + len(unmatched)

        if args.inplace:
            out_path = md_path
        else:
            out_path = md_path.with_stem(md_path.stem + "_fixed")

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(output_lines) + "\n")

        print_report(
            md_path.name, total_headings, matches, unmatched, skipped, len(hierarchy),
        )
        print(f"Output written to: {out_path}")


if __name__ == "__main__":
    main()

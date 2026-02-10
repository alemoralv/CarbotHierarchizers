#!/usr/bin/env python3
"""
LSAR_fix_headings.py

Fixes heading levels in MinerU-generated Markdown files using a reference
hierarchy file that contains the correct nesting depth for each heading.

Adapted specifically for the LSAR (Ley de los Sistemas de Ahorro para el
Retiro) document, which has the following structural patterns:
  - Hierarchy entries have descriptive suffixes after em-dashes
    (e.g. "Artículo 1o — Objeto de la Ley") that the MD text doesn't have.
  - Articles use ordinal markers: "Artículo 1o.-", "Artículo 2o.-".
  - bis/ter/quáter/quinquies article variants (e.g. "Artículo 18 bis").
  - Roman numeral fractions (I, II, ..., XXVIII) standalone and with inline text.
  - Roman bis fractions like "III bis.", "IX Bis.".
  - Lettered incisos "a)", "b)" with parenthesis and "a.", "b." with period.
  - Section headings ("Sección I De la Comisión") as plain text.
  - CAPITULO headings.
  - Derogated articles: "Artículo 109.- (Se deroga)".
  - Complex TRANSITORIOS section with multiple reform decree blocks.
  - Decree titles using "LSAR" abbreviation vs full law name in MD.
  - DOF-dated decree references in hierarchy.

Handles three cases:
  1. Lines that already have # but the wrong level  → fix the level.
  2. Plain-text lines that match a hierarchy entry   → add the correct # level.
  3. Lines where heading text is merged with body    → split + add heading level.

Algorithm: two-pointer sequential matching.  Both files follow the same
document order, so we walk through the .md file and advance a pointer through
the hierarchy file, matching headings by normalised text (exact first,
prefix match for em-dash entries, then fuzzy fallback for # headings only).

Usage:
    python LSAR_fix_headings.py inputpdfs/LSAR.md --hierarchy inputpdfs/LSAR_hierarchy.md
    python LSAR_fix_headings.py inputpdfs/LSAR.md                  # auto-discover hierarchy
    python LSAR_fix_headings.py inputpdfs/                         # batch mode
    python LSAR_fix_headings.py inputpdfs/LSAR.md --inplace
    python LSAR_fix_headings.py inputpdfs/LSAR.md --threshold 0.90
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
    strip trailing punctuation.  Em-dashes/en-dashes become spaces so that
    hierarchy entries like 'Sección I — De la Comisión' match plain-text
    'Sección I De la Comisión'."""
    text = text.lower()
    # Replace em-dashes and en-dashes with spaces (before whitespace collapse)
    text = text.replace("\u2014", " ").replace("\u2013", " ")
    # NFKD decomposition then drop combining marks (accents)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    # strip all quote characters so 'ANEXO "C"' and 'ANEXO C' match
    text = text.translate(_QUOTE_MAP)
    text = re.sub(r"""[\"'`]""", "", text)
    # collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Iteratively strip trailing punctuation and footnote markers.
    # Handles cascading cases like "ANEXO B2 63" -> "ANEXO B2" -> "ANEXO B"
    # and "RETIRO. 56" -> "RETIRO." -> "RETIRO".
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"[\.\-;]+$", "", text).strip()
        text = re.sub(r"\.\d+$", "", text).strip()
        text = re.sub(r"(?<=\D)\d{1,2}$", "", text).strip()
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

# Each hierarchy entry: (level, raw_text, norm_text, abbrev_norm, prefix_norm)
# abbrev_norm is the short form for Roman/Inciso entries, else None.
# prefix_norm is the text before " — " (em-dash suffix), else None.
HEntry = Tuple[int, str, str, Optional[str], Optional[str]]


def _compute_abbreviation(norm_text: str) -> Optional[str]:
    """Extract abbreviated form from hierarchy entries.

    RI hierarchy has entries like:
      'i. administradoras'                  -> 'i'
      'xxxiii bis. error de seguimiento'    -> 'xxxiii bis'
      'xxxa. empresas publicas del estado'  -> 'xxxa'
      'a) oferta publica en pais elegible'  -> 'a)'

    Also supports CUF-style prefixes:
      'fraccion i'  -> 'i'
      'inciso a)'   -> 'a)'
    """
    # Roman numeral (possibly with trailing letter or BIS/TER) followed by ". description"
    m = re.match(
        r"^([ivxlcdm]+[a-z]?(?:\s+(?:bis|ter|quater|quinquies|sexies|septies))?)\.\s+",
        norm_text,
    )
    if m:
        return m.group(1)

    # Inciso with description: "a) text" -> "a)"
    m = re.match(r"^([a-z]\))\s+", norm_text)
    if m:
        return m.group(1)

    # Inciso with period: "a. description" -> "a."
    m = re.match(r"^([a-z]\.)\s+", norm_text)
    if m:
        return m.group(1)

    # CUF-style abbreviations (fraccion/inciso prefix)
    for prefix in ("fraccion ", "inciso "):
        if norm_text.startswith(prefix):
            return norm_text[len(prefix):]

    return None


def _compute_prefix_from_raw(raw_text: str) -> Optional[str]:
    """Extract prefix before em-dash/en-dash separator from the *raw* text.

    Since normalize() converts em-dashes to spaces, we must split on the
    raw text and then normalize the prefix portion.

    Hierarchy entries like 'Artículo 1o — Objeto de la Ley' → prefix
    normalize('Artículo 1o') = 'articulo 1o'.
    Returns None if there is no dash separator.
    """
    for sep in (" \u2014 ", " \u2013 ", " -- "):
        if sep in raw_text:
            prefix = raw_text.split(sep, 1)[0].strip()
            if prefix:
                return normalize(prefix)
    return None


def _compute_prefix_norm(norm_text: str) -> Optional[str]:
    """Extract prefix by stripping known suffixes from the normalised text.

    Handles:
      - Derogated articles: 'articulo 109 (se deroga)' → 'articulo 109'
      - DOF-dated decrees:  'decreto ... (dof 23-01-1998)' → 'decreto ...'
    Returns None if no recognised suffix is found.
    """
    # (Se deroga) suffix
    m = re.match(r"^(.+?)\s*\(se deroga\)\s*$", norm_text)
    if m:
        prefix = m.group(1).strip()
        if prefix and prefix != norm_text:
            return prefix
    # DOF date suffix  (dof dd-mm-yyyy)
    m = re.match(r"^(.+?)\s*\(dof\s+[\d-]+\)\s*$", norm_text)
    if m:
        prefix = m.group(1).strip()
        if prefix and prefix != norm_text:
            return prefix
    return None


def load_hierarchy(path: Path) -> List[HEntry]:
    """Read hierarchy file → [(level, raw_text, norm_text, abbrev_norm, prefix_norm), ...]."""
    entries: List[HEntry] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parsed = parse_heading(line.rstrip("\n"))
            if parsed:
                level, text = parsed
                if text:
                    norm = normalize(text)
                    abbrev = _compute_abbreviation(norm)
                    # Prefix from em-dash separator (uses raw text)
                    prefix = _compute_prefix_from_raw(text)
                    # Fallback: prefix from suffix patterns (uses normalised text)
                    if prefix is None:
                        prefix = _compute_prefix_norm(norm)
                    entries.append((level, text, norm, abbrev, prefix))
    return entries


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

_SHORT_TEXT_LEN = 15  # short texts use tighter fuzzy threshold

# Separator pattern: "prefix .- body"  (PRIMERA.- text..., Artículo N.- text...)
_ARTICLE_SEP_RE = re.compile(r"^(.+?)\s*\.\s*[-\u2013\u2014]\s*(.+)$")

# Roman numeral fraction: standalone like "I.", "III bis.", "XXIX.", "XXXa."
# Accepts optional trailing lowercase letter for non-standard extensions.
# Requires the trailing period so random words starting with I/V/X etc. don't match.
_ROMAN_STANDALONE_RE = re.compile(
    r"^([IVXLCDM]+[a-z]?(?:\s+(?:bis|ter|qu[a\u00e1]ter|quinquies|sexies|septies))?)\s*\.\s*$",
    re.IGNORECASE,
)

# Roman numeral with inline text: "XXXIII. Entidades Financieras, a las..."
# No dash after the period — just "ROMAN. text".
_ROMAN_WITH_TEXT_RE = re.compile(
    r"^([IVXLCDM]+[a-z]?(?:\s+(?:BIS|TER|QU[A\u00c1]TER|QUINQUIES|SEXIES|SEPTIES))?)\.\s+(.+)$",
    re.IGNORECASE,
)

# Inciso pattern: single letter followed by ) -- standalone only  (e.g. "a)" alone)
_INCISO_STANDALONE_RE = re.compile(r"^([a-zA-Z]\))\s*$")

# Inciso with inline text: "a) Ser ofertados mediante..." — letter + ) + space + text
_INCISO_WITH_TEXT_RE = re.compile(r"^([a-zA-Z]\))\s+(.+)$")

# Inciso with period — standalone: "a.", "b." (letter + period, no text)
_INCISO_DOT_STANDALONE_RE = re.compile(r"^([a-zA-Z]\.)\s*$")

# Inciso with period — inline text: "a. El adecuado conocimiento..."
_INCISO_DOT_WITH_TEXT_RE = re.compile(r"^([a-zA-Z]\.)\s+(.+)$")

# Sub-inciso pattern: Roman lowercase with ) — standalone or with text
# e.g. "i. Un bono cupón cero..." or "ii." standalone
_SUB_INCISO_STANDALONE_RE = re.compile(
    r"^([ivxlcdm]+)\.\s*$",
    re.IGNORECASE,
)


_MAX_SCAN_ABBREV = 40   # max entries to scan ahead for abbreviated/prefix matches
_MAX_SCAN_FUZZY  = 30   # max entries to scan ahead for fuzzy matches


_MAX_SCAN_EXACT = 80  # max entries to scan ahead for exact matches (0 = full scan)


def _try_match(
    md_norm: str,
    hierarchy: List[HEntry],
    start: int,
    threshold: float,
    *,
    check_abbrev: bool = False,
    allow_fuzzy: bool = True,
    max_exact_scan: int = 0,
) -> Optional[Tuple[int, str, float]]:
    """Scan hierarchy from *start* for a match against *md_norm*.

    Pass 1  — exact normalised match (full or windowed scan).
    Pass 1b — exact abbreviated match (windowed, if check_abbrev).
    Pass 1c — exact prefix match (full or windowed scan).
    Pass 1c2 — LSAR abbreviation expansion.
    Pass 1d — singular/plural variation (CONSIDERANDO ↔ CONSIDERANDOS).
    Pass 2  — fuzzy match (windowed, if allow_fuzzy).

    Abbreviated and fuzzy passes use a limited scan window to avoid
    cascade errors from short ambiguous abbreviations like 'i', 'ii', 'iii'.
    When max_exact_scan > 0, exact and prefix passes are also windowed
    to prevent separator-split plain-text matches from jumping too far.

    Returns (index, match_type, ratio) or None.
    """
    h_len = len(hierarchy)
    end_exact = min(start + max_exact_scan, h_len) if max_exact_scan > 0 else h_len

    # --- pass 1: exact on norm_text ---
    for i in range(start, end_exact):
        if md_norm == hierarchy[i][2]:
            return i, "exact", 1.0

    # --- pass 1b: exact on abbreviated form (windowed, section-bounded) ---
    # Don't let short abbreviations like "i", "ii" cross major section
    # boundaries (### or higher) to avoid jumping into the wrong section.
    if check_abbrev:
        end_abbrev = min(start + _MAX_SCAN_ABBREV, h_len)
        for i in range(start, end_abbrev):
            # Stop at major section boundaries (level ≤ 3) to prevent
            # abbreviated matches from jumping across CAPITULO/Sección sections
            if i > start and hierarchy[i][0] <= 3:
                break
            abbr = hierarchy[i][3]
            if abbr is not None and md_norm == abbr:
                return i, "exact-abbrev", 1.0

    # --- pass 1c: exact on prefix before em-dash / suffix ---
    # Uses end_exact so that plain-text separator matches don't jump too far.
    for i in range(start, end_exact):
        prefix = hierarchy[i][4]
        if prefix is not None and md_norm == prefix:
            return i, "exact-prefix", 1.0

    # --- pass 1c2: LSAR abbreviation expansion ---
    # Hierarchy uses "lsar" shorthand; MD has the full law name.
    # Try matching md_norm against hierarchy text with "lsar" expanded.
    if _LSAR_ABBREV in md_norm or len(md_norm) > 40:
        for i in range(start, end_exact):
            h_norm = hierarchy[i][2]
            if _LSAR_ABBREV in h_norm:
                expanded = _expand_lsar(h_norm)
                if md_norm == expanded:
                    return i, "exact-lsar", 1.0
            # Also try against expanded prefix
            prefix = hierarchy[i][4]
            if prefix is not None and _LSAR_ABBREV in prefix:
                expanded_prefix = _expand_lsar(prefix)
                if md_norm == expanded_prefix:
                    return i, "exact-lsar-prefix", 1.0

    # --- pass 1d: singular/plural variation ---
    # Handles CONSIDERANDO ↔ CONSIDERANDOS and similar
    variant = None
    if md_norm.endswith("o"):
        variant = md_norm + "s"
    elif md_norm.endswith("os"):
        variant = md_norm[:-1]
    if variant:
        end_var = min(start + _MAX_SCAN_ABBREV, h_len)
        for i in range(start, end_var):
            if variant == hierarchy[i][2]:
                return i, "exact-variant", 1.0
            # Also check against prefix
            prefix = hierarchy[i][4]
            if prefix is not None and variant == prefix:
                return i, "exact-variant", 1.0

    # --- pass 2: fuzzy on norm_text (windowed) ---
    if allow_fuzzy:
        eff_threshold = threshold
        if len(md_norm) < _SHORT_TEXT_LEN:
            eff_threshold = max(threshold, 0.95)
        end_fuzzy = min(start + _MAX_SCAN_FUZZY, h_len)
        for i in range(start, end_fuzzy):
            ratio = fuzzy_ratio(md_norm, hierarchy[i][2])
            if ratio >= eff_threshold:
                return i, "fuzzy", ratio
            # Also try fuzzy against prefix
            prefix = hierarchy[i][4]
            if prefix is not None:
                ratio = fuzzy_ratio(md_norm, prefix)
                if ratio >= eff_threshold:
                    return i, "fuzzy-prefix", ratio
            # Also try fuzzy with LSAR abbreviation expanded
            h_norm = hierarchy[i][2]
            if _LSAR_ABBREV in h_norm:
                expanded = _expand_lsar(h_norm)
                ratio = fuzzy_ratio(md_norm, expanded)
                if ratio >= eff_threshold:
                    return i, "fuzzy-lsar", ratio

    return None


_LSAR_ABBREV = "lsar"
_LSAR_EXPANSION = "ley de los sistemas de ahorro para el retiro"


def _expand_lsar(text: str) -> str:
    """Replace 'lsar' abbreviation with its full expansion."""
    return text.replace(_LSAR_ABBREV, _LSAR_EXPANSION)


def _is_title_match(md_norm: str, title_norm: str, threshold: float) -> bool:
    """Check if md_norm matches the document title.

    The LSAR title is straightforward — just exact, fuzzy, and long-prefix
    matching.  Also handles trailing footnote markers.
    """
    if not md_norm or not title_norm:
        return False
    if md_norm == title_norm:
        return True
    if fuzzy_ratio(md_norm, title_norm) >= threshold:
        return True

    # Long common-prefix heuristic (handles trailing footnote markers etc.)
    shorter = min(len(md_norm), len(title_norm))
    if shorter > 40:
        common = 0
        for a, b in zip(md_norm, title_norm):
            if a == b:
                common += 1
            else:
                break
        if common >= shorter * 0.75:
            return True

    return False


def _try_plain_text_match(
    stripped: str,
    hierarchy: List[HEntry],
    h_idx: int,
    threshold: float,
) -> Optional[Tuple[int, str, float, str, Optional[str]]]:
    """Try to match a plain-text line (no #) against the hierarchy.

    Strategies tried in order:
      1.  Full standalone match  — for "TÍTULO XIV", "Capítulo I", etc.
      2.  Separator split        — for "PRIMERA.- body text..."
      3a. Standalone Roman       — for "I.", "III bis.", "XXXa."
      3b. Roman + inline text    — for "XXXIII. Entidades Financieras, ..."
      4a. Standalone Inciso      — for "a)", "b)"
      4b. Inciso + inline text   — for "a) Ser ofertados mediante..."
      5.  Standalone sub-inciso  — for "i.", "ii." (lowercase roman)

    Returns (h_index, match_type, ratio, heading_text, body_text) or None.
    body_text is None when no split is needed.
    Plain-text matching uses exact + abbreviated only (no fuzzy) to
    avoid false positives on regular body lines.
    """
    norm = normalize(stripped)
    if not norm:
        return None

    # --- Strategy 1: full standalone match ---
    # Limit raised to 600 for LSAR: decree titles and long law names in the
    # reform section can exceed 200 chars but are still structural elements.
    # All plain-text strategies use windowed exact scan to prevent short
    # ambiguous texts (I, II, etc.) from matching distant hierarchy entries.
    if len(stripped) < 600:
        result = _try_match(
            norm, hierarchy, h_idx, threshold,
            check_abbrev=True, allow_fuzzy=False,
            max_exact_scan=_MAX_SCAN_EXACT,
        )
        if result:
            return (*result, stripped, None)

    # --- Strategy 2: separator split  (prefix .- body) ---
    # Handles "Artículo 1o.- text", "PRIMERA.- text", "I.- text", etc.
    # Uses windowed exact scan to prevent false matches like
    # "ARTICULO PRIMERO" in the preamble jumping to TRANSITORIOS.
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
                    max_exact_scan=_MAX_SCAN_EXACT,
                )
                # Backward retry for article-level separator splits:
                # If the prefix looks like an article ("articulo NNN"),
                # retry from 0 to recover from cascade errors.
                if result is None and h_idx > 0 and re.match(
                    r"^articulo\s+\d", prefix_norm
                ):
                    result = _try_match(
                        prefix_norm, hierarchy, 0, threshold,
                        check_abbrev=False, allow_fuzzy=False,
                        max_exact_scan=0,  # full scan for retry
                    )
                if result:
                    return (*result, prefix, body)

    # --- Strategy 3a: standalone Roman numeral fraction ---
    # Only matches lines that are JUST a numeral + period: "I.", "III bis.", "XXXa."
    m = _ROMAN_STANDALONE_RE.match(stripped)
    if m:
        numeral = m.group(1).strip()
        numeral_norm = normalize(numeral)
        if numeral_norm:
            result = _try_match(
                numeral_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
                max_exact_scan=_MAX_SCAN_EXACT,
            )
            if result:
                heading = numeral + "."
                return (*result, heading, None)

    # --- Strategy 3b: Roman numeral + inline text (no dash) ---
    # Matches "XXXIII. Entidades Financieras, a las autorizadas..."
    # Splits into heading "XXXIII" and body text.
    m = _ROMAN_WITH_TEXT_RE.match(stripped)
    if m:
        numeral = m.group(1).strip()
        body = m.group(2).strip()
        if numeral and body:
            numeral_norm = normalize(numeral)
            if numeral_norm:
                result = _try_match(
                    numeral_norm, hierarchy, h_idx, threshold,
                    check_abbrev=True, allow_fuzzy=False,
                    max_exact_scan=_MAX_SCAN_EXACT,
                )
                if result:
                    heading = numeral + "."
                    return (*result, heading, body)

    # --- Strategy 4a: standalone Inciso ---
    # Only matches lines that are JUST "a)", "b)", etc.
    m = _INCISO_STANDALONE_RE.match(stripped)
    if m:
        letter = m.group(1).strip()
        letter_norm = normalize(letter)
        if letter_norm:
            result = _try_match(
                letter_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
                max_exact_scan=_MAX_SCAN_EXACT,
            )
            if result:
                return (*result, letter, None)

    # --- Strategy 4b: Inciso + inline text ---
    # Matches "a) Ser ofertados mediante un mecanismo..."
    # Splits into heading "a)" and body text.
    m = _INCISO_WITH_TEXT_RE.match(stripped)
    if m:
        letter = m.group(1).strip()
        body = m.group(2).strip()
        if letter and body:
            letter_norm = normalize(letter)
            if letter_norm:
                result = _try_match(
                    letter_norm, hierarchy, h_idx, threshold,
                    check_abbrev=True, allow_fuzzy=False,
                    max_exact_scan=_MAX_SCAN_EXACT,
                )
                if result:
                    return (*result, letter, body)

    # --- Strategy 5: standalone sub-inciso (lowercase roman numeral) ---
    # Matches lines that are JUST "i.", "ii.", "iii." etc.
    m = _SUB_INCISO_STANDALONE_RE.match(stripped)
    if m:
        numeral = m.group(1).strip()
        numeral_norm = normalize(numeral)
        if numeral_norm:
            result = _try_match(
                numeral_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
                max_exact_scan=_MAX_SCAN_EXACT,
            )
            if result:
                heading = numeral + "."
                return (*result, heading, None)

    # --- Strategy 6a: standalone Inciso with period ---
    # Only matches lines that are JUST "a.", "b.", etc.
    m = _INCISO_DOT_STANDALONE_RE.match(stripped)
    if m:
        letter_dot = m.group(1).strip()
        letter_dot_norm = normalize(letter_dot)
        if letter_dot_norm:
            result = _try_match(
                letter_dot_norm, hierarchy, h_idx, threshold,
                check_abbrev=True, allow_fuzzy=False,
                max_exact_scan=_MAX_SCAN_EXACT,
            )
            if result:
                return (*result, letter_dot, None)

    # --- Strategy 6b: Inciso with period + inline text ---
    # Matches "a. El adecuado conocimiento de sus clientes..."
    # Splits into heading "a." and body text.
    m = _INCISO_DOT_WITH_TEXT_RE.match(stripped)
    if m:
        letter_dot = m.group(1).strip()
        body = m.group(2).strip()
        if letter_dot and body:
            letter_dot_norm = normalize(letter_dot)
            if letter_dot_norm:
                result = _try_match(
                    letter_dot_norm, hierarchy, h_idx, threshold,
                    check_abbrev=True, allow_fuzzy=False,
                    max_exact_scan=_MAX_SCAN_EXACT,
                )
                if result:
                    return (*result, letter_dot, body)

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
        self.match_type = match_type   # "exact"|"exact-abbrev"|"exact-prefix"|"exact-variant"|"fuzzy"|"fuzzy-prefix"
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

            # TOC/body reset — use the special title matcher for SIEFORES
            if title_norm and title_seen and h_idx > 0:
                if _is_title_match(md_norm, title_norm, threshold):
                    h_idx = 0

            # Special handling for title (entry 0) — SIEFORES expansion
            result = None
            if h_idx == 0 and not title_seen and hierarchy:
                if _is_title_match(md_norm, title_norm, threshold):
                    result = (0, "exact-title", 1.0)

            if result is None:
                result = _try_match(
                    md_norm, hierarchy, h_idx, threshold,
                    check_abbrev=True, allow_fuzzy=True,
                )

            # Backward retry: if the forward scan from h_idx failed,
            # try from the beginning.  This handles cascade errors where
            # h_idx overshoots past section headings (Sección, CAPITULO).
            if result is None and h_idx > 0:
                result = _try_match(
                    md_norm, hierarchy, 0, threshold,
                    check_abbrev=False, allow_fuzzy=True,
                )

            # Fallback: try plain-text strategies on the heading text.
            # Handles cases like "# XXXb. Emisora Simplificada, ..." where
            # the heading has a Roman numeral prefix with inline body text.
            body_from_heading = None
            if result is None:
                pt_fallback = _try_plain_text_match(
                    md_text, hierarchy, h_idx, threshold,
                )
                if pt_fallback:
                    fi, fmt, frat, fhead, fbody = pt_fallback
                    result = (fi, fmt, frat)
                    body_from_heading = fbody
                    md_text = fhead  # use extracted heading text

            if result:
                i, match_type, ratio = result
                new_line = "#" * hierarchy[i][0] + " " + md_text
                source = "heading"
                if body_from_heading:
                    source = "split"
                matches.append(MatchRecord(
                    line_no, raw_line, new_line, match_type, ratio, source,
                    body_from_heading,
                ))
                matched_h.add(i)
                output.append(new_line)
                if body_from_heading:
                    output.append("")
                    output.append(body_from_heading)
                # Only advance h_idx forward, never backward
                if i >= h_idx:
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

        # TOC/body reset on plain-text title — use the special title matcher
        pt_norm = normalize(stripped)
        if title_norm and title_seen and h_idx > 0 and pt_norm:
            if _is_title_match(pt_norm, title_norm, threshold):
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
            # Only advance h_idx forward, never backward
            if i >= h_idx:
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
    fuzzy_matches   = [m for m in matches if m.match_type in ("fuzzy", "fuzzy-prefix", "fuzzy-lsar")]
    title_matches   = [m for m in matches if m.match_type == "exact-title"]
    abbrev_matches  = [m for m in matches if m.match_type == "exact-abbrev"]
    prefix_matches  = [m for m in matches if m.match_type == "exact-prefix"]
    variant_matches = [m for m in matches if m.match_type == "exact-variant"]

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
        if m.match_type in ("fuzzy", "fuzzy-prefix", "fuzzy-lsar"):
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

    # --- Prefix detail (em-dash matches) ---
    if prefix_matches:
        print(f"\nPREFIX MATCHES ({len(prefix_matches)}) — matched before em-dash:")
        for m in prefix_matches:
            src = f", {m.source}" if m.source != "heading" else ""
            print(f"  Line {m.line_no:5d}: {m.old_line.strip()[:60]!r} -> {m.new_line[:60]!r}{src}")

    # --- Variant detail (singular/plural) ---
    if variant_matches:
        print(f"\nVARIANT MATCHES ({len(variant_matches)}) — singular/plural:")
        for m in variant_matches:
            src = f", {m.source}" if m.source != "heading" else ""
            print(f"  Line {m.line_no:5d}: {m.old_line.strip()[:60]!r} -> {m.new_line[:60]!r}{src}")

    # --- LSAR abbreviation expansion detail ---
    lsar_matches = [m for m in matches if m.match_type in ("exact-lsar", "exact-lsar-prefix", "fuzzy-lsar")]
    if lsar_matches:
        print(f"\nLSAR EXPANSION MATCHES ({len(lsar_matches)}) — LSAR abbreviation expanded:")
        for m in lsar_matches:
            src = f", {m.source}" if m.source != "heading" else ""
            ratio_str = f", {m.ratio:.2f}" if m.match_type == "fuzzy-lsar" else ""
            print(f"  Line {m.line_no:5d}: {m.old_line.strip()[:60]!r} -> {m.new_line[:60]!r}{src}{ratio_str}")

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
        description="Fix heading levels in MinerU-generated Markdown (LSAR document) using a hierarchy reference."
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
    parser.add_argument(
        "--outdir", type=Path, default=None,
        help="Directory for output files (default: same directory as input).",
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
        elif args.outdir:
            args.outdir.mkdir(parents=True, exist_ok=True)
            out_path = args.outdir / (md_path.stem + "_fixed.md")
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

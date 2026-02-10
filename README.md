# reformatPRO

A Python toolset for fixing heading levels in Markdown files. Designed for Profuturo legal documents (Mexican retirement savings regulations), it uses reference hierarchy files to correct structural issues that arise when PDFs are converted to Markdown via OCR.

## Overview

When legal documents are converted from PDF to Markdown, heading levels are often incorrect or missing. This project provides document-specific scripts that:

1. **Fix wrong heading levels** — Lines with `#` but incorrect nesting depth
2. **Add missing headings** — Plain-text lines that match hierarchy entries get the correct `#` level
3. **Split merged content** — Lines where heading text is merged with body (e.g. `Artículo 1.- body text...`) are split and formatted correctly

The algorithm uses a two-pointer sequential matching approach: both the Markdown file and hierarchy file follow the same document order, so headings are matched by normalized text (exact first, prefix match for em-dash entries, then fuzzy fallback).

## Requirements

- Python 3.8+
- No external dependencies (uses only the standard library)

## Project Structure

```
reformatPRO/
├── inputpdfs/           # Input Markdown files and hierarchy references
│   ├── CUF.md
│   ├── CUF_hierarchy.md
│   ├── LSAR.md
│   ├── LSAR_hierarchy.md
│   ├── RI.md
│   └── RI_hierarchy.md
├── outputpdfs/          # Output fixed Markdown files
├── CUF_fix_headings.py  # CUF (Disposiciones de carácter general)
├── LSAR_fix_headings.py # LSAR (Ley de los Sistemas de Ahorro para el Retiro)
├── RI_fix_headings.py   # RI (Régimen de Inversión)
└── README.md
```

## Document Types

Each script is tailored to the structural patterns of its document type:

| Script | Document | Notable patterns |
|--------|----------|------------------|
| `CUF_fix_headings.py` | Disposiciones de carácter general | Fracciones (I, III bis), Incisos (a), b)), abbreviated forms |
| `LSAR_fix_headings.py` | Ley de los Sistemas de Ahorro para el Retiro | Artículos with ordinals (1o, 2o), em-dash suffixes, Secciones, TRANSITORIOS, derogated articles |
| `RI_fix_headings.py` | Régimen de Inversión | Ordinal dispositions (PRIMERA, SEGUNDA), Roman numerals with inline text, CONSIDERANDO(S) |

## Usage

### Single file

```bash
# Auto-discover hierarchy (expects <name>_hierarchy.md next to <name>.md)
python LSAR_fix_headings.py inputpdfs/LSAR.md

# Explicit hierarchy path
python LSAR_fix_headings.py inputpdfs/LSAR.md --hierarchy inputpdfs/LSAR_hierarchy.md
```

### Batch mode (process all .md files in a directory)

```bash
python LSAR_fix_headings.py inputpdfs/
```

### Options

| Option | Description |
|--------|-------------|
| `--hierarchy PATH` | Explicit path to hierarchy file (auto-discovered if omitted) |
| `--inplace` | Overwrite the original file instead of writing `*_fixed.md` |
| `--threshold FLOAT` | Fuzzy-match threshold 0–1 (default: 0.85) |
| `--outdir PATH` | Output directory (default: same as input) |

### Examples

```bash
# Fix LSAR and write to outputpdfs/
python LSAR_fix_headings.py inputpdfs/LSAR.md --outdir outputpdfs/

# Fix in place (overwrite original)
python LSAR_fix_headings.py inputpdfs/LSAR.md --inplace

# Looser fuzzy matching
python LSAR_fix_headings.py inputpdfs/LSAR.md --threshold 0.90
```

## Hierarchy File Format

Hierarchy files are Markdown files where each heading defines the document structure. The number of `#` characters indicates the nesting level:

```markdown
# Document Title

## CAPITULO I — Disposiciones Preliminares
### Artículo 1o — Objeto de la Ley
### Artículo 2o — CONSAR como órgano regulador
#### I. Administradora
#### II. Base de Datos Nacional SAR
##### a) Carteras de inversión
##### b) Adquisición de valores extranjeros
```

Hierarchy files must be placed next to the source Markdown with the naming convention `<basename>_hierarchy.md` (e.g. `LSAR_hierarchy.md` for `LSAR.md`).

## Output

By default, each script writes to `<basename>_fixed.md` in the same directory as the input (or to `--outdir` if specified). A summary report is printed showing matched headings, unmatched entries, and skipped lines.

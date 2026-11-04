# Repository Guidelines

## Project Structure & Module Organization

This repository stores shipment, export, and customs documentation rather than application source code.

- `input/整机/` and `input/配件/`: incoming documents for complete machines and parts, including email (`.msg`) and spreadsheet (`.xlsx`) files.
- `output/整机/` and `output/配件/`: prepared upload workbooks and export ledger documents.
- `database/`: reference workbooks, including the HS code review table.
- `整机/`: supporting materials such as invoices, packing lists, ledger header images, and freight notes.
- `配件/`: a top-level directory reserved for parts-related supporting materials.

Keep incoming records, reference data, and prepared outputs in their respective directories.

## Build, Test, and Development Commands

No build system, dependency manifest, automated tests, or development server is currently present.

- `git status --short`: review pending changes before committing.
- `git diff --stat`: inspect the scope of tracked changes; spreadsheet contents require separate review.
- `git ls-files`: list tracked repository files.

Open workbooks in a spreadsheet application to inspect and validate them. Do not assume that a successful file save validates the data.

## File Style & Naming Conventions

Preserve existing Chinese directory names and descriptive filenames. Retain shipment identifiers and reference dates when naming new documents. Quote paths containing spaces in shell commands.

Keep workbook sheet names, column order, formulas, and formatting compatible with their intended upload templates. Store completed documents under `output/` without replacing source records in `input/`. No code formatter, linter, or indentation convention is configured.

## Validation Guidelines

Before submitting a workbook change, verify required fields, shipment identifiers, quantities, units, totals, and applicable HS codes against the source documents and reference workbook. Check formulas for errors and confirm that the saved file reopens correctly. Describe the checks performed in the pull request; no automated coverage target exists.

## Commit & Pull Request Guidelines

The repository has no commits yet, so no established commit convention exists. Use concise, imperative messages, such as `Add parts export ledger for shipment BEECS-6613427`.

Pull requests should describe the affected shipment, changed files, source references, and validation performed. Include screenshots when layout changes need visual review. Review documents for sensitive commercial or contact information before committing, and exclude local metadata such as `.DS_Store`.

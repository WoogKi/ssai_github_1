# SSAI Documentation Guide

## Purpose

This directory keeps SSAI/SIMS AI project documents. Markdown (`.md`) files are
the source of truth. Generated files such as DOCX, PDF, XLSX, CSV, PNG, ZIP, and
logs are distribution or runtime artifacts and should not be committed unless a
separate release policy explicitly allows them.

## Current Official Documents

- Roadmap: `docs/00_roadmap/SIMS_AI_PLATFORM_MASTER_ROADMAP_v2.2_20260719.md`
- Schedule: `docs/00_roadmap/SIMS_AI_PLATFORM_EXPECTED_SCHEDULE_v1.1_20260719.md`
- Dashboard Lite design: `docs/02_design/DASHBOARD_LITE_V01_DESIGN.md`

## Folder Policy

- `00_roadmap/`: official roadmap, schedule, and planning baseline documents.
- `01_phase_reports/`: phase close reports, runbooks, and follow-up TODOs.
- `02_design/`: design documents for upcoming features and UX changes.
- `03_specs/`: functional or technical specifications when they are separated
  from design documents.
- `04_runbooks/`: operational runbooks that remain current outside a phase
  report.
- `05_tests/`: curated test plans and manually reviewed test summaries.
- `90_archive/`: previous versions, superseded documents, and historical
  references.

## Versioning Rules

- Keep the latest official document in the active folder.
- Move previous versions to `90_archive/` when a newer official baseline
  replaces them.
- Do not keep duplicate latest documents in multiple folders.
- Prefer clear version and date suffixes for official baselines.

## Git Hygiene

- Commit Markdown source documents.
- Do not commit logs, temporary execution results, local exports, downloads,
  uploaded files, or cache files.
- Do not commit secrets, DB passwords, real connection strings, API tokens, or
  customer data.
- If a document must show configuration, use masked examples rather than real
  values.

# Documentation Index

Use this directory as the first stop when onboarding to the codebase.

## Core Docs

- `../README.md`: canonical architecture, endpoint contract, and local run commands.
- `ARCHITECTURE.md`: consolidated system architecture (C4-style, diagrams, data model, workflows).
- `ARCHITECTURE_RESET.md`: corrected domain model and pipeline semantics (supersedes drifted v3.3 assumptions).
- `SETUP_TROUBLESHOOTING.md`: setup issues seen in local/dev environments and verified fixes.
- `PROJECT_NAVIGATION.md`: where to make changes for common engineering tasks.

## Suggested Reading Order For New Contributors

1. `../README.md`
2. `ARCHITECTURE.md`
3. `SETUP_TROUBLESHOOTING.md`
4. `PROJECT_NAVIGATION.md`

## Keeping Docs Accurate

- If a PR changes setup behavior, endpoint contracts, service ownership, or startup steps, update `../README.md` and the relevant file under `docs/` in the same PR.
- New setup warnings or failures that block/confuse onboarding go in `SETUP_TROUBLESHOOTING.md` with symptom, root cause, fix, and a short verification checklist.
- New major modules, services, queues, or route groups should be reflected in `PROJECT_NAVIGATION.md`.
- Prefer canonical route and service names from `../README.md`. Remove stale aliases when encountered.
- Keep sections short, actionable, and copy-paste ready.

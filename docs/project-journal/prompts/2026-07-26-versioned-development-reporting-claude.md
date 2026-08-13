# Claude Prompt — Versioned Development Reporting System

Prepared on 2026-07-26. Copy the complete prompt below into Claude.

---

Continue working in the existing project:

`C:\Users\birka\Desktop\youtube automation`

Implement a local, version-based development reporting system inside this
repository. The product owner wants every meaningful revision to have an
auditable record showing exactly what changed, what was verified, what remains
and how the repository differs from the preceding reported version.

Do not stop at a written proposal. Implement and test the reporting system,
then create the first baseline report from the project's current independently
verified state.

## 1. Read the current source of truth first

Before changing files, read the complete current versions of:

- `docs/project-journal/README.md`
- `docs/project-journal/2026-07-26-session.md`
- `docs/project-journal/2026-07-26-preflight-session.md`
- `docs/pre-alpha-local/00-index.md`
- `docs/pre-alpha-local/08-implementation-milestones.md`
- `docs/pre-alpha-local/09-development-workflow.md`
- `.gitignore`
- `pyproject.toml`
- `content_studio/settings.py`
- `studio/models.py`
- `studio/preflight.py`
- `studio/worker.py`
- `studio/migrations/`
- `tests/`

Inspect the current repository tree and Git status as well.

Important current facts:

- This is a native Windows 11, Python 3.12, Django 5.2 project.
- The repository has no remote.
- A Git baseline commit does not exist because user name/email are not
  configured. Do not invent an identity, change Git configuration or create a
  commit.
- All repository files currently appear untracked, so Git cannot yet be the
  only source for before/after comparisons.
- The latest independently verified application result is:
  - Ruff passed.
  - Django system check passed.
  - Migration drift check reported no changes.
  - pytest: 62 passed.
  - migration
    `studio/migrations/0003_job_attempt_count_job_cancel_requested_and_more.py`
    is applied.
- The bounded sequential source-preflight worker is implemented.
- No AI model was loaded or downloaded.
- No real source-preflight job was run because only about 3.7 GB GPU VRAM was
  free at the final inspection; the approved threshold is 8 GB.
- No source video was modified.

Do not present any unverified claim as completed.

## 2. Goal and design principles

Create a reporting system that works before and after a future Git baseline
commit. It must provide:

1. A human-readable report for every reported version.
2. A machine-readable index of all versions.
3. A deterministic file snapshot for each version.
4. A comparison between the new snapshot and the previous reported snapshot.
5. A validation command that detects unreported repository changes.
6. A repeatable Windows-native command for creating the next report.
7. Clear workflow documentation that future Codex/Claude sessions can follow.

The system is for development history, not application user data. It must not
read or hash source videos, generated media, AI models, the runtime SQLite
database, secrets, caches or virtual-environment contents.

Prefer Python standard-library code and the existing Django management-command
framework. Do not add a dependency unless absolutely necessary.

## 3. Required directory structure

Use a clear structure under:

`docs/development-reports/`

At minimum create:

```text
docs/development-reports/
├── README.md
├── CHANGELOG.md
├── index.json
├── report-template.md
├── versions/
│   └── 0.1.0-baseline.md
└── snapshots/
    └── 0.1.0-baseline.json
```

You may add a narrowly scoped Python module and Django management commands in
`studio/` if that keeps generation and validation maintainable.

Do not place generated reports in `docs/project-journal/`. The project journal
is cross-session narrative context; development reports are the versioned,
structured revision ledger. Document this distinction.

## 4. Version rules

Use SemVer-compatible identifiers:

- baseline: `0.1.0-baseline`
- normal future examples: `0.1.1`, `0.2.0`

Rules:

- Version identifiers must be unique.
- Reports and snapshots are append-only after a version is finalized.
- Corrections to a finalized historical report require a new version that
  explicitly references the corrected version; do not silently rewrite
  history.
- `index.json` must identify the current/latest finalized version.
- Every non-baseline report must reference its preceding version.
- Timestamps must be ISO 8601 and include the `Europe/Istanbul` UTC offset.
- A report may have `draft` or `final` status.
- The strict validation command must fail if the latest report is still a
  draft.

Do not couple the version number to Django migration numbers or an eventual Git
commit hash. A report may record a Git commit hash when one exists, but it must
remain optional.

## 5. Human-readable report schema

The Markdown template and baseline report must contain explicit sections for:

- version
- status (`draft` or `final`)
- created timestamp and timezone
- previous version
- objective/scope
- executive summary
- independently verified baseline used
- files added
- files modified
- files deleted
- behavioral changes
- user-visible changes
- database/model/migration changes
- configuration/default changes
- security/privacy/source-safety impact
- tests and verification commands with exact results
- manual checks
- known limitations and unresolved issues
- compatibility or migration notes
- rollback guidance
- Git commit, if available
- snapshot identifier/hash
- next possible stage
- authorship/provenance:
  - implemented by
  - independently verified by
  - handoff claims not independently verified

Use an explicit `No changes` value where a section has no entries. Do not omit
sections silently.

The report must distinguish:

- verified results;
- implementation claims;
- planned/not-yet-run work.

## 6. Machine-readable index

`docs/development-reports/index.json` must have a documented, stable schema.
Include at least:

- schema version;
- latest finalized version;
- ordered version entries;
- version;
- status;
- created timestamp;
- previous version;
- report path;
- snapshot path;
- snapshot digest;
- concise summary;
- optional Git commit.

Validation must reject:

- malformed JSON;
- duplicate versions;
- missing report/snapshot files;
- path traversal or absolute paths in the index;
- invalid status;
- broken previous-version chain;
- multiple inconsistent “latest” values;
- digest mismatch.

Use repository-relative POSIX-style paths inside JSON even on Windows.

## 7. Deterministic snapshots

Create a deterministic snapshot format that records, for every included
repository file:

- repository-relative POSIX path;
- SHA-256 digest;
- byte size.

The snapshot file must also contain:

- schema version;
- report version;
- creation timestamp;
- included file count;
- aggregate snapshot digest;
- exclusion-policy identifier or version.

Requirements:

- Sort paths deterministically.
- Hash file bytes without modifying timestamps or contents.
- Reject symlink/reparse-point escapes outside the repository.
- Do not follow paths outside the repository root.
- Do not store absolute paths in snapshots.
- Bound file handling sensibly and stream hashes rather than reading large
  files entirely into memory.
- Snapshot generation must be read-only except for its own reporting outputs.

The exclusion policy must at least exclude:

- `.git/`
- `.venv/`
- Python caches
- pytest/Ruff caches
- runtime database files and SQLite sidecars
- `data/`
- media and generated-output directories/files
- model files
- logs
- secrets and local environment files
- Claude local permission/settings files if they can contain local-only data
- generated `docs/development-reports/versions/`
- generated `docs/development-reports/snapshots/`
- generated `docs/development-reports/index.json`
- generated `docs/development-reports/CHANGELOG.md`

Do not exclude the reporting implementation itself:

- reporting Python modules;
- management commands;
- tests;
- `docs/development-reports/README.md`;
- `docs/development-reports/report-template.md`;
- workflow documentation.

This prevents reports from recursively changing their own source snapshot while
still detecting changes to the reporting system.

Use `.gitignore` as one input where sensible, but do not trust it as the only
security/exclusion boundary.

## 8. Version comparison

When creating a non-baseline version, compare its snapshot to the previous
snapshot and derive:

- added files;
- modified files;
- deleted files;
- unchanged file count.

The generated draft report must pre-populate these exact path lists.

Do not claim semantic behavior merely from a file hash. The author must still
fill the behavioral and verification sections.

For the baseline, list every included file as baseline inventory rather than
pretending it was added by a prior version.

## 9. Management commands

Implement Windows-native, non-interactive commands with clear help output.
Suggested names:

```powershell
python manage.py create_development_report `
  --version 0.1.1 `
  --title "Short revision title"

python manage.py validate_development_reports
python manage.py validate_development_reports --strict
```

Exact names may differ only if you document a clearer choice.

Creation command requirements:

- Refuse invalid or duplicate versions.
- Require an existing finalized previous version for non-baseline reports.
- Generate the deterministic snapshot.
- Compare against the preceding snapshot.
- Create a `draft` Markdown report with path lists pre-populated.
- Update the index without discarding historical entries.
- Update `CHANGELOG.md` deterministically.
- Never overwrite a finalized version.
- Avoid partial output on failure; use temporary files plus atomic replace for
  reporting files where practical on Windows.
- Print concise structured output.

Validation command requirements:

- Validate all index, report, snapshot, digest, path and chain invariants.
- Recompute the current repository snapshot using the same exclusion policy.
- Compare it with the latest finalized snapshot.
- In strict mode, fail with a non-zero exit when:
  - current included files differ from the latest finalized snapshot;
  - the latest version is draft;
  - any schema/integrity error exists.
- Clearly list unreported added/modified/deleted paths.
- Make no filesystem changes.

Provide a safe, documented way to finalize a draft only after its report fields
and verification results have been completed. This may be a separate
`finalize_development_report` command or a carefully validated flag. Finalizing
must update the report, index and changelog consistently and must not allow
placeholder text such as `TODO`, `TBD` or unchecked template markers.

## 10. Baseline report

Create and finalize `0.1.0-baseline` from the current state before your
reporting-system changes are considered complete.

The baseline report must summarize all verified work to date, including:

- native Windows/Django application skeleton;
- local source-library registration;
- dashboard, project creation/detail and Moment Explorer screens;
- persisted AnalysisRun, HotMoment and MomentSelection behavior;
- transactional demo seeding and project/source integrity;
- authoritative selection summaries;
- local static asset routing regression protection;
- sequential source-preflight worker;
- JobCheckpoint persistence and migration 0003;
- safe source-path validation;
- native FFprobe discovery and normalized inspection;
- source size/duration/container/video-stream guards;
- generated/model storage checks;
- CUDA/free-VRAM inspection without model loading;
- checkpoint reuse and cancellation;
- honest paused-after-preflight UI;
- latest verification result of 62 passed tests before this reporting task.

Then run the full verification suite after implementing the reporting system
and record the new exact test count/result in the baseline report. The baseline
must clearly distinguish the earlier 62-test state from the final result after
new reporting tests are added.

The baseline must also record:

- real source preflight remains not run;
- Whisper and Qwen remain not loaded or downloaded;
- Git baseline commit remains unavailable;
- all Git files still appear untracked;
- current known continuation point is to wait for at least 8 GB free VRAM,
  run one real source preflight, then plan the transcript cascade.

## 11. Workflow integration

Update:

- `docs/pre-alpha-local/09-development-workflow.md`
- `docs/project-journal/README.md`

Add a concise mandatory reporting workflow:

1. Read the latest finalized development report.
2. Make and verify the scoped changes.
3. Create the next draft report.
4. Complete semantic, safety, verification and next-stage sections.
5. Finalize the report.
6. Run strict report validation.
7. End the user-facing handoff with a Turkish `Bir sonraki aşama` section.

Do not rewrite old project journals. Add a new journal entry for this reporting
system session and update the journal README to point to it.

Make it explicit that:

- a report is required for every completed development revision;
- tiny work-in-progress edits may remain unreported only while the version is
  still actively being developed;
- a revision is not considered complete until the report is finalized and
  strict validation passes;
- generated reports do not replace Git; a reviewed Git baseline commit should
  still be created later after the user supplies an identity.

## 12. Tests

Add automated tests for at least:

- deterministic file ordering;
- stable SHA-256 results;
- aggregate digest stability;
- excluded cache/database/media/secret paths;
- repository-relative POSIX paths;
- traversal/symlink escape rejection;
- baseline snapshot creation;
- next-version added/modified/deleted comparison;
- invalid version rejection;
- duplicate version rejection;
- finalized version overwrite rejection;
- malformed index rejection;
- duplicate index entry rejection;
- missing report rejection;
- missing snapshot rejection;
- snapshot digest mismatch rejection;
- broken previous-version chain rejection;
- draft rejection in strict mode;
- placeholder rejection during finalization;
- strict detection of unreported added file;
- strict detection of unreported modified file;
- strict detection of unreported deleted file;
- no filesystem writes during validation;
- management-command help/clean output where appropriate.

Tests must use temporary repositories/directories. They must not hash:

- the real `G:\Youtube kaynak videolar` library;
- `G:\AIContent`;
- `D:\AIContent\models`;
- the project `.venv`;
- the real runtime SQLite database.

## 13. Constraints

- Native Windows only; no Bash/WSL/Docker dependency.
- Do not launch `start-app.ps1`, `start-app.cmd`, Waitress or `runserver`.
- Do not run the real source-preflight job.
- Do not load or download Whisper, Qwen or any other AI model.
- Do not terminate or inspect unrelated user processes.
- Do not modify source videos or generated media.
- Do not add cloud services, telemetry or network calls.
- Do not change Git configuration.
- Do not create a Git commit.
- Preserve all existing application behavior and tests.
- Do not modify existing migrations.
- Create a new Django migration only if genuinely necessary; a documentation
  reporting system should normally require none.
- Do not include secrets, local absolute paths or source-media filenames in
  snapshots/reports unless already intentionally documented in the project
  journal.

## 14. Verification

Run:

```powershell
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
pytest
python manage.py validate_development_reports
python manage.py validate_development_reports --strict
```

Do not report completion unless every command passes.

After finalizing the baseline, strict validation must prove that the current
included repository state exactly matches `0.1.0-baseline`.

## 15. Completion report

At completion report:

- exact files added/modified/deleted;
- reporting directory structure;
- snapshot schema and exclusion policy;
- commands and examples;
- how draft/final transitions work;
- how added/modified/deleted files are detected;
- how unreported changes fail strict validation;
- baseline version and snapshot digest;
- exact baseline inventory count;
- baseline summary;
- exact test count and all verification results;
- confirmation that no model was loaded/downloaded;
- confirmation that no source video/runtime database was hashed or modified;
- confirmation that no Git configuration/commit was created;
- known limitations;
- the next possible stage.

End with a Turkish section titled:

`Bir sonraki aşama`

Implement and verify the system. Do not stop at a plan or mockup.

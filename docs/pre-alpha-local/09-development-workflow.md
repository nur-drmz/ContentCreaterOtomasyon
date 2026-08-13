# Local Pre-Alpha Development Workflow

**Status:** Approved planning baseline

## 1. Repository

- Use a local Git repository with short, descriptive commits.
- Do not configure a remote unless explicitly requested later.
- Never commit media, extracted frames, models, SQLite runtime databases,
  backups, logs, API keys or environment files.
- Treat `docs/pre-alpha-local/` as current scope and `docs/alpha/` as future
  vision.

## 2. Change discipline

- Every implementation change maps to one milestone checkbox and acceptance
  criterion.
- Keep media pipeline stages independently runnable and testable.
- Update pipeline/model/config versions whenever output semantics change.
- Preserve valid user data and unrelated local changes.
- Do not enable cloud providers, remote bind addresses or automatic deletion
  through convenience defaults.

## 3. Dependency discipline

- Use a Python lockfile after the GPU PoC establishes compatible versions.
- Pin model repository revisions, not only mutable model aliases.
- Record Windows version, NVIDIA driver, CUDA runtime, CTranslate2/PyTorch and
  FFmpeg versions in the PoC report.
- Model downloads are explicit setup actions, never hidden runtime behavior.

## 4. Verification

- Run unit tests for every change.
- Run integration tests for touched pipeline stages.
- Run the short PoC fixture after CUDA/model dependency changes.
- Run the full three-source evaluation only at milestone gates or when scoring,
  prompts, model revisions or sampling behavior changes.

## 5. Documentation updates

- Record PoC findings in this directory before changing approved defaults.
- New high-impact decisions receive an ADR.
- If Pre-Alpha scope grows, update PRD, architecture, tests and milestones in
  the same change.

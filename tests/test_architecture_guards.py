"""Guard tests against architecture regressions.

These enforce DEPENDENCY DIRECTION, not a file inventory. They deliberately avoid
hard-coding every filename so the repository stays refactorable: each test states
a rule and discovers the files it applies to.

No runtime behaviour is exercised here.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Directories that are not source for these checks.
_SKIP_PARTS = {".git", "__pycache__", "node_modules", "data", ".pytest_cache",
               ".local_runtime", "experiments"}


def _py_files(*relative_dirs):
    for rel in relative_dirs:
        base = ROOT / rel
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            yield path


def _imported_roots(file_path):
    """Top-level package names imported by `file_path`."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return set()
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: stays inside its own package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def _contains(file_path, tokens):
    text = file_path.read_text(encoding="utf-8")
    return [t for t in tokens if t in text]


# ---------------------------------------------------------------------------
# Transport must not know about the media engine
# ---------------------------------------------------------------------------

def test_api_layers_do_not_import_local_pipeline():
    """Transport and request dispatch must not import the media engine."""
    for rel in ("services/api/main.py", "creator_console/api.py"):
        assert "local_pipeline" not in _imported_roots(ROOT / rel), (
            f"{rel} imports local_pipeline; pipeline logic must not reach transport"
        )


def test_web_frontend_does_not_import_backend():
    web = ROOT / "apps" / "web" / "src"
    for path in web.rglob("*"):
        if path.suffix not in {".js", ".jsx"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert "local_pipeline" not in text, f"{path} references local_pipeline"
        assert "creator_console" not in text, f"{path} references creator_console"


def test_provider_sdk_construction_limited():
    """Provider SDK clients must come from the gateway modules, not transport."""
    tokens = ["OpenAI(", "genai.Client(", "Anthropic("]
    for rel in ("services/api/main.py", "creator_console/api.py"):
        found = _contains(ROOT / rel, tokens)
        assert not found, f"{rel} constructs a provider SDK: {found}"


# ---------------------------------------------------------------------------
# Layering: local_pipeline is the bottom of the stack
# ---------------------------------------------------------------------------

def test_local_pipeline_does_not_import_upper_layers():
    """The deterministic media engine must not depend on console/app/infra."""
    forbidden = {"creator_console", "application", "infrastructure", "services", "apps"}
    offenders = []
    for path in _py_files("local_pipeline"):
        leaked = _imported_roots(path) & forbidden
        if leaked:
            offenders.append((path.relative_to(ROOT).as_posix(), sorted(leaked)))
    assert not offenders, f"local_pipeline imports upper layers: {offenders}"


# ---------------------------------------------------------------------------
# Ports: dependency inversion must stay inverted
# ---------------------------------------------------------------------------

def test_ports_do_not_import_concrete_adapters():
    """`application/ports` must describe contracts, not depend on implementations.

    A port that imports `infrastructure` (or a concrete console class) would make
    the dependency arrow point the wrong way and defeat the whole abstraction.
    """
    forbidden = {"infrastructure", "services", "apps"}
    offenders = []
    for path in _py_files("application/ports"):
        leaked = _imported_roots(path) & forbidden
        if leaked:
            offenders.append((path.relative_to(ROOT).as_posix(), sorted(leaked)))
    assert not offenders, f"ports import concrete infrastructure: {offenders}"


def test_infrastructure_does_not_import_transport():
    """Adapters are called by upper layers; they must not call back into them."""
    forbidden = {"services", "apps"}
    offenders = []
    for path in _py_files("infrastructure"):
        leaked = _imported_roots(path) & forbidden
        if leaked:
            offenders.append((path.relative_to(ROOT).as_posix(), sorted(leaked)))
    assert not offenders, f"infrastructure imports transport: {offenders}"


# ---------------------------------------------------------------------------
# Application services: the dependency arrow must not point back
# ---------------------------------------------------------------------------

def _imported_modules(file_path):
    """Full dotted module names imported by `file_path` (not just roots)."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return set()
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: stays inside its own package
                continue
            if node.module:
                modules.add(node.module)
    return modules


def _imported_names(file_path):
    """`(module, name)` pairs for every `from module import name`.

    `from local_pipeline import script_generation` registers the PACKAGE in
    `_imported_modules`, so proving a submodule dependency needs the names.
    """
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
        return set()
    pairs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                pairs.add((node.module, alias.name))
    return pairs


def test_application_layer_does_not_import_transport_or_facade():
    """`application/` must not depend on HTTP, the frontend, the console
    package or concrete infrastructure -- ports and `local_pipeline` only."""
    forbidden = {"services", "apps", "creator_console", "infrastructure",
                 "fastapi", "starlette", "flask", "django"}
    offenders = []
    for path in _py_files("application"):
        leaked = _imported_roots(path) & forbidden
        if leaked:
            offenders.append((path.relative_to(ROOT).as_posix(), sorted(leaked)))
    assert not offenders, (
        f"application imports a forbidden layer: {offenders}; "
        "application services depend on ports and local_pipeline only"
    )


def test_ports_do_not_import_the_media_engine():
    """Ports describe contracts; `local_pipeline` types stay out of them."""
    offenders = []
    for path in _py_files("application/ports"):
        if "local_pipeline" in _imported_roots(path):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"ports import local_pipeline: {offenders}"


def test_artifact_adapter_does_not_import_the_facade():
    """The PipelineService -> EditorService -> adapter -> PipelineService cycle
    is the regression this milestone exists to prevent."""
    forbidden_prefixes = ("creator_console.pipeline_service",
                          "creator_console.api")
    offenders = []
    for path in _py_files("infrastructure", "application"):
        for module in _imported_modules(path):
            if module.startswith(forbidden_prefixes):
                offenders.append((path.relative_to(ROOT).as_posix(), module))
    assert not offenders, (
        f"adapter/application imports the facade or transport: {offenders}"
    )


def test_editor_service_takes_no_facade_owned_source_callables():
    """Milestone 3: the editor reads the source through `ports.SourceQuery`.

    Milestone 2 injected `PipelineService.source_path` / `.source_duration_sec`
    bound methods here. Re-introducing a callable parameter would hide a façade
    dependency behind a lambda, which the import guards above cannot see.
    """
    import inspect

    from application.services import EditorService

    params = set(inspect.signature(EditorService.__init__).parameters)
    forbidden = {"source_path", "source_duration", "pipeline_service", "facade"}
    assert not (params & forbidden), (
        f"EditorService regained a façade-owned dependency: {sorted(params & forbidden)}"
    )
    assert "source" in params


def test_no_application_service_takes_a_facade_owned_callable():
    """Milestone 4 rule (ADR-001): a service may not receive a callable that
    reaches back into `PipelineService`.

    Import guards cannot see a bound method passed as an argument, so the
    constructor signatures are asserted instead. `tracker_factory` is listed
    because that was the actual Milestone 3 leak.
    """
    import inspect

    from application import services

    forbidden = {"tracker_factory", "facade", "pipeline_service", "service",
                 "source_path", "source_duration"}
    offenders = []
    for name in services.__all__:
        cls = getattr(services, name)
        params = set(inspect.signature(cls.__init__).parameters)
        leaked = params & forbidden
        if leaked:
            offenders.append((name, sorted(leaked)))
    assert not offenders, (
        f"application service takes a façade-owned dependency: {offenders}"
    )


def test_application_services_do_not_depend_on_each_other_concretely():
    """Services may share a PORT (e.g. `SourceQuery`), never import a sibling.

    `AnalysisService` produces the assessment that `SourceService` reads for
    duration; importing one another would turn that into a cycle. The same
    rule is why `AnalysisService` reaches approval through the narrow
    `ApprovalInvalidator` port rather than importing `ApprovalService`.
    """
    forbidden_prefixes = ("application.services.source_service",
                          "application.services.analysis_service",
                          "application.services.editor_service",
                          "application.services.approval_service",
                          "application.services.publish_service",
                          "application.services.video_analysis_service",
                          "application.services.moment_service",
                          "application.services.transcript_service",
                          "application.services.script_service",
                          "application.services.workflow_gate")
    offenders = []
    for path in _py_files("application/services"):
        if path.name == "__init__.py":
            continue
        for module in _imported_modules(path):
            if module.startswith(forbidden_prefixes):
                offenders.append((path.name, module))
    assert not offenders, f"application services import each other: {offenders}"


def test_progress_port_has_a_real_implementation_and_consumer():
    """A port with no implementation or no consumer is dead abstraction."""
    import inspect

    from application.ports import ProgressFactory
    from application.services import AnalysisService, SourceService
    from infrastructure.progress.local import LocalProgressFactory

    assert isinstance(LocalProgressFactory.__init__, object)
    assert hasattr(LocalProgressFactory, "create")
    for cls in (SourceService, AnalysisService):
        assert "progress" in inspect.signature(cls.__init__).parameters
    assert hasattr(ProgressFactory, "create")


def test_approval_ports_have_real_implementations_and_consumers():
    """Milestone 5: the two approval-facing ports must both be wired.

    `ApprovalInvalidator` is how `AnalysisService` records that an edit
    invalidated a grant; `ReviewGate` is how `ApprovalService` consults the
    rights/review preconditions. A port with no implementation or no consumer
    is dead abstraction.
    """
    import inspect

    from application.ports import ApprovalInvalidator, ReviewGate
    from application.services import AnalysisService, ApprovalService
    from infrastructure.review.local import LocalReviewGate

    assert isinstance(LocalReviewGate.__init__, object)
    assert isinstance(AnalysisService.__init__, object)
    # The invalidator is implemented by the approval owner and consumed by the
    # editing service -- never imported as a sibling.
    assert "approvals" in inspect.signature(AnalysisService.__init__).parameters
    assert hasattr(ApprovalService, "record_invalidation")
    # The review gate is implemented by the local adapter and consumed by the
    # approval service.
    assert "review_gate" in inspect.signature(ApprovalService.__init__).parameters
    for method in ("copyright_review_state", "render_review_state",
                   "voice_review_state"):
        assert hasattr(LocalReviewGate, method)
    assert isinstance(ApprovalService(artifacts=None, audit=None, ownership=None,
                                      review_gate=None),
                      ApprovalInvalidator)


def test_publish_service_is_wired_through_ports():
    """Milestone 6: publishing consumes the approval chain read-only and the
    provider through the canonical `PublishProvider` protocol.

    `ApprovalQuery` (implemented by `ApprovalService`) is the only approval
    edge `PublishService` is allowed to have -- a bound
    `PipelineService.approval_chain` or a sibling import would hide the
    façade dependency the extraction exists to remove.
    """
    import inspect

    from application.ports import ApprovalQuery
    from application.services import ApprovalService, PublishService
    from local_pipeline import publish as publish_module

    assert "approvals" in inspect.signature(PublishService.__init__).parameters
    assert "provider" in inspect.signature(PublishService.__init__).parameters
    assert isinstance(ApprovalService(artifacts=None, audit=None, ownership=None,
                                      review_gate=None),
                      ApprovalQuery)
    for method in ("describe_credential_state", "upload_private",
                   "set_visibility"):
        assert hasattr(publish_module.PublishProvider, method)


def test_workflow_gate_port_has_a_real_implementation_and_consumer():
    """Milestone 9: future production services ask eligibility through
    `ports.WorkflowGateQuery`, never through the façade or a sibling import.

    The port must have a real implementation (`WorkflowGate`, satisfied
    structurally) and a real consumer surface (the facade delegates its
    `plan_gate`/`require_phase`/slot methods to it). A port with no
    implementation or no consumer is dead abstraction.
    """
    import inspect

    from application.ports import WorkflowGateQuery
    from application.services import WorkflowGate

    params = set(inspect.signature(WorkflowGate.__init__).parameters)
    assert params == {"self", "artifacts"}, params
    assert isinstance(WorkflowGate(artifacts=None), WorkflowGateQuery)
    for method in ("plan_gate", "require_phase", "plan_decision",
                   "slot_count", "slot_indexes", "require_slot",
                   "slot_moment_rank"):
        assert hasattr(WorkflowGateQuery, method)
        assert hasattr(WorkflowGate, method)


def test_video_analysis_service_is_wired_through_ports():
    """Milestone 10: the analysis orchestration consumes narrow ports, never
    the façade.

    `WorkflowGateQuery` (implemented by `WorkflowGate`) is the only
    eligibility edge; `SourceQuery` (implemented by `SourceService`) is the
    only media edge. A bound `PipelineService.require_phase`/`.source_path`
    injected instead would hide the façade dependency this extraction
    removes -- so the constructor names are asserted, and the module text is
    checked for a façade reference the AST import guard cannot see through
    `TYPE_CHECKING` tricks alone.
    """
    import inspect

    from application.ports import SourceQuery, WorkflowGateQuery
    from application.services import (SourceService, VideoAnalysisService,
                                      WorkflowGate)

    params = set(inspect.signature(VideoAnalysisService.__init__).parameters)
    assert params == {"self", "artifacts", "namespace", "ownership", "source",
                      "binaries", "progress", "workflow"}, params
    assert isinstance(WorkflowGate(artifacts=None), WorkflowGateQuery)
    assert isinstance(SourceService(artifacts=None, audit=None, namespace=None,
                                    ownership=None, binaries=None,
                                    safe_filename=None, new_id=None,
                                    progress=None), SourceQuery)
    text = (ROOT / "application" / "services" / "video_analysis_service.py") \
        .read_text(encoding="utf-8")
    # `PipelineServiceError` legitimately names the façade; `PipelineService`
    # itself must not (the AST guard catches imports, this catches aliases).
    import re
    assert not re.search(r"\bPipelineService\b(?!Error)", text), (
        "VideoAnalysisService references the façade")
    assert "provider.call" not in text, (
        "video analysis is local: no provider-call audit may be emitted")


def test_moment_service_is_wired_through_ports():
    """Milestone 11: moment selection + clip bounds consume narrow ports,
    never the façade.

    `WorkflowGateQuery` supplies the phase/slot gates, `SourceQuery` the source
    path/duration, `RevisionArchiver` the shared `.revisions/` invalidation
    (implemented by `LocalRevisionArchive`). A bound façade callable would hide
    the dependency this extraction removes, so the constructor names are
    asserted and the module text is checked for a façade reference the AST
    import guard cannot see through `TYPE_CHECKING` tricks alone.
    """
    import inspect
    import re

    from application.ports import (ArtifactRepository, AuditSink,
                                   RevisionArchiver, SourceQuery,
                                   WorkflowGateQuery)
    from application.services import MomentService, SourceService, WorkflowGate
    from creator_console.app import ConsoleApp
    from creator_console.faults import _ATOMIC_WRITE_HOLDERS
    from infrastructure.persistence.local import LocalRevisionArchive

    params = set(inspect.signature(MomentService.__init__).parameters)
    assert params == {"self", "artifacts", "source", "workflow", "archiver",
                      "audit"}, params
    assert isinstance(WorkflowGate(artifacts=None), WorkflowGateQuery)
    assert isinstance(SourceService(artifacts=None, audit=None, namespace=None,
                                    ownership=None, binaries=None,
                                    safe_filename=None, new_id=None,
                                    progress=None), SourceQuery)
    # The new port has a real implementation AND a real consumer.
    assert isinstance(LocalRevisionArchive(namespace=None, ownership=None),
                      RevisionArchiver)
    assert "archiver" in params
    assert issubclass(ConsoleApp.moment_service_cls, MomentService)
    # `PipelineServiceError` legitimately names the façade; `PipelineService`
    # itself must not (the AST guard catches imports, this catches aliases).
    text = (ROOT / "application" / "services" / "moment_service.py") \
        .read_text(encoding="utf-8")
    assert not re.search(r"\bPipelineService\b(?!Error)", text), (
        "MomentService references the façade")
    assert not re.search(r"\b(FastAPI|starlette|ConsoleApi)\b", text)
    # The clip_bounds write went from a module-level `atomic_write_json` in
    # pipeline_service to the repository's `write_artifact_path`; the new
    # manifest writer in revision_archive is registered for fault injection.
    assert "atomic_write_json" not in text
    assert "infrastructure.persistence.local.revision_archive" \
        in _ATOMIC_WRITE_HOLDERS
    # LocalArtifactRepository now satisfies the extended write surface.
    from infrastructure.persistence.local import LocalArtifactRepository
    assert hasattr(LocalArtifactRepository, "write_artifact_path")
    assert isinstance(LocalArtifactRepository, type)
    assert hasattr(ArtifactRepository, "write_artifact_path")
    assert hasattr(AuditSink, "record")


def test_moment_selection_has_a_single_writer():
    """Every application writer of `moment_selection` was audited: exactly one
    `write_artifact("moment_selection"` call exists, inside MomentService."""
    writers = []
    for path in _py_files("application", "creator_console", "infrastructure"):
        text = path.read_text(encoding="utf-8")
        if 'write_artifact("moment_selection"' in text \
                or "write_artifact('moment_selection'" in text:
            writers.append(path.relative_to(ROOT).as_posix())
    assert writers == ["application/services/moment_service.py"], writers


def test_transcript_service_is_wired_through_ports():
    """Milestone 12: the transcript lifecycle consumes narrow ports, never
    the façade.

    `WorkflowGateQuery` supplies `require_phase`/`require_slot`/`plan_gate`,
    `ApprovalQuery` the read-only approval `state`, `RevisionArchiver` the
    replaced-transcript snapshot and `ProgressFactory` the tracker. A bound
    façade callable would hide the dependency this extraction removes, so the
    constructor names are asserted and the module text is checked for a
    façade reference the AST import guard cannot see through `TYPE_CHECKING`.
    """
    import inspect
    import re

    from application.ports import (ApprovalQuery, ProgressFactory,
                                   RevisionArchiver, SourceQuery,
                                   WorkflowGateQuery)
    from application.services import (ApprovalService, SourceService,
                                      TranscriptService, WorkflowGate)
    from creator_console.app import ConsoleApp
    from infrastructure.persistence.local import LocalRevisionArchive
    from infrastructure.progress.local import LocalProgressFactory

    params = set(inspect.signature(TranscriptService.__init__).parameters)
    assert params == {"self", "artifacts", "source", "workflow", "approvals",
                      "archiver", "progress", "namespace", "ownership",
                      "audit"}, params
    # Every port consumed has a real implementation, satisfied structurally.
    assert isinstance(WorkflowGate(artifacts=None), WorkflowGateQuery)
    assert isinstance(SourceService(artifacts=None, audit=None, namespace=None,
                                    ownership=None, binaries=None,
                                    safe_filename=None, new_id=None,
                                    progress=None), SourceQuery)
    assert isinstance(ApprovalService(artifacts=None, audit=None,
                                      ownership=None, review_gate=None),
                      ApprovalQuery)
    assert isinstance(LocalRevisionArchive(namespace=None, ownership=None),
                      RevisionArchiver)
    assert isinstance(LocalProgressFactory(None, None), ProgressFactory)
    # The extended read surface is genuinely on the port.
    assert hasattr(ApprovalQuery, "state")
    assert hasattr(ApprovalQuery, "chain")
    assert issubclass(ConsoleApp.transcript_service_cls, TranscriptService)
    text = (ROOT / "application" / "services" / "transcript_service.py") \
        .read_text(encoding="utf-8")
    # `PipelineServiceError` legitimately names the façade; `PipelineService`
    # itself must not (the AST guard catches imports, this catches aliases).
    assert not re.search(r"\bPipelineService\b(?!Error)", text), (
        "TranscriptService references the façade")
    assert not re.search(r"\b(FastAPI|starlette|ConsoleApi)\b", text), (
        "TranscriptService references the transport layer")
    # Every transcript write goes through the repository -- no module-level
    # atomic_write_json binding to register for fault injection.
    assert "atomic_write_json" not in text
    assert "provider.call" not in text, (
        "transcription is local ASR: no provider-call audit may be emitted")


def test_moment_transcript_has_a_single_application_writer():
    """Every application writer of `moment_transcript` was audited: all
    `write_artifact("moment_transcript"` calls live inside TranscriptService."""
    import re

    writers = []
    for path in _py_files("application", "creator_console", "infrastructure"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'write_artifact\(\s*"moment_transcript"', text):
            writers.append(path.relative_to(ROOT).as_posix())
    assert writers == ["application/services/transcript_service.py"], writers


def test_script_service_is_wired_through_ports():
    """Milestone 13: the narrated script lifecycle consumes narrow ports and
    the ESTABLISHED client-factory seam, never the façade.

    `WorkflowGateQuery` supplies `require_phase("script_generation")` (which
    is what denies direct/original-audio mode), `RevisionArchiver` the
    `voice`-stage downstream snapshot, `ProgressFactory` the tracker. The
    provider clients arrive as the same injected factories the façade
    receives -- a bound `PipelineService._build_google_client` would be the
    façade-owned callable ADR-001 forbids.
    """
    import inspect
    import re

    from application.ports import (ProgressFactory, RevisionArchiver,
                                   WorkflowGateQuery)
    from application.services import ScriptService, WorkflowGate
    from creator_console.app import ConsoleApp
    from infrastructure.persistence.local import LocalRevisionArchive
    from infrastructure.progress.local import LocalProgressFactory

    params = set(inspect.signature(ScriptService.__init__).parameters)
    assert params == {"self", "artifacts", "workflow", "archiver", "progress",
                      "audit", "secrets", "google_client_factory",
                      "openai_client_factory",
                      "anthropic_client_factory"}, params
    assert isinstance(WorkflowGate(artifacts=None), WorkflowGateQuery)
    assert isinstance(LocalRevisionArchive(namespace=None, ownership=None),
                      RevisionArchiver)
    assert isinstance(LocalProgressFactory(None, None), ProgressFactory)
    assert issubclass(ConsoleApp.script_service_cls, ScriptService)
    text = (ROOT / "application" / "services" / "script_service.py") \
        .read_text(encoding="utf-8")
    # `PipelineServiceError` legitimately names the façade; `PipelineService`
    # itself must not (the AST guard catches imports, this catches aliases).
    assert not re.search(r"\bPipelineService\b(?!Error)", text), (
        "ScriptService references the façade")
    assert not re.search(r"\b(FastAPI|starlette|ConsoleApi)\b", text), (
        "ScriptService references the transport layer")
    # Every write goes through the repository: no module-level atomic writer
    # to register for fault injection.
    assert "atomic_write_json" not in text


def test_script_service_uses_the_canonical_generation_engine():
    """Prompt building, provider routing and parsing stay in `local_pipeline`.

    An AST import check proves the dependency rather than a string scan, and
    the service must not re-declare the engine's own vocabulary.
    """
    path = ROOT / "application" / "services" / "script_service.py"
    imported = _imported_names(path)
    assert ("local_pipeline", "script_generation") in imported, imported
    text = path.read_text(encoding="utf-8")
    assert "script_generation.run_script_generation(" in text
    # The cost contract lives in the engine; the service must not rebuild it.
    for forbidden in ("build_script_candidates", "build_user_prompt",
                      "parse_script_json", "GOOGLE_SCRIPT_MODELS",
                      "build_selected_script_artifact"):
        assert forbidden not in text, (
            f"ScriptService reimplements canonical generation: {forbidden}")


def test_script_generation_has_no_transcript_or_glossary_dependency():
    """Milestone 13 must not invent upstream inputs the system never had."""
    path = ROOT / "application" / "services" / "script_service.py"
    imported = _imported_names(path)
    assert ("local_pipeline", "transcript_edit") not in imported, imported
    assert ("local_pipeline", "glossary") not in imported, imported
    text = path.read_text(encoding="utf-8")
    for forbidden in ('load_artifact("moment_transcript"',
                      'load_artifact("glossary"',
                      'write_artifact("glossary"',
                      'write_artifact("moment_transcript"'):
        assert forbidden not in text, (
            f"script generation gained an upstream input it never had: {forbidden}")


def test_selected_script_has_a_single_application_writer():
    """Both audited mutation paths (generation, human edit) converge on
    ScriptService; no other module writes `selected_script`."""
    import re

    writers = []
    for path in _py_files("application", "creator_console", "infrastructure"):
        text = path.read_text(encoding="utf-8")
        if re.search(r'write_artifact\(\s*"selected_script"', text):
            writers.append(path.relative_to(ROOT).as_posix())
    assert writers == ["application/services/script_service.py"], writers


def test_no_persistent_script_candidate_artifact_was_introduced():
    """Provider candidates are in-memory routing state, not an artifact."""
    from application import artifact_catalog

    assert "script_candidates" not in artifact_catalog.ARTIFACTS
    assert not any("script_candidate" in value
                   for value in artifact_catalog.ARTIFACTS.values())


def test_source_service_does_not_probe_the_filesystem_directly():
    """Path safety belongs to the namespace store, not to the service.

    `SourceService` legitimately streams bytes with `open()`/`os.replace`, but
    it must never build namespace paths itself: every target path has to come
    from `self._namespace.resolve(...)`, which owns traversal rejection.
    """
    source = (ROOT / "application" / "services" / "source_service.py").read_text(
        encoding="utf-8")
    assert "_namespace.resolve(" in source
    for forbidden in ("Path.home(", "os.getcwd(", "os.walk(", ".expanduser("):
        assert forbidden not in source, (
            f"SourceService resolves paths on its own: {forbidden}")


# ---------------------------------------------------------------------------
# Milestone 1 scope guards: no premature backend runtime
# ---------------------------------------------------------------------------

def test_no_postgresql_or_object_storage_sdk_runtime_yet():
    """Milestone 1 creates contracts only; no DB/cloud SDK may be imported.

    This guard is scoped to the layers under active refactor, so adding a real
    adapter later means deleting or narrowing this test deliberately rather than
    drifting into a half-wired backend.
    """
    forbidden = {"sqlalchemy", "alembic", "psycopg", "psycopg2", "asyncpg",
                 "boto3", "botocore", "minio", "redis", "celery"}
    offenders = []
    for path in _py_files("application", "infrastructure"):
        leaked = _imported_roots(path) & forbidden
        if leaked:
            offenders.append((path.relative_to(ROOT).as_posix(), sorted(leaked)))
    assert not offenders, (
        f"premature backend dependency imported: {offenders}; "
        "PostgreSQL/object-storage runtime belongs to a later milestone"
    )


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_no_hardcoded_secret_assignment_in_ports_or_adapters():
    """A port or adapter must never carry a literal credential."""
    suspicious = ["api_key = \"", "api_key = '", "secret = \"", "secret = '",
                  "password = \"", "password = '", "refresh_token = \"",
                  "refresh_token = '"]
    offenders = []
    for path in _py_files("application", "infrastructure"):
        found = _contains(path, suspicious)
        if found:
            offenders.append((path.relative_to(ROOT).as_posix(), found))
    assert not offenders, f"possible hardcoded secret: {offenders}"

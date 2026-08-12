"""The lease-protected generation pipeline (spec:
decisions/package-generation-design.md, "Storage, states, CLI").

Reserves a version in a short write transaction, runs model and render work
outside any write transaction under a heartbeated lease, persists stage
progress as it goes at deterministic generation-fenced locators, and
conditionally finalizes. The heartbeat runs on its own thread for the whole
generation and is stopped and joined before any terminal transition; a
worker checks lease ownership immediately before each object write and stops
without writing when the lease is gone. A stale write can land at worst as
an inert, attributable orphan at a stale-generation locator; nothing
finalizes from it."""

import hashlib
import json
import threading
import time
from dataclasses import dataclass

from domain.ats_check import DEFAULT_PAGE_BUDGET, check_ats
from domain.context import GenerationContext
from domain.cv_model import CvModel
from domain.generation import CvDraftingService, DraftResult
from domain.grounding import GroundingVerifier
from domain.packages import FAILED, VERIFIED, LeaseLostError, PackageVersion, object_locator
from domain.ports import CvRenderer, PackageRepository, PdfTextExtractor, StorageAdapter

LEASE_SECONDS = 60
HEARTBEAT_INTERVAL_SECONDS = 10


@dataclass(frozen=True)
class PipelineResult:
    version_id: str
    status: str
    detail: str


class _Heartbeat:
    """Concurrent heartbeat thread running for the whole generation. Renewal
    is one fenced conditional update at the repository; a zero-row renewal
    permanently stops this worker (an expired lease is never resurrected)."""

    def __init__(self, repo: PackageRepository, version_id: str, owner: str, generation: int):
        self._repo = repo
        self._version_id = version_id
        self._owner = owner
        self._generation = generation
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            try:
                renewed = self._repo.renew_lease(
                    self._version_id, self._owner, self._generation, LEASE_SECONDS)
            except Exception:
                renewed = False
            if not renewed:
                self.lost.set()
                return

    def stop_and_join(self) -> None:
        self._stop.set()
        self._thread.join()


class GenerationPipeline:
    def __init__(self, repo: PackageRepository, storage: StorageAdapter,
                 renderer: CvRenderer, extractor: PdfTextExtractor,
                 drafter: CvDraftingService | None):
        self._repo = repo
        self._storage = storage
        self._renderer = renderer
        self._extractor = extractor
        self._drafter = drafter

    def generate(self, package_id: str, context: GenerationContext,
                 page_budget: int = DEFAULT_PAGE_BUDGET,
                 edited_model: CvModel | None = None) -> PipelineResult:
        """Generate the next version. edited_model bypasses the model call
        (the review write-back loop regenerates from a user-edited draft) but
        never the verifier, the render, or the ATS check."""
        owner = f"worker-{int(time.time() * 1000)}-{threading.get_ident()}"
        version = self._repo.reserve_version(package_id, owner, LEASE_SECONDS)
        heartbeat = _Heartbeat(self._repo, version.id, owner, version.lease_generation)
        heartbeat.start()
        stage_ref = ["reserve"]
        try:
            return self._run(version, owner, context, page_budget, edited_model,
                             heartbeat, stage_ref)
        except LeaseLostError:
            heartbeat.stop_and_join()
            return PipelineResult(version.id, FAILED,
                                  "lease lost; worker stopped without writing"
                                  " (recovery owns this version now)")
        except Exception as e:  # any stage error becomes a structured failure
            heartbeat.stop_and_join()
            stage = stage_ref[0]
            report = json.dumps({"stage": stage, "error": f"{type(e).__name__}: {e}"})
            try:
                self._repo.fail(version.id, owner, version.lease_generation, report)
            except Exception:
                pass  # recovery will claim the row; nothing else to write
            return PipelineResult(version.id, FAILED, f"failed at {stage}: {e}")
        finally:
            heartbeat.stop_and_join()

    def _run(self, version: PackageVersion, owner: str, context: GenerationContext,
             page_budget: int, edited_model: CvModel | None,
             heartbeat: _Heartbeat, stage_ref: list[str]) -> PipelineResult:
        repo, generation = self._repo, version.lease_generation

        def guard() -> None:
            """Lease check immediately before each object write."""
            if heartbeat.lost.is_set() or not repo.check_lease(version.id, owner, generation):
                raise LeaseLostError(f"lease lost on {version.id}")

        def locator(name: str) -> str:
            return object_locator(version.package_id, version.version, generation, name)

        stage_ref[0] = "context-snapshot"
        # Stage 1: persist the exact canonical context snapshot (locator
        # recorded deterministically before object creation).
        snapshot_locator = locator("context.json")
        guard()
        self._storage.write_text_new(snapshot_locator, context.snapshot_json())
        repo.record_progress(version.id, owner, generation,
                             context_snapshot_locator=snapshot_locator,
                             input_context_hash=context.snapshot_hash())

        stage_ref[0] = "draft"
        # Stage 2: draft and verify (model work runs outside any write txn).
        generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if edited_model is not None:
            report = GroundingVerifier(context).verify(edited_model)
            draft = DraftResult(cv=edited_model, report=report, attempts=0,
                                fallback_used=False, dropped=())
        else:
            if self._drafter is None:
                raise RuntimeError("no drafting service configured")
            draft = self._drafter.draft(context, generated_at)
        repo.record_progress(version.id, owner, generation,
                             content_model_json=draft.cv.to_json(),
                             verifier_report_json=draft.trail_json())
        if not draft.report.passed:
            failure = json.dumps({
                "stage": "verify",
                "error": "grounding verifier rejected the draft"
                         + (" (user edit ungrounded)" if edited_model is not None else
                            " after retries and fallback"),
                "findings": json.loads(draft.report.to_json())["findings"],
            })
            heartbeat.stop_and_join()
            repo.fail(version.id, owner, generation, failure)
            return PipelineResult(version.id, FAILED, "grounding verification failed")

        stage_ref[0] = "render"
        # Stage 3: render, store the artifact write-once, hash the stored
        # bytes read back at finalization.
        pdf = self._renderer.render_pdf(draft.cv)
        artifact_locator = locator("cv.pdf")
        guard()
        self._storage.write_bytes_new(artifact_locator, pdf)
        artifact_hash = hashlib.sha256(self._storage.read_bytes(artifact_locator)).hexdigest()
        repo.record_progress(version.id, owner, generation,
                             artifact_locator=artifact_locator, artifact_hash=artifact_hash)

        stage_ref[0] = "ats-check"
        # Stage 4: the mandatory separate pdftotext step.
        ats_report = check_ats(self._extractor.extract_layout(pdf), draft.cv, page_budget)
        repo.record_progress(version.id, owner, generation,
                             ats_report_json=ats_report.to_json())
        if not ats_report.passed:
            failure = json.dumps({
                "stage": "ats-check", "error": "pdftotext ATS check failed",
                "findings": json.loads(ats_report.to_json())["findings"],
            })
            heartbeat.stop_and_join()
            repo.fail(version.id, owner, generation, failure)
            return PipelineResult(version.id, FAILED, "ATS check failed")

        stage_ref[0] = "finalize"
        # Finalize: heartbeat stopped and joined first, then one atomic
        # lease-validated transition carrying the full audit bundle.
        heartbeat.stop_and_join()
        repo.finalize_verified(
            version.id, owner, generation,
            content_model_json=draft.cv.to_json(),
            context_snapshot_locator=snapshot_locator,
            input_context_hash=context.snapshot_hash(),
            verifier_report_json=draft.trail_json(),
            ats_report_json=ats_report.to_json(),
            artifact_locator=artifact_locator,
            artifact_hash=artifact_hash)
        return PipelineResult(version.id, VERIFIED,
                              "verified" + (" (verbatim fallback used)"
                                            if draft.fallback_used else ""))


def recover_expired(repo: PackageRepository, storage: StorageAdapter,
                    versions: list[PackageVersion]) -> tuple[list[str], list[str]]:
    """Startup/`package recover`: claim only rows whose lease has expired (via
    the repository compare-and-set), mark them FAILED with a structured
    interruption report, and reconcile objects by locator, including for
    versions already marked FAILED. Returns (claimed ids, orphan locators)."""
    claimed = []
    orphans = []
    report = json.dumps({"stage": "recovery",
                         "error": "generation interrupted; lease expired and was"
                                  " claimed by recovery"})
    for version in versions:
        if version.status == "GENERATING" and repo.claim_expired_and_fail(version.id, report):
            claimed.append(version.id)
    for version in versions:
        current = repo.get_version(version.id)
        referenced = {current.context_snapshot_locator, current.artifact_locator}
        for generation in range(1, current.lease_generation + 1):
            for name in ("context.json", "cv.pdf"):
                loc = object_locator(current.package_id, current.version, generation, name)
                if loc not in referenced and storage.exists(loc):
                    orphans.append(loc)
    return claimed, orphans

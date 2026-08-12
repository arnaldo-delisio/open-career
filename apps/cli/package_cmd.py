"""`open-career package generate|show|review|export|recover` (spec:
decisions/package-generation-design.md, "Storage, states, CLI").

generate: walk, generate, verify, render, ATS-check under the lease.
review: accept, or edit -> write-back loop -> regenerate; an edit the
verifier cannot ground either mints the underlying fact and regenerates, or
is dropped. There is no third path where text ships ungrounded.
export: defaults to the approved version, revalidates stored bytes against
the recorded hash. recover: claims expired leases, reconciles orphan objects
by locator."""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from adapters.storage.instance import instance_dir

from adapters.storage.family_strategy import FamilyStrategyService
from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
    SqliteRoleFamilyRepository,
)
from adapters.storage.sqlite_packages import SqlitePackageRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.families import resolve_family
from domain.context import GenerationContext, StrategySnapshot
from domain.cv_model import Bullet, CvExperienceEntry, CvModel, parse_cv_model
from domain.edges import CareerEdge
from domain.entities import CareerFact, Evidence
from domain.generation import CvDraftingService
from domain.grounding import GroundingVerifier
from domain.ids import new_id
from domain.packages import APPROVED, GENERATING, VERIFIED, PackageVersion
from domain.pipeline import GenerationPipeline, PipelineResult, recover_expired
from domain.ports import ModelAdapter, StorageAdapter
from domain.selection import FamilyEvidenceSelection
from domain.traversal import EvidenceTraversal
from prompts import load_prompt

Ask = Callable[[str], str]
Say = Callable[[str], None]


class PackageCliError(RuntimeError):
    pass


def build_context(conn: sqlite3.Connection, family_ref: str) -> GenerationContext:
    """The input gate: selection report, profile, experience rows, plus the
    immutable strategy snapshot. Nothing else enters the prompt."""
    family = resolve_family(conn, family_ref)
    if family is None:
        raise PackageCliError(f"unknown role family '{family_ref}'")
    if family.status != "active":
        raise PackageCliError(f"family '{family.name}' is {family.status}, not active")
    service = FamilyStrategyService(conn)
    objective, allocations = service.current_allocations()
    if objective is None or family.id not in allocations:
        raise PackageCliError(
            f"family '{family.name}' has no allocation in the current approved"
            " strategy version; run `open-career families init` (or edit)")
    strategy_version = SqliteStrategyVersionNumber(conn).current_version()
    edges = SqliteCareerEdgeRepository(conn)
    traversal = EvidenceTraversal(
        edges, SqliteEvidenceRepository(conn), SqliteCareerFactRepository(conn),
        SqliteExperienceRepository(conn))
    selection = FamilyEvidenceSelection(
        edges, SqliteCapabilityRepository(conn), traversal).select(family.id)
    return GenerationContext(
        role_family_id=family.id,
        selection=selection,
        profile=SqliteUserProfileRepository(conn).get_fields(),
        experiences=tuple(SqliteExperienceRepository(conn).list_all()),
        strategy=StrategySnapshot(family=family, strategy_version=strategy_version,
                                  objective=objective,
                                  allocation=allocations[family.id]))


class SqliteStrategyVersionNumber:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def current_version(self) -> int:
        row = self._conn.execute(
            "SELECT MAX(version) FROM strategy_versions WHERE user_approved = 1").fetchone()
        return row[0] or 0


def make_pipeline(conn: sqlite3.Connection, storage: StorageAdapter,
                  model: ModelAdapter | None) -> GenerationPipeline:
    from adapters.render.html_pdf import PlaywrightCvRenderer
    from adapters.render.pdftext import PopplerPdfTextExtractor

    drafter = (CvDraftingService(model, load_prompt("cv_generation.md"))
               if model is not None else None)
    return GenerationPipeline(SqlitePackageRepository(conn), storage,
                              PlaywrightCvRenderer(), PopplerPdfTextExtractor(), drafter,
                              heartbeat_repo_factory=heartbeat_repo_factory(conn))


def heartbeat_repo_factory(conn: sqlite3.Connection):
    """The heartbeat thread's repository: a dedicated connection to the same
    database file, opened inside the thread (sqlite connections are
    single-thread by default; sharing the worker's connection across threads
    raises ProgrammingError on every renewal). None for an in-memory db."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    if not db_file:
        return None

    @contextmanager
    def factory():
        heartbeat_conn = sqlite3.connect(db_file)
        try:
            yield SqlitePackageRepository(heartbeat_conn)
        finally:
            heartbeat_conn.close()

    return factory


def run_generate(conn: sqlite3.Connection, storage: StorageAdapter,
                 model: ModelAdapter | None, family_ref: str, page_budget: int,
                 say: Say, edited_model: CvModel | None = None) -> PipelineResult:
    context = build_context(conn, family_ref)
    if not context.selection.selections:
        raise PackageCliError(
            "the family targets no capabilities (confirm TARGETS via"
            " `open-career families edit`)")
    _require_renderable_experience(context)
    repo = SqlitePackageRepository(conn)
    package = repo.get_or_create_base_package(context.role_family_id)
    for gap in context.selection.gaps:
        say(f"gap: targeted capability '{gap.name}' has no eligible evidence chain")
    result = make_pipeline(conn, storage, model).generate(
        package.id, context, page_budget=page_budget, edited_model=edited_model)
    say(f"version {result.version_id}: {result.status} ({result.detail})")
    return result


def _require_renderable_experience(context: GenerationContext) -> None:
    """The husk gate: if the family's walk reaches no fact attached to an
    experience, the CV would render as a contact header plus skill names.
    Fail honestly with the missing links named instead of stamping VERIFIED
    on an empty document. Gaps beside at least one real experience entry stay
    a report, not a failure."""
    if any(fc.experience is not None
           for s in context.selection.selections for chain in s.chains
           for fc in chain.facts):
        return
    lines = ["the walk reaches no experience-backed fact; refusing to generate"
             " a CV with zero experience entries"]
    for selection in context.selection.selections:
        name = selection.capability.name
        if not selection.covered:
            lines.append(f"  uncovered capability '{name}': no eligible"
                         " evidence SUPPORTS it")
        else:
            lines.append(f"  capability '{name}': its supporting evidence proves"
                         " no fact attached to an experience")
    lines.append("link evidence that proves your confirmed experience facts to"
                 " the targeted capabilities: `open-career edges add` (SUPPORTS:"
                 " evidence -> capability), or re-run `open-career onboard`")
    raise PackageCliError("\n".join(lines))


def _version_or_die(repo: SqlitePackageRepository, version_id: str) -> PackageVersion:
    version = repo.get_version(version_id)
    if version is None:
        raise PackageCliError(f"unknown package version '{version_id}'")
    return version


def run_show(conn: sqlite3.Connection, ref: str, as_json: bool, say: Say) -> None:
    """Show a package (by package id or family) or one version (by version id):
    content, traces, reports, gaps."""
    repo = SqlitePackageRepository(conn)
    version = repo.get_version(ref)
    if version is None:
        package = repo.get_package(ref)
        if package is None:
            family = resolve_family(conn, ref)
            package = repo.get_base_package_for_family(family.id) if family else None
        if package is None:
            raise PackageCliError(f"nothing found for '{ref}'")
        versions = repo.list_versions(package.id)
        if as_json:
            say(json.dumps({
                "package": package.id, "role_family_id": package.role_family_id,
                "approved_version_id": package.approved_version_id,
                "versions": [{"id": v.id, "version": v.version, "status": v.status,
                              "created_at": v.created_at} for v in versions]}, indent=2))
            return
        say(f"package {package.id} (family {package.role_family_id},"
            f" approved: {package.approved_version_id or 'none'})")
        for v in versions:
            say(f"  v{v.version}  {v.id}  {v.status}  {v.created_at}")
        return

    payload = {
        "id": version.id, "package_id": version.package_id, "version": version.version,
        "status": version.status,
        "content_model": json.loads(version.content_model_json or "null"),
        "verifier_report": json.loads(version.verifier_report_json or "null"),
        "ats_report": json.loads(version.ats_report_json or "null"),
        "failure_report": json.loads(version.failure_report_json or "null"),
        "context_snapshot_locator": version.context_snapshot_locator,
        "artifact_locator": version.artifact_locator,
    }
    if as_json:
        say(json.dumps(payload, indent=2))
        return
    say(f"version {version.id} (v{version.version}, {version.status})")
    # Locators are storage-relative; print the real on-disk paths.
    if version.artifact_locator:
        say(f"  artifact: {instance_dir() / version.artifact_locator}")
    if version.context_snapshot_locator:
        say(f"  context snapshot: {instance_dir() / version.context_snapshot_locator}")
    cv = parse_cv_model(version.content_model_json) if version.content_model_json else None
    if cv:
        say(f"  summary: {cv.summary or '(dropped)'}")
        say(f"  skills: {', '.join(s.name for s in cv.skills) or '(none)'}")
        for entry in cv.all_entries():
            say(f"  {entry.title} @ {entry.org} [{entry.experience_id}]")
            for bullet in entry.bullets:
                say(f"    - {bullet.text}  (facts: {', '.join(bullet.fact_ids)})")
    for name in ("verifier_report", "ats_report", "failure_report"):
        if payload[name] is not None:
            say(f"  {name}: {json.dumps(payload[name])[:200]}")


def _mint_fact_for_edit(conn: sqlite3.Connection, context: GenerationContext,
                        entry: CvExperienceEntry, statement: str,
                        ask: Ask, say: Say) -> str | None:
    """The write-back loop's minting step: an interview-sourced approved fact
    with PROVES and SUPPORTS edges, reachable by this family's walk."""
    covered_and_gaps = [s.capability for s in context.selection.selections]
    if not covered_and_gaps:
        say("no targeted capabilities to attach the fact to; edit dropped")
        return None
    say("Which targeted capability does this fact support?")
    for i, capability in enumerate(covered_and_gaps):
        say(f"  [{i}] {capability.name}")
    answer = ask(f"capability index [0]: ").strip() or "0"
    if not answer.isdigit() or int(answer) >= len(covered_and_gaps):
        say("invalid index; edit dropped")
        return None
    capability = covered_and_gaps[int(answer)]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    evidence = Evidence(id=new_id("ev"), evidence_type="user_statement",
                        title=f"Package review write-back {now[:10]}")
    SqliteEvidenceRepository(conn).add(evidence)
    fact = CareerFact(id=new_id("fact"), fact_type="achievement", statement=statement,
                      source="interview", user_approved=1, verified_at=now,
                      experience_id=entry.experience_id)
    SqliteCareerFactRepository(conn).add(fact)
    edges = SqliteCareerEdgeRepository(conn)
    edges.add(CareerEdge(id=new_id("edge"), source_type="evidence", source_id=evidence.id,
                         edge_type="PROVES", target_type="career_fact", target_id=fact.id,
                         claim_kind="fact", provenance="package-review:write-back",
                         created_by="user", user_verified=1))
    edges.add(CareerEdge(id=new_id("edge"), source_type="evidence", source_id=evidence.id,
                         edge_type="SUPPORTS", target_type="capability",
                         target_id=capability.id, claim_kind="fact",
                         provenance="package-review:write-back",
                         created_by="user", user_verified=1))
    say(f"Minted fact {fact.id} under {entry.experience_id}, supporting"
        f" '{capability.name}'.")
    return fact.id


def run_review(conn: sqlite3.Connection, storage: StorageAdapter,
               model: ModelAdapter | None, version_id: str, page_budget: int,
               ask: Ask, say: Say) -> None:
    repo = SqlitePackageRepository(conn)
    version = _version_or_die(repo, version_id)
    if version.status not in (VERIFIED, APPROVED):
        raise PackageCliError(f"only a VERIFIED version can be reviewed;"
                              f" '{version_id}' is {version.status}")
    cv = parse_cv_model(version.content_model_json)
    run_show(conn, version_id, False, say)
    action = ask("accept/edit/quit (a/e/q) [a]: ").strip().lower() or "a"
    if action in ("q", "quit"):
        return
    if action in ("a", "accept"):
        repo.approve(version.id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        say(f"approved {version.id}")
        return

    # Edit: pick a bullet, replace its text, then the write-back loop.
    entries = cv.all_entries()
    bullets = [(entry, i) for entry in entries for i in range(len(entry.bullets))]
    for n, (entry, i) in enumerate(bullets):
        say(f"  [{n}] ({entry.title}) {entry.bullets[i].text}")
    answer = ask("bullet index to edit: ").strip()
    if not answer.isdigit() or int(answer) >= len(bullets):
        say("invalid index; nothing changed")
        return
    entry, i = bullets[int(answer)]
    new_text = ask("new text: ").strip()
    if not new_text:
        say("empty edit; nothing changed")
        return

    package = repo.get_package(version.package_id)
    family_ref = package.role_family_id
    context = build_context(conn, family_ref)
    edited = _with_bullet_text(cv, entry.experience_id, i, new_text)
    report = GroundingVerifier(context).verify(edited)
    if not report.passed:
        say("The edit is not grounded in approved career state:")
        for finding in report.findings[:10]:
            say(f"  [{finding.rule}] {finding.element}: {finding.message}")
        choice = ask("mint the underlying fact and regenerate, or drop the edit?"
                     " (mint/drop) [drop]: ").strip().lower()
        if choice != "mint":
            say("edit dropped; nothing ships ungrounded")
            return
        fact_id = _mint_fact_for_edit(conn, context, entry, new_text, ask, say)
        if fact_id is None:
            return
        context = build_context(conn, family_ref)  # re-walk with the new fact
        edited = _with_bullet_text(cv, entry.experience_id, i, new_text,
                                   extra_fact_id=fact_id)
        report = GroundingVerifier(context).verify(edited)
        if not report.passed:
            say("still ungrounded after minting; edit dropped:")
            for finding in report.findings[:10]:
                say(f"  [{finding.rule}] {finding.element}: {finding.message}")
            return
    say("Edit grounds; regenerating a new version with the edit applied...")
    run_generate(conn, storage, model, family_ref, page_budget, say, edited_model=edited)


def _with_bullet_text(cv: CvModel, experience_id: str, bullet_index: int, text: str,
                      extra_fact_id: str | None = None) -> CvModel:
    def patch_entries(entries):
        patched = []
        for entry in entries:
            if entry.experience_id != experience_id:
                patched.append(entry)
                continue
            bullets = list(entry.bullets)
            old = bullets[bullet_index]
            fact_ids = old.fact_ids + ((extra_fact_id,) if extra_fact_id else ())
            bullets[bullet_index] = Bullet(text=text, fact_ids=fact_ids)
            patched.append(CvExperienceEntry(**{**entry.__dict__, "bullets": tuple(bullets)}))
        return tuple(patched)

    return CvModel(header=cv.header, summary=cv.summary, skills=cv.skills,
                   experiences=patch_entries(cv.experiences),
                   projects=patch_entries(cv.projects),
                   education=patch_entries(cv.education), meta=cv.meta)


def run_export(conn: sqlite3.Connection, storage: StorageAdapter, ref: str | None,
               out: Path, say: Say) -> None:
    """Export a version's PDF; defaults to the approved version, never the
    newest. Revalidates stored bytes against the recorded hash."""
    repo = SqlitePackageRepository(conn)
    version = repo.get_version(ref) if ref else None
    if version is None and ref:
        package = repo.get_package(ref)
        if package is None:
            family = resolve_family(conn, ref)
            package = repo.get_base_package_for_family(family.id) if family else None
        if package is None:
            raise PackageCliError(f"nothing found for '{ref}'")
        if package.approved_version_id is None:
            raise PackageCliError("no approved version to export (run package review)")
        version = _version_or_die(repo, package.approved_version_id)
    if version is None:
        raise PackageCliError("export needs a version id, package id, or family")
    if version.status not in (VERIFIED, APPROVED):
        raise PackageCliError(f"version is {version.status}; nothing exportable")
    if not version.artifact_locator or not version.artifact_hash:
        raise PackageCliError("version has no stored artifact")
    pdf = storage.read_bytes(version.artifact_locator)
    digest = hashlib.sha256(pdf).hexdigest()
    if digest != version.artifact_hash:
        raise PackageCliError(
            f"stored artifact hash mismatch ({digest[:12]}... !="
            f" {version.artifact_hash[:12]}...); refusing to export")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)
    say(f"exported {version.id} ({version.status}) to {out}")


def run_recover(conn: sqlite3.Connection, storage: StorageAdapter, say: Say) -> None:
    repo = SqlitePackageRepository(conn)
    versions = [v for row in conn.execute("SELECT id FROM package_versions")
                for v in [repo.get_version(row[0])]]
    claimed, orphans = recover_expired(repo, storage, versions)
    generating = [v.id for v in (repo.get_version(v.id) for v in versions)
                  if v.status == GENERATING]
    for version_id in claimed:
        say(f"claimed expired generation {version_id}: FAILED with interruption report")
    for locator in orphans:
        say(f"orphan object (stale-generation locator, attributable, inert): {locator}")
    for version_id in generating:
        say(f"live generation left alone (unexpired lease): {version_id}")
    if not claimed and not orphans:
        say("nothing to recover")

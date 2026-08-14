"""`open-career package generate|show|review|export|recover` (spec:
decisions/package-generation-design.md, "Storage, states, CLI").

generate: walk, generate, verify, render, ATS-check under the lease.
review: accept, or edit -> write-back loop -> regenerate; an edit the
verifier cannot ground either mints the underlying fact and regenerates, or
is dropped. There is no third path where text ships ungrounded.
export: defaults to the approved version, revalidates stored bytes against
the recorded hash. recover: claims expired leases, reconciles orphan objects
by locator."""

import dataclasses
import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Callable

from adapters.storage.instance import instance_dir

from adapters.storage.sqlite_edges import SqliteCareerEdgeRepository
from adapters.storage.sqlite_entities import (
    SqliteCapabilityRepository,
    SqliteCareerFactRepository,
    SqliteEvidenceRepository,
    SqliteExperienceRepository,
    SqliteRoleFamilyRepository,
    SqliteStrategyRepository,
)
from adapters.storage.sqlite_packages import SqlitePackageRepository
from adapters.storage.sqlite_profile import SqliteUserProfileRepository
from apps.cli.families import resolve_family
from domain.context import GenerationContext, StrategySnapshot
from domain.cv_model import Bullet, CvExperienceEntry, CvModel, parse_cv_model
from domain.edges import CareerEdge
from domain.entities import CareerFact, Evidence
from adapters.storage.sqlite_conn import connect as sqlite_connect
from adapters.storage.sqlite_policies import SqliteUserPolicyRepository
from domain.gauntlet import SUITE_VERSION, GauntletRunner, GauntletRunResult
from domain.gauntlet_invariants import FAIL as FAIL_DISPOSITION
from domain.gauntlet_judges import JUDGES, PROMPT_FILES
from domain.generation import CvDraftingService, build_headline
from domain.grounding import GroundingVerifier
from domain.ids import new_id
from domain.packages import APPROVED, GENERATING, VERIFIED, PackageStateError, PackageVersion
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
    """The input gate: selection report, profile, the experience rows the
    walk actually reaches (plus confirmed education/project rows, which
    render skeleton-only), and the immutable strategy snapshot. Nothing else
    enters the prompt or the grounding corpus: an unreachable employment
    row's words are not grounding sources."""
    family = resolve_family(conn, family_ref)
    if family is None:
        raise PackageCliError(f"unknown role family '{family_ref}'")
    if family.status != "active":
        raise PackageCliError(f"family '{family.name}' is {family.status}, not active")
    # One read of the current approved StrategyVersion row: version number,
    # objective, and allocations all derive from that single row, so the
    # context can never mix two approved versions (versions are append-only
    # and a version's allocations land in the same transaction as its row).
    current = SqliteStrategyRepository(conn).current()
    allocations = ({a.role_family_id: a.allocation for a in current.allocations}
                   if current else {})
    if current is None or family.id not in allocations:
        raise PackageCliError(
            f"family '{family.name}' has no allocation in the current approved"
            " strategy version; run `open-career families init` (or edit)")
    edges = SqliteCareerEdgeRepository(conn)
    traversal = EvidenceTraversal(
        edges, SqliteEvidenceRepository(conn), SqliteCareerFactRepository(conn),
        SqliteExperienceRepository(conn))
    selection = FamilyEvidenceSelection(
        edges, SqliteCapabilityRepository(conn), traversal).select(family.id)
    reachable = {fc.fact.experience_id
                 for s in selection.selections for chain in s.chains
                 for fc in chain.facts if fc.fact.experience_id}
    return GenerationContext(
        role_family_id=family.id,
        selection=selection,
        profile=SqliteUserProfileRepository(conn).get_fields(),
        experiences=tuple(
            e for e in SqliteExperienceRepository(conn).list_all()
            if e.id in reachable or e.kind in ("project", "education")),
        strategy=StrategySnapshot(family=family, strategy_version=current.version,
                                  objective=current.objective,
                                  allocation=allocations[family.id]))


def make_pipeline(conn: sqlite3.Connection, storage: StorageAdapter,
                  model: ModelAdapter | None,
                  say: Say | None = None) -> GenerationPipeline:
    from adapters.render.html_pdf import PlaywrightCvRenderer
    from adapters.render.pdftext import PopplerPdfTextExtractor

    drafter = (CvDraftingService(model, load_prompt("cv_generation.md"))
               if model is not None else None)
    return GenerationPipeline(SqlitePackageRepository(conn), storage,
                              PlaywrightCvRenderer(), PopplerPdfTextExtractor(), drafter,
                              heartbeat_repo_factory=heartbeat_repo_factory(conn),
                              progress=(lambda line: say(f"  {line}...")) if say else None)


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
        # Through the shared connection boundary: the heartbeat writes to the
        # same instance database another session may be writing, so it needs
        # the same WAL and busy timeout every instance connection gets.
        heartbeat_conn = sqlite_connect(db_file)
        try:
            yield SqlitePackageRepository(heartbeat_conn)
        finally:
            heartbeat_conn.close()

    return factory


def run_generate(conn: sqlite3.Connection, storage: StorageAdapter,
                 model: ModelAdapter | None, family_ref: str, page_budget: int,
                 say: Say, edited_model: CvModel | None = None,
                 judge_models: dict[str, ModelAdapter] | None = None,
                 findings_from: str | None = None) -> PipelineResult:
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
    external_findings = (_blocking_findings(repo, findings_from)
                         if findings_from else ())
    say(f"generating a package for '{family_ref}' (this takes minutes: one"
        " drafting model call, then three judges)")
    result = make_pipeline(conn, storage, model, say).generate(
        package.id, context, page_budget=page_budget, edited_model=edited_model,
        external_findings=external_findings)
    say(f"version {result.version_id}: {result.status} ({result.detail})")
    # The Gauntlet runs inline immediately after finalizing VERIFIED (OC-20:
    # unattended execution); a crash in this window leaves a VERIFIED version
    # with no run, reconciled automatically by `package review` and surfaced
    # by `package show` as gauntlet: pending.
    if result.status == VERIFIED and judge_models is not None:
        version = repo.get_version(result.version_id)
        gauntlet = _run_gauntlet_attempt(conn, storage, judge_models, version, say)
        if gauntlet.verdict == "FAIL":
            for line in remedy_lines(gauntlet.run, family_ref, result.version_id):
                say(line)
    return result


# What a failed invariant actually needs from the human, by rule. Stage zero
# never judges the drafted text, so regeneration cannot fix any of these: a
# `--findings-from` re-run spends another six minutes to reach the identical
# verdict under a new version id. The remedy names the real repair instead.
def _invariant_remedies(rule: str, detail: str) -> tuple[str, ...]:
    if rule == "work-authorization":
        return (
            "  the rendered text asserts a work-authorization status that your"
            " profile does not support. Set the fields the projection is built"
            " from, then regenerate:",
            "    `open-career profile set authorized_in_country yes|no`",
            "    `open-career profile set needs_sponsorship yes|no`",
            "  (or remove the authorization sentence from the CV via"
            " `open-career package review` -> edit)")
    if rule == "date-coherence":
        return (
            "  the canonical experience dates do not cohere. Fix them at the"
            " source (the CV walk re-asks each date; '2015-09', 'September"
            " 2015' and 'present' are all understood):",
            "    `open-career onboard`",
            "  then regenerate; a redraft cannot change a canonical date.")
    if rule == "user-constraints":
        return (
            "  the CV renders a string on your never-render list. Either drop"
            " the entry from the list (`open-career policy set never_render"
            " '[...]'`) or edit the text out:",
            "    `open-career package review <version-id>` -> edit")
    if rule in ("audit-integrity", "regrounding", "artifact-recheck"):
        return (
            "  the stored audit bundle itself does not verify, so no redraft of"
            " the text can help. Generate a fresh version:",
            "    `open-career package generate <family>`")
    return (f"  no automated remedy is known for invariant '{rule}': {detail}",)


def remedy_lines(run, family_ref: str, version_id: str) -> tuple[str, ...]:
    """The repair guidance after a FAIL, specific to what actually failed.
    `--findings-from` is offered ONLY for judge findings, which are about the
    drafted text and are exactly what a regeneration can address."""
    if run is None:
        return ()
    report = json.loads(run.report_json)
    lines: list[str] = []
    for r in report.get("invariants", []):
        if r["disposition"] == FAIL_DISPOSITION:
            lines.append(f"failed invariant {r['rule']}: {r['detail']}")
            lines.extend(_invariant_remedies(r["rule"], r["detail"]))
    if any(f["severity"] == "blocking" for judge in report.get("judges", [])
           for f in judge["findings"]):
        lines.append(
            "the judges named blocking findings in the drafted text;"
            " regenerate with those failures fed back:")
        lines.append(f"  `open-career package generate {family_ref}"
                     f" --findings-from {version_id}`")
    if not lines:
        lines.append(f"the run failed ({report.get('stop_reason', 'no reason recorded')});"
                     f" inspect it with `open-career package show {version_id}`")
    return tuple(lines)


def _blocking_findings(repo: SqlitePackageRepository, version_id: str) -> tuple[str, ...]:
    """A prior version's blocking Gauntlet findings, rendered as named
    failures for the regeneration prompt."""
    version = _version_or_die(repo, version_id)
    run = repo.effective_gauntlet_run(version.id, SUITE_VERSION)
    if run is None:
        raise PackageCliError(
            f"'{version_id}' has no effective Gauntlet run under the current"
            f" suite '{SUITE_VERSION}'; run `open-career package gauntlet"
            f" {version_id}` first")
    report = json.loads(run.report_json)
    named = tuple(
        f"[{judge['judge']}/{f['element_id']}] {f['message']} (quote: '{f['quote']}')"
        for judge in report["judges"] for f in judge["findings"]
        if f["severity"] == "blocking")
    named += tuple(
        f"[invariant/{r['rule']}] {r['detail']}"
        for r in report["invariants"] if r["disposition"] == "fail")
    if not named:
        raise PackageCliError(
            f"'{version_id}' has no blocking Gauntlet findings to feed back")
    return named


# -- gauntlet ---------------------------------------------------------------

def _gauntlet_runner(conn: sqlite3.Connection, storage: StorageAdapter,
                     judge_models: dict[str, ModelAdapter],
                     say: Say | None = None) -> GauntletRunner:
    from adapters.render.pdftext import PopplerPdfTextExtractor
    from prompts import load_prompt as _load

    missing = set(JUDGES) - set(judge_models)
    if missing:
        raise PackageCliError(f"no model adapter configured for judges {sorted(missing)}")
    repo = SqlitePackageRepository(conn)
    # An in-memory database has no file to open a second connection to, so
    # the heartbeat shares this one explicitly (the runner refuses to assume
    # a shared connection on its own).
    factory = heartbeat_repo_factory(conn) or (lambda: nullcontext(repo))
    return GauntletRunner(
        repo, storage, PopplerPdfTextExtractor(),
        judge_models, {j: _load(PROMPT_FILES[j]) for j in JUDGES},
        heartbeat_repo_factory=factory,
        progress=(lambda line: say(f"  {line}...")) if say else None)


def _run_gauntlet_attempt(conn: sqlite3.Connection, storage: StorageAdapter,
                          judge_models: dict[str, ModelAdapter],
                          version: PackageVersion, say: Say) -> GauntletRunResult:
    profile = SqliteUserProfileRepository(conn).get_fields()
    policies = SqliteUserPolicyRepository(conn).get_policies()
    result = _gauntlet_runner(conn, storage, judge_models, say).run(
        version, profile, policies)
    if result.run is None:
        say(f"gauntlet: {result.detail}")
    else:
        say(f"gauntlet run {result.run.id} (suite {SUITE_VERSION}, attempt"
            f" {result.run.attempt}): {result.verdict} ({result.detail})")
    return result


def _print_gauntlet_report(run, say: Say) -> None:
    report = json.loads(run.report_json)
    say(f"gauntlet {run.id}  suite {run.suite_version}  attempt {run.attempt}"
        f"  {'complete' if run.complete else 'incomplete'}"
        f"  verdict {report['verdict']}  ({report['stop_reason']})")
    for r in report.get("invariants", []):
        say(f"  invariant {r['rule']}: {r['disposition']}"
            + (f" ({r['detail']})" if r["disposition"] != "pass" else ""))
    for judge in report.get("judges", []):
        say(f"  judge {judge['judge']} (model {judge['model']},"
            f" attempts {judge['attempts']}): {judge['verdict']}")
        for f in judge["findings"]:
            say(f"    [{f['severity']}] {f['element_id']}: {f['message']}"
                f" (quote: '{f['quote']}')")
        if judge["invalid_findings"]:
            say(f"    invalid findings discarded: {len(judge['invalid_findings'])}")


def run_gauntlet(conn: sqlite3.Connection, storage: StorageAdapter,
                 judge_models: dict[str, ModelAdapter], version_id: str,
                 as_json: bool, say: Say) -> None:
    """`package gauntlet <version-id>`: deliberate early repair and re-judging
    under a newer suite; never a step completion depends on. Re-runs are
    governed solely by the suite-uniqueness rule."""
    repo = SqlitePackageRepository(conn)
    version = _version_or_die(repo, version_id)
    result = _run_gauntlet_attempt(conn, storage, judge_models, version,
                                   say if not as_json else (lambda _s: None))
    if result.run is None:
        if as_json:
            say(json.dumps({"run": None, "detail": result.detail}, indent=2))
        return
    if as_json:
        say(json.dumps({"run": result.run.id,
                        "report": json.loads(result.run.report_json)}, indent=2))
        return
    _print_gauntlet_report(result.run, say)
    if result.verdict == "FAIL":
        package = repo.get_package(version.package_id)
        family_ref = package.role_family_id if package else "<family>"
        for line in remedy_lines(result.run, family_ref, version.id):
            say(line)


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

    runs = repo.list_gauntlet_runs(version.id)  # newest first by seq
    decisions = repo.list_approval_decisions(version.id)
    gauntlet_status = ("pending"
                       if version.status in (VERIFIED, APPROVED)
                       and repo.effective_gauntlet_run(version.id, SUITE_VERSION) is None
                       else "adjudicated" if runs else None)
    payload = {
        "id": version.id, "package_id": version.package_id, "version": version.version,
        "status": version.status,
        "content_model": json.loads(version.content_model_json or "null"),
        "verifier_report": json.loads(version.verifier_report_json or "null"),
        "ats_report": json.loads(version.ats_report_json or "null"),
        "failure_report": json.loads(version.failure_report_json or "null"),
        "context_snapshot_locator": version.context_snapshot_locator,
        "artifact_locator": version.artifact_locator,
        "gauntlet": gauntlet_status,
        # The full report, not only its verdict: a machine consumer must learn
        # WHAT failed (invariant dispositions and details, judge outcomes,
        # findings, limitation lines), exactly as the human view shows it.
        "gauntlet_runs": [{"id": r.id, "suite_version": r.suite_version,
                           "attempt": r.attempt, "complete": bool(r.complete),
                           "verdict": r.verdict, "created_at": r.created_at,
                           "report": json.loads(r.report_json)}
                          for r in runs],
        # An override is never silent: the approval decision travels with the
        # version in both modes, naming the run and verdict it overrode.
        "approval_decisions": [
            {"id": d[0], "gauntlet_run_id": d[2], "verdict_at_decision": d[3],
             "override": bool(d[4]), "override_reason": d[5], "created_at": d[6]}
            for d in decisions],
    }
    if as_json:
        say(json.dumps(payload, indent=2))
        return
    say(f"version {version.id} (v{version.version}, {version.status})")
    if gauntlet_status == "pending":
        say(f"  gauntlet: pending (no run under the current suite {SUITE_VERSION})")
    for run in runs:
        _print_gauntlet_report(run, say)
    for decision in payload["approval_decisions"]:
        if decision["override"]:
            say(f"  approval {decision['id']}: APPROVED DESPITE the Gauntlet"
                f" (run {decision['gauntlet_run_id']}, verdict"
                f" {decision['verdict_at_decision']}) at {decision['created_at']}")
            say(f"    override reason: {decision['override_reason']}")
        else:
            say(f"  approval {decision['id']}: approved on run"
                f" {decision['gauntlet_run_id']} (verdict"
                f" {decision['verdict_at_decision']}) at {decision['created_at']}")
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
               ask: Ask, say: Say,
               judge_models: dict[str, ModelAdapter] | None = None,
               accept_despite: str | None = None) -> None:
    repo = SqlitePackageRepository(conn)
    version = _version_or_die(repo, version_id)
    if version.status not in (VERIFIED, APPROVED):
        raise PackageCliError(f"only a VERIFIED version can be reviewed;"
                              f" '{version_id}' is {version.status}")
    # Reconciliation is automatic, not remembered: a VERIFIED version with no
    # run under the currently shipped suite is judged here, before the gate.
    if (version.status == VERIFIED
            and repo.effective_gauntlet_run(version.id, SUITE_VERSION) is None
            and judge_models is not None):
        say(f"gauntlet: no run under the current suite {SUITE_VERSION};"
            " judging now")
        _run_gauntlet_attempt(conn, storage, judge_models, version, say)
    cv = parse_cv_model(version.content_model_json)
    run_show(conn, version_id, False, say)
    action = ask("accept/edit/quit (a/e/q) [a]: ").strip().lower() or "a"
    if action in ("q", "quit"):
        return
    if action in ("a", "accept"):
        try:
            repo.approve(version.id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         override=accept_despite is not None,
                         override_reason=accept_despite)
        except PackageStateError as e:
            raise PackageCliError(str(e)) from e
        if accept_despite is not None:
            say(f"approved {version.id} DESPITE the Gauntlet verdict"
                f" (override recorded: {accept_despite})")
        else:
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
    edited = _with_bullet_text(cv, context, entry.experience_id, i, new_text)
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
        edited = _with_bullet_text(cv, context, entry.experience_id, i, new_text,
                                   extra_fact_id=fact_id)
        report = GroundingVerifier(context).verify(edited)
        if not report.passed:
            say("still ungrounded after minting; edit dropped:")
            for finding in report.findings[:10]:
                say(f"  [{finding.rule}] {finding.element}: {finding.message}")
            return
    say("Edit grounds; regenerating a new version with the edit applied...")
    run_generate(conn, storage, model, family_ref, page_budget, say, edited_model=edited)


def _with_bullet_text(cv: CvModel, context: GenerationContext, experience_id: str,
                      bullet_index: int, text: str,
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

    # The edited model regenerates under the CURRENT context: the code-stamped
    # fields come from that context, never carried stale from the old version.
    # The headline is one of them, so editing a version stored before the
    # positioning layer existed (headline null) does not produce a new version
    # missing it; the pipeline re-asserts the same stamp on the way through.
    meta = dataclasses.replace(cv.meta, role_family_id=context.role_family_id,
                               strategy_version=context.strategy.strategy_version)
    return CvModel(header=cv.header, headline=build_headline(context),
                   summary=cv.summary, skills=cv.skills,
                   experiences=patch_entries(cv.experiences),
                   projects=patch_entries(cv.projects),
                   education=patch_entries(cv.education), meta=meta)


def run_export(conn: sqlite3.Connection, storage: StorageAdapter, ref: str | None,
               out: Path, say: Say) -> None:
    """Export a version's PDF; defaults to the approved version, never the
    newest. Revalidates stored bytes against the recorded hash."""
    repo = SqlitePackageRepository(conn)
    version = repo.get_version(ref) if ref else None
    if version is None:
        if ref:
            package = repo.get_package(ref)
            if package is None:
                family = resolve_family(conn, ref)
                package = repo.get_base_package_for_family(family.id) if family else None
            if package is None:
                raise PackageCliError(f"nothing found for '{ref}'")
        else:
            rows = conn.execute("SELECT id FROM packages ORDER BY id").fetchall()
            if len(rows) != 1:
                raise PackageCliError(
                    "no packages exist; run package generate" if not rows else
                    "several packages exist; name a version id, package id, or family")
            package = repo.get_package(rows[0][0])
        if package.approved_version_id is None:
            raise PackageCliError("no approved version to export (run package review)")
        version = _version_or_die(repo, package.approved_version_id)
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

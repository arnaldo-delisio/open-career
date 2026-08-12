"""One-shot session driving (OC-36): the file transport's correctness
(seq-matched answers, atomic pending, transcript accumulation), the serve loop
end to end in a thread, and the CLI guardrails (one live session at a time, no
answer without a pending question)."""

import json
import os
import threading
import time

import pytest

from adapters.storage.migrations import migrate
from apps.cli.main import main
from apps.cli.session import FileTransport, run_serve

POLL = 0.05
DEADLINE = 15.0


def _wait_for(predicate, message: str):
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(POLL)
    raise AssertionError(f"timed out waiting for {message}")


def _write_answer(directory, seq: int, text: str, session_id: str | None = None) -> None:
    (directory / f"answer-{seq}.json").write_text(
        json.dumps({"text": text, "session": session_id}))


def test_transport_ask_blocks_until_its_own_answer(tmp_path):
    transport = FileTransport(tmp_path)
    transport.say("hello")
    result = {}

    def worker():
        result["text"] = transport.ask("First question: ")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_for(lambda: (tmp_path / "pending.json").exists(), "pending.json")
    pending = json.loads((tmp_path / "pending.json").read_text())
    assert pending == {"seq": 1, "prompt": "First question: ", "session": None}

    _write_answer(tmp_path, 1, "the answer")
    thread.join(timeout=DEADLINE)
    assert not thread.is_alive()
    assert result["text"] == "the answer"
    # Pickup consumed both the pending marker and the answer file.
    assert not (tmp_path / "pending.json").exists()
    assert not (tmp_path / "answer-1.json").exists()
    transcript = (tmp_path / "transcript.log").read_text()
    assert transcript == "hello\nFirst question: \n> the answer\n"


def test_transport_never_consumes_a_stale_answer_from_an_earlier_seq(tmp_path):
    transport = FileTransport(tmp_path)
    # A leftover answer for a question that no longer exists.
    _write_answer(tmp_path, 0, "stale")
    result = {}

    def worker():
        result["text"] = transport.ask("Q: ")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_for(lambda: (tmp_path / "pending.json").exists(), "pending.json")
    time.sleep(0.6)  # several poll intervals: the stale file must not unblock it
    assert thread.is_alive()
    assert (tmp_path / "answer-0.json").exists()

    _write_answer(tmp_path, 1, "fresh")
    thread.join(timeout=DEADLINE)
    assert result["text"] == "fresh"


def test_transport_empty_answer_is_a_real_blank(tmp_path):
    transport = FileTransport(tmp_path)
    result = {}

    def worker():
        result["text"] = transport.ask("Skippable: ")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_for(lambda: (tmp_path / "pending.json").exists(), "pending.json")
    _write_answer(tmp_path, 1, "")
    thread.join(timeout=DEADLINE)
    assert result["text"] == ""


def test_transport_rejects_an_answer_from_another_session(tmp_path):
    """A seq-matching answer file carrying a different session id (a leftover
    from an earlier session) is deleted and ignored, never consumed."""
    transport = FileTransport(tmp_path, session_id="current")
    _write_answer(tmp_path, 1, "foreign", session_id="previous")
    result = {}

    def worker():
        result["text"] = transport.ask("Q: ")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    _wait_for(lambda: not (tmp_path / "answer-1.json").exists(),
              "foreign answer deletion")
    assert thread.is_alive()

    _write_answer(tmp_path, 1, "ours", session_id="current")
    thread.join(timeout=DEADLINE)
    assert result["text"] == "ours"


def test_serve_loop_runs_a_flow_to_done(tmp_path, monkeypatch):
    """The deepen sitting served through the transport in a thread, every
    question answered blank by a driver loop, ends with done.json status done
    and a transcript carrying every prompt."""
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    _stage_session(session, "tok")

    thread = threading.Thread(target=lambda: run_serve("deepen", "tok", None), daemon=True)
    thread.start()
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() < deadline and not (session / "done.json").exists():
        pending_path = session / "pending.json"
        if pending_path.exists():
            pending = json.loads(pending_path.read_text())
            _write_answer(session, pending["seq"], "", pending["session"])
            # Wait for pickup before re-reading, so one question is never
            # answered twice.
            _wait_for(lambda: not (session / f"answer-{pending['seq']}.json").exists(),
                      "answer pickup")
        time.sleep(POLL)
    thread.join(timeout=DEADLINE)
    assert not thread.is_alive()
    done = json.loads((session / "done.json").read_text())
    assert done["status"] == "done"
    transcript = (session / "transcript.log").read_text()
    assert "Deepen: remaining profile fields" in transcript
    assert "Deepen done." in transcript


def _stage_session(session_dir, token: str, flow: str = "deepen") -> None:
    """The record session start writes before spawning the worker."""
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "session.json").write_text(json.dumps(
        {"pid": None, "flow": flow, "session": token,
         "started_at": "2026-08-12T00:00:00Z"}))


def test_serve_writes_done_error_on_crash(tmp_path, monkeypatch):
    """A serve process that dies still leaves done.json with the error, so
    show can report a crash instead of an ambiguous silence."""
    instance = tmp_path / "missing"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    _stage_session(instance / "session", "tok")
    # No database: _connect exits nonzero, which the serve wrapper records.
    with pytest.raises(SystemExit) as exc:
        run_serve("deepen", "tok", None)
    assert exc.value.code == 1
    done = json.loads((instance / "session" / "done.json").read_text())
    assert done["status"] == "error"


def test_serve_refuses_without_a_matching_launch_token(tmp_path, monkeypatch, capsys):
    """serve is spawned by start, never typed: no record, a wrong token, or a
    record already claimed by a live worker all refuse before any write."""
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"

    with pytest.raises(SystemExit):  # no session record at all
        run_serve("deepen", "tok", None)
    assert not (session / "done.json").exists()

    _stage_session(session, "tok")
    with pytest.raises(SystemExit):  # wrong token
        run_serve("deepen", "other", None)
    assert not (session / "done.json").exists()

    (session / "session.json").write_text(json.dumps(
        {"pid": os.getpid() + 1, "flow": "deepen", "session": "tok",
         "started_at": "2026-08-12T00:00:00Z"}))
    with pytest.raises(SystemExit):  # record claimed by another pid
        run_serve("deepen", "tok", None)
    assert not (session / "done.json").exists()
    assert "refusing to serve" in capsys.readouterr().err


def test_session_start_refuses_a_second_live_session(tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    # A live session: this test process's own pid is certainly alive.
    (session / "session.json").write_text(json.dumps(
        {"pid": os.getpid(), "flow": "deepen", "started_at": "2026-08-12T00:00:00Z"}))

    with pytest.raises(SystemExit) as exc:
        main(["session", "start", "deepen"])
    assert exc.value.code == 1
    assert "already running" in capsys.readouterr().err


def test_start_show_and_stop_defer_while_another_start_holds_the_lock(
        tmp_path, monkeypatch, capsys):
    """The start critical section holds a flock on instance/session.lock: a
    concurrent start refuses, and show/stop finding a pid-less record report
    a start in progress instead of cleaning the directory out from under it."""
    import fcntl

    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    # The state a start-in-flight leaves mid-section: a pid-less record.
    (session / "session.json").write_text(json.dumps(
        {"pid": None, "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))

    fd = os.open(instance / "session.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(SystemExit) as exc:
            main(["session", "start", "deepen"])
        assert exc.value.code == 1
        assert "already in progress" in capsys.readouterr().err

        main(["session", "show"])
        assert "start is in progress" in capsys.readouterr().out
        assert (session / "session.json").exists()  # nothing cleaned

        main(["session", "stop"])
        assert "start is in progress" in capsys.readouterr().out
        assert (session / "session.json").exists()
    finally:
        os.close(fd)

    # Lock released (holder gone): the stale pid-less record is now cleanable.
    main(["session", "show"])
    assert "failed before the worker launched" in capsys.readouterr().out
    assert not session.exists()


def test_session_answer_without_a_pending_question_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(tmp_path / "instance"))
    with pytest.raises(SystemExit) as exc:
        main(["session", "answer", "hello"])
    assert exc.value.code == 1
    assert "no pending question" in capsys.readouterr().err


def test_session_start_rejects_cv_for_non_onboard_flows(tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    cv = tmp_path / "cv.txt"
    cv.write_text("body\n")
    with pytest.raises(SystemExit) as exc:
        main(["session", "start", "deepen", str(cv)])
    assert exc.value.code == 1
    assert "only applies to the onboard flow" in capsys.readouterr().err


def test_session_show_reports_new_transcript_then_pending(tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "transcript.log").write_text("line one\nQ1?\n")
    (session / "session.json").write_text(json.dumps(
        {"pid": os.getpid(), "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    (session / "pending.json").write_text(json.dumps({"seq": 1, "prompt": "Q1?"}))

    main(["session", "show"])
    out = capsys.readouterr().out
    assert "line one" in out and "waiting for your answer (question 1)" in out

    # A second show repeats nothing already shown; the pending question stays.
    main(["session", "show"])
    out = capsys.readouterr().out
    assert "line one" not in out and "waiting for your answer" in out

    # And --full replays the whole transcript.
    main(["session", "show", "--full"])
    assert "line one" in capsys.readouterr().out


def test_session_stop_reports_an_unconsumed_answer_as_discarded(
        tmp_path, monkeypatch, capsys):
    """Stopping with an answer file no worker will ever consume must not
    claim everything is saved (the worker here is a dead pid, so the wait
    times out)."""
    monkeypatch.setattr("apps.cli.session.STOP_CONSUME_WAIT", 0.3)
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "transcript.log").write_text("Q1?\n")
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    _write_answer(session, 1, "unpicked", session_id="s1")

    main(["session", "stop"])
    out = capsys.readouterr().out
    assert "had not been processed and was discarded" in out
    assert not session.exists()


def test_stale_pending_from_a_dead_worker_is_not_answerable(
        tmp_path, monkeypatch, capsys):
    """A pending question whose worker died is stale: show reports the crash
    instead of inviting an answer, and answer refuses."""
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "transcript.log").write_text("Q1?\n")
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    (session / "pending.json").write_text(json.dumps(
        {"seq": 1, "prompt": "Q1?", "session": "s1"}))

    main(["session", "show"])
    out = capsys.readouterr().out
    assert "crashed" in out and "waiting for your answer" not in out
    assert not (session / "pending.json").exists()  # stale marker cleaned

    (session / "pending.json").write_text(json.dumps(
        {"seq": 1, "prompt": "Q1?", "session": "s1"}))
    with pytest.raises(SystemExit) as exc:
        main(["session", "answer", "hello"])
    assert exc.value.code == 1
    assert "stale" in capsys.readouterr().err


def test_recycled_pid_with_wrong_fingerprint_is_dead_session_state(
        tmp_path, monkeypatch, capsys):
    """A live pid whose start-time fingerprint does not match the record is a
    recycled pid: start proceeds instead of refusing, and stop cleans up
    without signalling the innocent process."""
    import signal as signal_mod

    from apps.cli import session as session_mod

    instance = tmp_path / "instance"
    migrate(instance / "open-career.sqlite3")
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"

    def stage_recycled():
        session.mkdir(parents=True, exist_ok=True)
        (session / "session.json").write_text(json.dumps(
            {"pid": os.getpid(), "pid_start": "not-this-incarnation",
             "flow": "deepen", "session": "s1",
             "started_at": "2026-08-12T00:00:00Z"}))

    signals = []
    real_kill = os.kill

    def recording_kill(pid, sig):
        if sig == 0:
            return real_kill(pid, sig)  # liveness probes stay real
        signals.append(("kill", pid, sig))

    monkeypatch.setattr(os, "kill", recording_kill)
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: signals.append(("killpg", pid, sig)),
                        raising=False)

    stage_recycled()
    main(["session", "stop"])
    assert "session stopped" in capsys.readouterr().out
    assert signals == []  # dead state: nothing signalled
    assert not session.exists()

    stage_recycled()

    class FakeProc:
        pid = 4242

    monkeypatch.setattr(session_mod.subprocess, "Popen",
                        lambda *a, **k: FakeProc())
    main(["session", "start", "deepen"])  # proceeds: no refusal SystemExit
    assert "session started" in capsys.readouterr().out
    assert signal_mod.SIGTERM not in [s[2] for s in signals if s[1] == os.getpid()]


def test_stop_defers_while_a_start_holds_the_lock_even_with_a_claimed_record(
        tmp_path, monkeypatch, capsys):
    """Between the worker's claim and the parent's spawn record the start
    still owns the directory: stop under a held lock defers (no signal, no
    cleanup) even though the record carries a pid, and works after release."""
    import fcntl

    import signal as signal_mod

    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "pid_start": "x", "flow": "deepen",
         "session": "s1", "started_at": "2026-08-12T00:00:00Z"}))

    signals = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill",
                        lambda pid, sig: real_kill(pid, sig) if sig == 0
                        else signals.append((pid, sig)))
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: signals.append((pid, sig)),
                        raising=False)

    fd = os.open(instance / "session.lock", os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        main(["session", "stop"])
        assert "start is in progress" in capsys.readouterr().out
        assert signals == [] and session.exists()  # deferred entirely
    finally:
        os.close(fd)

    main(["session", "stop"])
    assert "session stopped" in capsys.readouterr().out
    assert not session.exists()
    assert signal_mod.SIGTERM not in [s[1] for s in signals]  # dead pid: nothing signalled


def test_parent_spawn_record_never_overwrites_a_landed_worker_claim(tmp_path):
    """A fast worker can claim session.json before the parent's post-Popen
    write: the parent must leave that claim (and its authoritative
    fingerprint) untouched, and still write its own record otherwise."""
    from apps.cli.session import _record_spawn

    claim = {"pid": 4242, "pid_start": "workers-own-fingerprint",
             "flow": "deepen", "session": "s1",
             "started_at": "2026-08-12T00:00:00Z"}
    (tmp_path / "session.json").write_text(json.dumps(claim))
    _record_spawn(tmp_path, "deepen", "s1", 4242)
    assert json.loads((tmp_path / "session.json").read_text()) == claim

    # A record that is not this spawn's claim (different pid) is overwritten
    # with the parent's view.
    _record_spawn(tmp_path, "deepen", "s1", 5555)
    written = json.loads((tmp_path / "session.json").read_text())
    assert written["pid"] == 5555 and written["session"] == "s1"
    assert written["pid_start"] != "workers-own-fingerprint"


def test_stop_terminates_the_workers_process_group(tmp_path, monkeypatch, capsys):
    """Stop signals the worker's process group (pid == pgid under
    start_new_session), so an in-flight model subprocess dies with it."""
    import signal as signal_mod

    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    from apps.cli.session import _pid_start_fingerprint
    (session / "session.json").write_text(json.dumps(
        {"pid": os.getpid(),
         "pid_start": _pid_start_fingerprint(os.getpid()),
         "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))

    calls = []
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: calls.append((pid, sig)),
                        raising=False)
    main(["session", "stop"])
    assert calls == [(os.getpid(), signal_mod.SIGTERM)]
    assert not session.exists()


def test_session_stop_on_onboard_names_the_buffered_families_step(
        tmp_path, monkeypatch, capsys):
    """The families step buffers answers until the strategy version mints, so
    stopping an onboard session states that caveat instead of claiming every
    answer persisted."""
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "transcript.log").write_text("Q1?\n")
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "onboard", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    main(["session", "stop"])
    out = capsys.readouterr().out
    assert "everything already persisted is saved" in out
    assert "families init" in out


def test_session_stop_on_stories_names_the_in_progress_story(
        tmp_path, monkeypatch, capsys):
    """A story's three prompts buffer until the story completes, so stopping a
    stories sitting names the in-progress story caveat."""
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "transcript.log").write_text("Situation: \n")
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "stories", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    main(["session", "stop"])
    out = capsys.readouterr().out
    assert "everything already persisted is saved" in out
    assert "story" in out and "asked again" in out


def test_session_show_cleans_a_stale_start_that_never_spawned(
        tmp_path, monkeypatch, capsys):
    """A session.json with no pid, no progress files, and an old started_at is
    a start that died before spawning; show reports and cleans it."""
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    (session / "session.json").write_text(json.dumps(
        {"pid": None, "flow": "deepen", "session": "s1",
         "started_at": "2026-08-12T00:00:00Z"}))
    main(["session", "show"])
    assert "failed before the worker launched" in capsys.readouterr().out
    assert not session.exists()


def test_session_show_reports_a_dead_pid_without_done_as_crashed(
        tmp_path, monkeypatch, capsys):
    instance = tmp_path / "instance"
    monkeypatch.setenv("OPEN_CAREER_INSTANCE", str(instance))
    session = instance / "session"
    session.mkdir(parents=True)
    # A pid that cannot be alive: fork a child that exits immediately, or use
    # an id far beyond pid_max. The latter is deterministic.
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "deepen", "started_at": "x"}))
    main(["session", "show"])
    out = capsys.readouterr().out
    assert "crashed" in out and "everything answered so far is saved" in out
    assert "families init" not in out  # the caveat is onboard-only

    # A crashed onboard sitting names the buffered family setup step, and a
    # crashed stories sitting names the in-progress story.
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "onboard", "started_at": "x"}))
    main(["session", "show", "--full"])
    assert "families init" in capsys.readouterr().out
    (session / "session.json").write_text(json.dumps(
        {"pid": 2 ** 22 + 12345, "flow": "stories", "started_at": "x"}))
    main(["session", "show", "--full"])
    out = capsys.readouterr().out
    assert "story" in out and "asked again" in out

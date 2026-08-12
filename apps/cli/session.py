"""One-shot session driver for the interview sittings (OC-36).

An agent restricted to short-lived commands cannot hold an interactive sitting
open, so `session start` detaches a serve process that runs the exact same flow
entrypoints (onboard, deepen, stories) with ask/say wired to a file transport
under instance/session/. pending.json carries the current question, written
atomically so a reader never sees a torn file; answer-<seq>.json carries its
answer, matched by seq so a stale answer left over from an earlier question is
never consumed by a later one; transcript.log is the append-only human-readable
record of everything said, asked, and answered; done.json marks completion or
crash. Sittings persist per item, so a stopped or crashed session loses
nothing already persisted; the buffered multi-step units (_BUFFERED_UNITS)
are the stated exception, re-asked on the next run.
"""

import calendar
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from adapters.storage.instance import db_path, instance_dir

FLOWS = ("onboard", "deepen", "stories")
POLL_INTERVAL = 0.2

# How long stop waits for the worker to consume an already-written answer
# before honestly reporting it discarded, and the grace after consumption for
# the flow's own persist (effectively immediate) to land.
STOP_CONSUME_WAIT = 2.0
STOP_GRACE = 0.5

# A session.json without a pid and older than this is a start that died
# between writing the record and spawning the worker. The worker writes its
# own pid the moment it starts, so only a start that truly never spawned
# stays pid-less; the generous age covers a slow interpreter launch.
FAILED_START_AGE = 30.0

ANSWER_HINT = 'answer with: open-career session answer "<text>" (empty "" skips)'


def session_dir() -> Path:
    return instance_dir() / "session"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json_atomic(path: Path, payload: dict) -> None:
    # Unique temp file plus os.replace: the reader on the other side either
    # sees the previous state or the complete new file, never a partial
    # write, and two concurrent writers (the parent's pid update racing the
    # worker's claim) can never truncate each other's temp mid-write.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(payload))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _pid_start_fingerprint(pid: int) -> str | None:
    """Field 22 of /proc/<pid>/stat, the process start time in clock ticks:
    together with the pid it identifies one process incarnation, so a
    recycled pid never impersonates a dead worker. None when unreadable
    (dead pid, or no /proc on this platform)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes()
    except OSError:
        return None
    try:
        # comm (field 2) may contain spaces and parentheses; the fixed-format
        # fields resume after the last ')', starting at field 3.
        rest = stat[stat.rindex(b")") + 2:].split()
        return rest[19].decode()
    except (ValueError, IndexError):
        return None


def _session_alive(info: dict | None) -> bool:
    """The recorded pid counts as this session's worker only when the process
    is alive and, where both a recorded and a current start-time fingerprint
    exist, they match; a mismatch means the pid was recycled and the session
    state is dead. Without a fingerprint on either side, plain pid liveness
    decides (the pre-fingerprint record, or a platform without /proc)."""
    pid = info.get("pid") if info else None
    if pid is None:
        return False
    pid = int(pid)
    if not _pid_alive(pid):
        return False
    recorded = info.get("pid_start")
    if recorded is None:
        return True
    current = _pid_start_fingerprint(pid)
    if current is None:
        return True
    return current == recorded


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _lock_path() -> Path:
    return instance_dir() / "session.lock"


def _try_start_lock() -> int | None:
    """The start critical section's lock: a flock descriptor on a persistent
    instance/session.lock file, outside the session directory so cleanup
    inside the section cannot remove it. Returns the held descriptor, or None
    when another process holds the lock; closing the descriptor releases it,
    and process death releases it automatically (no staleness logic, no
    unlink, ever)."""
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _pending_is_stale(done: dict | None, info: dict | None) -> bool:
    """A pending question is only live while its worker is: done.json or a
    missing/dead pid means nobody will ever consume the answer."""
    if done is not None:
        return True
    return not _session_alive(info)


# Flows with a multi-step unit that buffers prompts until the unit completes,
# and the wording naming it: the family setup step persists only when the
# strategy version mints (OC-33), a story persists only when its three
# prompts are done. Resume state is computed from data, so an incomplete unit
# is simply asked again; no draft state exists to save.
_BUFFERED_UNITS = {
    "onboard": "the family setup step (`open-career families init` will ask"
               " its questions again)",
    "stories": "an in-progress story's prompts (the next `stories` run asks"
               " for that story again)",
}


def _say_buffer_caveat(info: dict | None, say) -> None:
    """Per-answer persistence claims need this caveat when a sitting with a
    buffered multi-step unit ended early: answers inside a unit still in
    progress were not yet persisted."""
    unit = _BUFFERED_UNITS.get(info.get("flow")) if info else None
    if unit is not None:
        say("Answers inside a multi-step unit still in progress were not yet"
            f" persisted and will be asked again, here {unit}.")


def _is_failed_start(directory: Path, info: dict) -> bool:
    """A session record with no pid means start died between writing the
    record and spawning the worker, unless the start is still in flight
    (young record) or the worker has since shown progress."""
    if info.get("pid") is not None:
        return False
    if any((directory / name).exists()
           for name in ("pending.json", "done.json", "transcript.log")):
        return False
    try:
        started = calendar.timegm(
            time.strptime(info.get("started_at", ""), "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        return True
    return time.time() - started > FAILED_START_AGE


class FileTransport:
    """Serve-side ask/say over the session directory. say appends to the
    transcript; ask publishes the question, then blocks polling for exactly
    its own answer file (seq-matched). With a session id set, an answer
    carrying a different id (a leftover from an earlier session) is deleted
    and ignored, never consumed."""

    def __init__(self, directory: Path, session_id: str | None = None):
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)
        self._session = session_id
        self._seq = 0

    def _log(self, line: str) -> None:
        with open(self._dir / "transcript.log", "a") as handle:
            handle.write(line + "\n")

    def say(self, line: str = "") -> None:
        self._log(str(line))

    def ask(self, prompt: str) -> str:
        self._seq += 1
        self._log(prompt)
        _write_json_atomic(self._dir / "pending.json",
                           {"seq": self._seq, "prompt": prompt,
                            "session": self._session})
        answer_path = self._dir / f"answer-{self._seq}.json"
        while True:
            payload = _read_json(answer_path)
            if payload is not None:
                if self._session is not None and payload.get("session") != self._session:
                    answer_path.unlink(missing_ok=True)  # another session's leftover
                else:
                    break
            time.sleep(POLL_INTERVAL)
        (self._dir / "pending.json").unlink(missing_ok=True)
        answer_path.unlink(missing_ok=True)
        text = str(payload.get("text", ""))
        self._log(f"> {text}")
        return text


def run_serve(flow: str, token: str, cv: str | None) -> None:
    """The detached worker: run the flow against the file transport, then
    record the outcome in done.json whatever happened (a crash without
    done.json would be indistinguishable from a kill). The token is the
    launch handshake: serve refuses to touch a session it was not started
    for, so a stray invocation can never hijack a live sitting."""
    directory = session_dir()
    info = _read_json(directory / "session.json")
    if info is None or info.get("session") != token \
            or info.get("pid") not in (None, os.getpid()):
        print("refusing to serve: no matching session record for this launch"
              " token (session serve is spawned by session start, not typed)",
              file=sys.stderr)
        raise SystemExit(1)
    transport = FileTransport(directory, token)
    try:
        # First act of the worker: claim the record with its own pid (session
        # id preserved), so a slow spawn is never mistaken for a failed start.
        # Inside the try so even a failure here still records done.json.
        _write_json_atomic(directory / "session.json",
                           {"pid": os.getpid(),
                            "pid_start": _pid_start_fingerprint(os.getpid()),
                            "flow": info.get("flow", flow),
                            "session": token,
                            "started_at": info.get("started_at") or _now()})
        _run_flow(flow, cv, transport)
    except BaseException as e:
        _write_json_atomic(directory / "done.json",
                           {"status": "error", "detail": f"{type(e).__name__}: {e}"})
        raise SystemExit(1)
    _write_json_atomic(directory / "done.json", {"status": "done", "detail": None})


def _run_flow(flow: str, cv: str | None, transport: FileTransport) -> None:
    # Imported here, not at module top: main.py imports this module, and the
    # flow entrypoints live there and in its siblings.
    from adapters.storage.local import LocalStorageAdapter
    from apps.cli import interview as interview_cli
    from apps.cli import main as cli_main
    from apps.cli import stories as stories_cli

    conn = cli_main._connect()
    try:
        if flow == "onboard":
            cli_main.run_onboard_flow(
                conn, LocalStorageAdapter(instance_dir()),
                Path(cv) if cv else None,
                transport.ask, transport.say, warn=transport.say)
        elif flow == "deepen":
            interview_cli.run_deepen(conn, transport.ask, transport.say)
        elif flow == "stories":
            stories_cli.run_stories(conn, LocalStorageAdapter(instance_dir()),
                                    transport.ask, transport.say)
        else:
            raise ValueError(f"unknown flow '{flow}'")
    finally:
        conn.close()


def run_start(flow: str, cv: str | None, say) -> None:
    if flow not in FLOWS:
        _fail(f"unknown flow '{flow}' (expected one of {', '.join(FLOWS)})")
    if cv is not None and flow != "onboard":
        _fail("a CV argument only applies to the onboard flow")
    if cv is not None and not Path(cv).exists():
        _fail(f"CV file not found: {cv}")
    if not db_path().exists():
        _fail("instance not initialized (run: open-career init)")
    directory = session_dir()
    # The whole critical section (liveness check, cleanup, session record,
    # spawn) runs under a short-lived lock outside the session directory, so
    # a concurrent start can never rmtree a directory another start just
    # created.
    lock_fd = _try_start_lock()
    if lock_fd is None:
        _fail("another session start is already in progress")
    try:
        info = _read_json(directory / "session.json")
        if _session_alive(info):
            _fail(f"a session is already running ({info.get('flow')},"
                  f" pid {info.get('pid')}); finish it, or `open-career session stop`")
        # Everything under session/ belongs to the previous (dead) run.
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        # The session record lands before the spawn (a worker without a record
        # would be untrackable); the pid follows once it exists.
        session_id = uuid.uuid4().hex
        _write_json_atomic(directory / "session.json",
                           {"pid": None, "flow": flow, "session": session_id,
                            "started_at": _now()})
        argv = [sys.executable, "-m", "apps.cli.main", "session", "serve",
                flow, session_id]
        if cv is not None:
            argv.append(str(Path(cv).resolve()))
        try:
            with open(directory / "serve.log", "ab") as log:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                        stderr=log, start_new_session=True)
        except OSError as e:
            shutil.rmtree(directory, ignore_errors=True)
            _fail(f"failed to start the session worker: {e}")
        _record_spawn(directory, flow, session_id, proc.pid)
    finally:
        os.close(lock_fd)
    say(f"session started ({flow}, pid {proc.pid});"
        " follow it with `open-career session show`")


def _record_spawn(directory: Path, flow: str, session_id: str, pid: int) -> None:
    """The parent's post-spawn pid record. A fast worker may have claimed the
    record already (same session id, same pid): that claim carries the
    authoritative fingerprint and is left untouched, never overwritten with
    the parent's view of it."""
    current = _read_json(directory / "session.json") or {}
    if current.get("session") == session_id and current.get("pid") == pid:
        return
    _write_json_atomic(directory / "session.json",
                       {"pid": pid, "pid_start": _pid_start_fingerprint(pid),
                        "flow": flow, "session": session_id,
                        "started_at": _now()})


def run_show(full: bool, say) -> None:
    directory = session_dir()
    transcript = directory / "transcript.log"
    offset_path = directory / "show.offset"
    data = transcript.read_bytes() if transcript.exists() else b""
    offset = 0
    if not full and offset_path.exists():
        try:
            offset = int(offset_path.read_text())
        except ValueError:
            offset = 0
    if offset > len(data):
        offset = 0  # a new session replaced the transcript since the last show
    new = data[offset:]
    if new:
        say(new.decode(errors="replace").rstrip("\n"))
    if data:
        offset_path.write_text(str(len(data)))

    pending = _read_json(directory / "pending.json")
    done = _read_json(directory / "done.json")
    info = _read_json(directory / "session.json")
    if pending is not None:
        if _pending_is_stale(done, info):
            # The worker died with a question on the table: the crash state
            # below is the truth, and the question must not invite an answer
            # nobody will consume.
            (directory / "pending.json").unlink(missing_ok=True)
        else:
            say(f"\nwaiting for your answer (question {pending.get('seq')}):")
            say(f"  {pending.get('prompt')}")
            say(f"  {ANSWER_HINT}")
            return
    if done is not None:
        if done.get("status") == "done":
            say("\nsession finished.")
        else:
            say(f"\nsession crashed: {done.get('detail')};"
                " everything answered so far is saved.")
            _say_buffer_caveat(info, say)
    elif info is None:
        say("no session.")
    elif info.get("pid") is None:
        # A pid-less record may be a start still in flight: only judge it
        # while holding the start lock, and never clean under a live start.
        lock_fd = _try_start_lock()
        if lock_fd is None:
            say("\na session start is in progress; try again in a moment.")
        else:
            try:
                info = _read_json(directory / "session.json") or info
                if _is_failed_start(directory, info):
                    shutil.rmtree(directory, ignore_errors=True)
                    say("session start failed before the worker launched; cleaned up.")
                elif _session_alive(info):
                    say("\nsession running; no pending question yet.")
                else:
                    say("\nsession starting; try again in a moment.")
            finally:
                os.close(lock_fd)
    elif _session_alive(info):
        say("\nsession running; no pending question yet.")
    else:
        say("\nsession crashed (process died without finishing);"
            " everything answered so far is saved.")
        _say_buffer_caveat(info, say)


def run_answer(text: str, say) -> None:
    directory = session_dir()
    pending = _read_json(directory / "pending.json")
    if pending is None:
        _fail("no pending question (see: open-career session show)")
    if _pending_is_stale(_read_json(directory / "done.json"),
                         _read_json(directory / "session.json")):
        _fail("the session is no longer running, so this question is stale"
              " and cannot be answered (see: open-career session show)")
    _write_json_atomic(directory / f"answer-{pending['seq']}.json",
                       {"text": text, "session": pending.get("session")})
    say(f"answered question {pending['seq']};"
        " `open-career session show` to continue")


def run_stop(say) -> None:
    directory = session_dir()
    info = _read_json(directory / "session.json")
    if info is None:
        say("no session.")
        return
    # Stop terminates and deletes, so the whole of it runs under the start
    # lock, pid-carrying records included: between the worker's claim and the
    # parent's spawn record the start still owns the directory, and a stop in
    # that window would fail the initiating start.
    lock_fd = _try_start_lock()
    if lock_fd is None:
        say("a session start is in progress; try again in a moment.")
        return
    try:
        info = _read_json(directory / "session.json") or info
        if info.get("pid") is None:
            if _is_failed_start(directory, info):
                shutil.rmtree(directory, ignore_errors=True)
                say("session start had failed before the worker launched; cleaned up.")
                return
            say("session starting; try again in a moment.")
            return
        _stop_worker(directory, info, say)
    finally:
        os.close(lock_fd)


def _stop_worker(directory: Path, info: dict, say) -> None:
    """Terminate the (claimed) worker and clean up, under the start lock."""
    # An answer already written but not yet consumed would be lost by the
    # kill: give the worker a moment to pick it up, and say so honestly if it
    # never did. After pickup the flow's own persist is effectively immediate;
    # a short grace covers it.
    discarded = False
    if list(directory.glob("answer-*.json")):
        deadline = time.monotonic() + STOP_CONSUME_WAIT
        while time.monotonic() < deadline and list(directory.glob("answer-*.json")):
            time.sleep(POLL_INTERVAL)
        if list(directory.glob("answer-*.json")):
            discarded = True
        else:
            time.sleep(STOP_GRACE)
    if _session_alive(info):
        pid = int(info["pid"])
        # The worker was launched with start_new_session=True, so its pid is
        # its process group: killpg takes an in-flight model subprocess down
        # with it; plain kill is the fallback.
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, AttributeError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    shutil.rmtree(directory, ignore_errors=True)
    if discarded:
        say("session stopped; the last answer had not been processed and was"
            " discarded, everything already persisted is saved.")
    else:
        say("session stopped; everything already persisted is saved.")
    _say_buffer_caveat(info, say)

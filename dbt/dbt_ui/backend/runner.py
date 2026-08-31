"""
Background dbt invocations with streamable logs.

dbt runs as a subprocess rather than in-process. That is a deliberate choice:

  * dbt-core keeps global state (flags, the logging manager, the adapter
    registry) that is not safe to re-enter from a long-lived server process.
  * a hard failure inside dbt cannot take the UI down with it.
  * the log output is byte-identical to what the team already sees in their
    terminal and in dbt Cloud, so nothing new has to be learned.

Each invocation becomes a Job with an append-only log buffer. The frontend polls
for lines after a cursor, so several people can watch the same run and a browser
refresh loses nothing.
"""

from __future__ import annotations

import itertools
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config, manifest as manifest_mod

MAX_JOBS_RETAINED = 40
MAX_LINES_PER_JOB = 8000

# Commands the UI is allowed to invoke. An allow-list beats sanitising strings:
# the browser sends a verb, never a command line.
ALLOWED_COMMANDS: Dict[str, Dict[str, Any]] = {
    "parse":  {"label": "Parse project",     "writes": False},
    "deps":   {"label": "Install packages",  "writes": False},
    "debug":  {"label": "Test connection",   "writes": False},
    "compile": {"label": "Compile",          "writes": False},
    "seed":   {"label": "Load seeds",        "writes": True},
    "run":    {"label": "Run models",        "writes": True},
    "test":   {"label": "Run tests",         "writes": False},
    "build":  {"label": "Build (seed+run+test)", "writes": True},
    "snapshot": {"label": "Snapshot",        "writes": True},
    "docs":   {"label": "Generate docs",     "writes": False},
    "source": {"label": "Source freshness",  "writes": False},
    "clean":  {"label": "Clean artifacts",   "writes": False},
    "ls":     {"label": "List resources",    "writes": False},
}

# Only these flags may be forwarded, and each is validated below.
_SELECTOR_SAFE = re.compile(r"^[A-Za-z0-9_@%*+,.:/ ()-]+$")


@dataclass
class Job:
    id: str
    command: str
    argv: List[str]
    label: str
    target: str
    writes: bool
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    status: str = "queued"          # queued | running | success | failed | cancelled
    exit_code: Optional[int] = None
    lines: List[Dict[str, Any]] = field(default_factory=list)
    process: Optional[subprocess.Popen] = None
    _launcher_len: int = 1
    _counter: Any = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------

    def append(self, text: str, stream: str = "stdout") -> None:
        with self._lock:
            if len(self.lines) >= MAX_LINES_PER_JOB:
                if self.lines[-1].get("text") != "... log truncated ...":
                    self.lines.append({
                        "seq": next(self._counter),
                        "text": "... log truncated ...",
                        "stream": "meta",
                        "level": "warn",
                        "ts": time.time(),
                    })
                return
            self.lines.append({
                "seq": next(self._counter),
                "text": text,
                "stream": stream,
                "level": _classify(text),
                "ts": time.time(),
            })

    def lines_after(self, cursor: int, limit: int = 800) -> List[Dict[str, Any]]:
        with self._lock:
            return [line for line in self.lines if line["seq"] > cursor][:limit]

    @property
    def duration(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    @property
    def display_command(self) -> str:
        """
        The equivalent command line, for the log header and the UI.

        Built here rather than in the browser because only this side knows how
        many leading argv entries are the launcher.
        """
        args = self.argv[self._launcher_len:]
        return " ".join(
            shlex.quote(arg) if (" " in arg or '"' in arg) else arg
            for arg in ["dbt", *args]
        )

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            last_seq = self.lines[-1]["seq"] if self.lines else 0
            line_count = len(self.lines)
        return {
            "id": self.id,
            "command": self.command,
            "label": self.label,
            "argv": self.argv,
            "display_command": self.display_command,
            "target": self.target,
            "writes": self.writes,
            "status": self.status,
            "exit_code": self.exit_code,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": self.duration,
            "line_count": line_count,
            "last_seq": last_seq,
            "is_active": self.status in ("queued", "running"),
        }


def _classify(text: str) -> str:
    """Tag a log line so the console can colour it."""
    lowered = text.lower()
    if "[error]" in lowered or " error " in lowered or "failure" in lowered:
        return "error"
    if "[warning]" in lowered or "warn" in lowered or "deprecat" in lowered:
        return "warn"
    if "[skip]" in lowered:
        return "skip"
    if "[pass]" in lowered or "completed successfully" in lowered \
            or " ok " in lowered or "[success" in lowered:
        return "success"
    if "[run]" in lowered or "start " in lowered:
        return "info"
    return "plain"


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

class JobRegistry:
    """Bounded, thread-safe store of recent jobs. One dbt run at a time."""

    def __init__(self) -> None:
        self._jobs: "OrderedDict[str, Job]" = OrderedDict()
        self._lock = threading.Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > MAX_JOBS_RETAINED:
                oldest_id, oldest = next(iter(self._jobs.items()))
                if oldest.status in ("queued", "running"):
                    break
                self._jobs.pop(oldest_id, None)

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> List[Dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [job.summary() for job in sorted(
            jobs, key=lambda j: j.created_at, reverse=True)]

    def active(self) -> Optional[Job]:
        with self._lock:
            for job in reversed(self._jobs.values()):
                if job.status in ("queued", "running"):
                    return job
        return None


REGISTRY = JobRegistry()


# --------------------------------------------------------------------------
# argv construction
# --------------------------------------------------------------------------

def _validate_selector(value: str, flag: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{flag} was empty.")
    if len(value) > 500:
        raise ValueError(f"{flag} is unreasonably long.")
    if not _SELECTOR_SAFE.match(value):
        raise ValueError(
            f"{flag} contains characters that are not allowed in a dbt "
            f"selector: {value!r}"
        )
    return value


class BlockedLayerError(ValueError):
    """A selector explicitly targeted a layer the UI may not build."""


# Commands that act on the node graph, so an exclusion is meaningful. `deps`,
# `debug` and `clean` do not select nodes at all.
_SELECTING_COMMANDS = {
    "run", "build", "test", "seed", "snapshot", "compile", "ls", "docs", "source",
}


def _assert_selector_not_blocked(selector: str) -> None:
    """
    Refuse a --select that names a blocked layer.

    Without this, `--select tag:gold` would fight the forced `--exclude
    tag:gold` and dbt would resolve it to nothing, which looks like a silent
    no-op rather than a refusal. Better to say why.
    """
    blocked = config.blocked_build_layers()
    if not blocked:
        return

    lowered = selector.lower()
    for layer in blocked:
        if f"tag:{layer}" in lowered:
            raise BlockedLayerError(
                f"This UI may not build the {layer} layer, so "
                f"'--select tag:{layer}' is refused."
            )
        # A path selector into the layer's folder, e.g. path:models/gold
        if f"models/{layer}" in lowered or f"models\\{layer}" in lowered:
            raise BlockedLayerError(
                f"This UI may not build the {layer} layer, so a selector "
                f"pointing at models/{layer} is refused."
            )

    # A bare model name that resolves into a blocked layer.
    mf = manifest_mod.try_load()
    if mf is None:
        return

    for token in re.split(r"[\s,]+", selector.strip()):
        name = token.strip().lstrip("+").rstrip("+")
        name = re.sub(r"^\d+\+", "", name)
        if not name or ":" in name:
            continue
        node = mf.node_by_name(name)
        if node is None:
            continue
        tags = [str(t).lower() for t in (node.get("tags") or [])]
        hit = next((layer for layer in blocked if layer in tags), None)
        if hit:
            raise BlockedLayerError(
                f"'{name}' is a {hit} model, and this UI may not build the "
                f"{hit} layer."
            )


def build_argv(
    command: str,
    target: Optional[str] = None,
    select: Optional[str] = None,
    exclude: Optional[str] = None,
    full_refresh: bool = False,
    threads: Optional[int] = None,
    subcommand: Optional[str] = None,
    extra: Optional[List[str]] = None,
) -> List[str]:
    """
    Assemble a dbt command line as an argv list.

    Never a shell string: every value is a separate argv element, so a selector
    containing a space or a quote cannot turn into a second command.

    Blocked layers are excluded unconditionally. The exclusion is added here
    rather than trusted to the caller, so there is no code path in the UI that
    can build them.
    """
    if command not in ALLOWED_COMMANDS:
        raise ValueError(f"'{command}' is not an allowed dbt command.")

    argv: List[str] = _dbt_launcher() + [command]

    if command == "docs":
        # --static bundles the whole site into target/static_index.html, which
        # the UI can then serve directly. No second web server to babysit.
        argv += ["generate", "--static"]
    elif command == "source":
        argv.append("freshness")

    argv += ["--profiles-dir", str(config.profiles_dir())]
    argv += ["--project-dir", str(config.PROJECT_DIR)]

    resolved_target = target or config.default_target_name()
    argv += ["--target", str(resolved_target)]

    if select:
        cleaned = _validate_selector(select, "--select")
        _assert_selector_not_blocked(cleaned)
        argv += ["--select", cleaned]

    # Merge the caller's exclusions with the mandatory ones. dbt accepts several
    # values after a single --exclude, and the union is what we want.
    exclusions: List[str] = []
    if exclude:
        exclusions.append(_validate_selector(exclude, "--exclude"))
    if command in _SELECTING_COMMANDS:
        exclusions.extend(config.blocked_exclude_selectors())

    if exclusions:
        argv.append("--exclude")
        argv.extend(exclusions)

    if full_refresh and command in ("run", "build", "seed"):
        argv.append("--full-refresh")
    if threads:
        argv += ["--threads", str(int(threads))]
    if extra:
        argv += list(extra)

    return argv


def _dbt_launcher() -> List[str]:
    """
    How to invoke dbt.

    Prefer the `dbt` console script that sits beside the running interpreter:
    it is the exact command the team already types, and it avoids the
    "found in sys.modules after import of package" RuntimeWarning that
    `python -m dbt.cli.main` prints on every single run.

    Falling back to the module form keeps this working in an environment where
    the script was not installed (some CI images, or a bare `pip install
    --no-scripts`).
    """
    interpreter = pathlib.Path(sys.executable)
    candidates = [
        interpreter.parent / "Scripts" / ("dbt.exe" if os.name == "nt" else "dbt"),
        interpreter.parent / ("dbt.exe" if os.name == "nt" else "dbt"),
        interpreter.parent / "bin" / "dbt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return [str(candidate)]

    found = shutil.which("dbt")
    if found:
        return [found]

    return [sys.executable, "-m", "dbt.cli.main"]


def _dbt_env() -> Dict[str, str]:
    env = dict(os.environ)
    # Deterministic, parseable log lines and no ANSI escapes in the buffer.
    env["DBT_LOG_FORMAT"] = "default"
    env["NO_COLOR"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("DBT_SEND_ANONYMOUS_USAGE_STATS", "False")
    return env


# --------------------------------------------------------------------------
# launching
# --------------------------------------------------------------------------

class JobBusyError(RuntimeError):
    """Raised when a dbt run is already in flight."""


def launch(
    command: str,
    target: Optional[str] = None,
    **kwargs: Any,
) -> Job:
    """Start a dbt command in the background and return its Job immediately."""
    active = REGISTRY.active()
    if active is not None:
        raise JobBusyError(
            f"'{active.label}' is still running (job {active.id[:8]}). "
            f"dbt writes to a shared target/ directory, so runs are "
            f"serialised. Wait for it to finish or cancel it."
        )

    argv = build_argv(command, target=target, **kwargs)
    meta = ALLOWED_COMMANDS[command]

    job = Job(
        id=uuid.uuid4().hex,
        command=command,
        argv=argv,
        label=meta["label"],
        target=str(target or config.default_target_name()),
        writes=bool(meta["writes"]),
        _launcher_len=len(_dbt_launcher()),
    )
    REGISTRY.add(job)

    thread = threading.Thread(target=_run_job, args=(job,), daemon=True)
    thread.start()
    return job


def _run_job(job: Job) -> None:
    job.status = "running"
    job.started_at = time.time()
    job.append(f"$ {job.display_command}", stream="meta")
    job.append(f"  cwd: {config.PROJECT_DIR}", stream="meta")

    creation_flags = 0
    if os.name == "nt":
        # Own process group, so cancelling kills dbt's children too.
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(
            job.argv,
            cwd=str(config.PROJECT_DIR),
            env=_dbt_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creation_flags,
        )
    except Exception as exc:
        job.append(f"Failed to start dbt: {exc}", stream="stderr")
        job.status = "failed"
        job.exit_code = -1
        job.finished_at = time.time()
        return

    job.process = process

    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\r\n")
            if _is_noise(line):
                continue
            job.append(line)
        process.wait(timeout=config.SETTINGS.dbt_command_timeout)
    except subprocess.TimeoutExpired:
        job.append(
            f"Timed out after {config.SETTINGS.dbt_command_timeout}s. "
            f"Terminating.",
            stream="meta",
        )
        _terminate(process)
        job.status = "failed"
    except Exception as exc:  # pragma: no cover
        job.append(f"Error while reading dbt output: {exc}", stream="stderr")
    finally:
        job.exit_code = process.poll()
        job.finished_at = time.time()

        if job.status == "cancelled":
            pass
        elif job.exit_code == 0:
            job.status = "success"
        else:
            job.status = "failed"

        job.append(
            f"-- {job.label} finished: {job.status} "
            f"(exit {job.exit_code}) in {job.duration}s",
            stream="meta",
        )

        # Any command that can change target/ invalidates the cached manifest.
        manifest_mod.invalidate()


_NOISE_PATTERNS = (
    "UserWarning: Your application has authenticated using end user "
    "credentials",
    "warnings.warn(_CLOUD_SDK_CREDENTIALS_WARNING)",
    "https://cloud.google.com/docs/authentication/adc-troubleshooting",
)


def _is_noise(line: str) -> bool:
    """
    Drop the ADC quota-project warning.

    google-auth emits it on every invocation when signed in with a user
    account. It is harmless and it drowns the real output, so it is filtered
    here and surfaced once, as a fixable item, on the Overview page instead.
    """
    return any(pattern in line for pattern in _NOISE_PATTERNS)


def _terminate(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def cancel(job_id: str) -> bool:
    job = REGISTRY.get(job_id)
    if job is None or job.process is None:
        return False
    if job.status not in ("queued", "running"):
        return False
    job.status = "cancelled"
    job.append("-- cancellation requested from the UI", stream="meta")
    _terminate(job.process)
    return True

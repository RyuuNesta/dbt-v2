"""
schedules.py - unattended dbt runs via the Windows Task Scheduler.

Why Task Scheduler
------------------
The constraint is no new dependencies, so Celery and friends are out: they need
a broker, a worker process and a pip install. A cron-like thread inside the UI
would be worse than nothing, because it would only fire while somebody happened
to have the server open, which is precisely when a schedule is least needed.

Task Scheduler is already on the machine, survives reboots and logouts, and is
something a Windows admin can inspect without learning anything new. It is also
honest about what this is: a stopgap for a team migrating off dbt Cloud, not a
replacement for an orchestrator. If the machine is asleep at 06:00, nothing
runs and nothing tells you. That limitation is surfaced in the UI rather than
buried here.

Design notes
------------
- Each schedule gets a small .cmd wrapper in .runtime/tasks/. Task Scheduler's
  /TR argument takes one string, and nesting quoted paths and arguments inside it
  is a well-known source of tasks that register fine and then fail silently at
  runtime. Pointing /TR at a file sidesteps the quoting entirely, and leaves
  something the user can read and run by hand.

- Commands are validated through runner.build_argv at save time, not at fire
  time. A selector naming a blocked layer should be refused while somebody is
  looking at the screen, not at 06:00 into a log nobody reads.

- Registering a task is never automatic. It makes dbt run unattended against a
  real warehouse, so it takes an explicit action and shows the exact command
  first.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from . import config, runlock, runner

SCHEDULES_PATH = config.RUNTIME_DIR / "schedules.json"
RUNS_PATH = config.RUNTIME_DIR / "schedule_runs.json"
TASKS_DIR = config.RUNTIME_DIR / "tasks"
LOGS_DIR = config.RUNTIME_DIR / "schedule_logs"

# Enough history to spot a pattern, small enough to read and to keep the file
# cheap to rewrite on every append.
MAX_RUN_RECORDS = 200

# The prefix every task this UI creates shares, so they can be listed and
# cleaned up without touching anything else in the user's Task Scheduler.
TASK_PREFIX = "dbtStudio"

_lock = threading.Lock()


# --------------------------------------------------------------------------
# what may be scheduled
# --------------------------------------------------------------------------

# A subset of runner.ALLOWED_COMMANDS. `clean` would delete target/ under a
# running UI, and `debug` and `deps` are interactive troubleshooting rather than
# anything worth automating.
SCHEDULABLE_COMMANDS = ("build", "run", "test", "seed", "snapshot", "parse",
                        "source", "docs")

FREQUENCIES = {
    "HOURLY": {"label": "Every hour", "needs_time": False, "needs_day": False},
    "DAILY": {"label": "Every day", "needs_time": True, "needs_day": False},
    "WEEKLY": {"label": "Every week", "needs_time": True, "needs_day": True},
    "ONLOGON": {"label": "When you log on", "needs_time": False, "needs_day": False},
}

WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,58}$")


class ScheduleError(ValueError):
    """A schedule definition that cannot be saved as given."""


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def _read_json(path: pathlib.Path, fallback: Any) -> Any:
    if not path.is_file():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt file must not take the page down. Preserve it for inspection
        # rather than silently overwriting whatever the user had.
        try:
            path.replace(path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return fallback


def _write_json(path: pathlib.Path, payload: Any) -> None:
    config.ensure_runtime_dir()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then replace, so a crash mid-write cannot leave a truncated file
    # that the next read would quarantine.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def list_schedules() -> List[Dict[str, Any]]:
    with _lock:
        data = _read_json(SCHEDULES_PATH, {"schedules": []})
    schedules = data.get("schedules") or []
    schedules.sort(key=lambda s: str(s.get("name") or "").lower())
    return schedules


def get_schedule(schedule_id: str) -> Optional[Dict[str, Any]]:
    return next((s for s in list_schedules() if s.get("id") == schedule_id), None)


def _persist(schedules: List[Dict[str, Any]]) -> None:
    _write_json(SCHEDULES_PATH, {"schedules": schedules, "updated_at": time.time()})


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def validate(payload: Dict[str, Any],
             existing_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Normalise and check a schedule, raising ScheduleError on anything unusable.

    The dbt command line is assembled here as part of validation. build_argv is
    where blocked layers are excluded and selectors are checked, so running it
    now means a schedule that would be refused at 06:00 is refused at save time
    instead.
    """
    name = str(payload.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ScheduleError(
            "Give the schedule a name of 1-59 characters, using letters, digits, "
            "spaces, dots, hyphens or underscores. It becomes the Task Scheduler "
            "task name."
        )

    command = str(payload.get("command") or "build").strip().lower()
    if command not in SCHEDULABLE_COMMANDS:
        raise ScheduleError(
            f"'{command}' cannot be scheduled. Choose one of: "
            f"{', '.join(SCHEDULABLE_COMMANDS)}."
        )

    frequency = str(payload.get("frequency") or "DAILY").strip().upper()
    if frequency not in FREQUENCIES:
        raise ScheduleError(
            f"'{frequency}' is not a supported frequency. Choose one of: "
            f"{', '.join(FREQUENCIES)}."
        )

    spec = FREQUENCIES[frequency]

    at = str(payload.get("at") or "").strip()
    if spec["needs_time"]:
        if not _TIME_RE.match(at):
            raise ScheduleError("Give a start time as HH:MM on a 24-hour clock.")
    else:
        at = ""

    day = str(payload.get("day") or "").strip().upper()
    if spec["needs_day"]:
        if day not in WEEKDAYS:
            raise ScheduleError(f"Choose a day: {', '.join(WEEKDAYS)}.")
    else:
        day = ""

    target = str(payload.get("target") or config.default_target_name()).strip()
    known = {t.to_dict()["name"] for t in config.list_targets()}
    if target not in known:
        raise ScheduleError(
            f"'{target}' is not a target in profiles.yml. Known targets: "
            f"{', '.join(sorted(known))}."
        )

    select = str(payload.get("select") or "").strip()
    exclude = str(payload.get("exclude") or "").strip()
    threads = payload.get("threads")
    full_refresh = bool(payload.get("full_refresh"))

    # The real check: assemble the command the way the runner would. Anything
    # build_argv refuses, a schedule must refuse too.
    try:
        argv = runner.build_argv(
            command,
            target=target,
            select=select or None,
            exclude=exclude or None,
            full_refresh=full_refresh,
            threads=int(threads) if threads else None,
        )
    except runner.BlockedLayerError as exc:
        raise ScheduleError(str(exc)) from exc
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc

    return {
        "id": existing_id or uuid.uuid4().hex[:12],
        "name": name,
        "command": command,
        "target": target,
        "select": select,
        "exclude": exclude,
        "full_refresh": full_refresh,
        "threads": int(threads) if threads else None,
        "frequency": frequency,
        "at": at,
        "day": day,
        "enabled": bool(payload.get("enabled", True)),
        "argv": argv,
        "task_name": _task_name(name, existing_id or ""),
        "created_at": payload.get("created_at") or time.time(),
        "updated_at": time.time(),
    }


def _task_name(name: str, schedule_id: str) -> str:
    """
    The Task Scheduler task name.

    Prefixed so every task this UI owns is identifiable, and suffixed with the id
    so renaming a schedule cannot orphan the task it already registered.
    """
    safe = re.sub(r"[^A-Za-z0-9 _.-]+", "_", name).strip() or "run"
    return f"{TASK_PREFIX} {safe} [{schedule_id[:8]}]"


def save(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create or update a schedule and write its .cmd wrapper."""
    schedule_id = str(payload.get("id") or "").strip() or None
    record = validate(payload, existing_id=schedule_id)

    with _lock:
        data = _read_json(SCHEDULES_PATH, {"schedules": []})
        schedules = data.get("schedules") or []

        if any(s["name"].lower() == record["name"].lower()
               and s["id"] != record["id"] for s in schedules):
            raise ScheduleError(
                f"Another schedule is already called '{record['name']}'. "
                f"Task Scheduler names have to be unique."
            )

        schedules = [s for s in schedules if s.get("id") != record["id"]]
        schedules.append(record)
        _persist(schedules)

    write_wrapper(record)
    return record


def delete(schedule_id: str) -> bool:
    """Remove a schedule, its wrapper, and its registered task."""
    record = get_schedule(schedule_id)
    if record is None:
        return False

    # Unregister first: leaving a task pointing at a deleted wrapper would fail
    # at its next fire time with an error nobody would connect to this.
    try:
        unregister(record)
    except Exception:
        pass

    with _lock:
        data = _read_json(SCHEDULES_PATH, {"schedules": []})
        schedules = [s for s in (data.get("schedules") or [])
                     if s.get("id") != schedule_id]
        _persist(schedules)

    wrapper = TASKS_DIR / f"{schedule_id}.cmd"
    if wrapper.is_file():
        try:
            wrapper.unlink()
        except OSError:
            pass

    return True


# --------------------------------------------------------------------------
# the .cmd wrapper
# --------------------------------------------------------------------------

def wrapper_path(schedule_id: str) -> pathlib.Path:
    return TASKS_DIR / f"{schedule_id}.cmd"


def write_wrapper(record: Dict[str, Any]) -> pathlib.Path:
    """
    A batch file that runs one schedule.

    Task Scheduler gets a path to this rather than a command line with nested
    quotes. It is also directly runnable, which makes "does this actually work"
    answerable without waiting for a trigger.
    """
    TASKS_DIR.mkdir(parents=True, exist_ok=True)
    path = wrapper_path(record["id"])

    entry = config.UI_DIR / "run_scheduled.py"
    lines = [
        "@echo off",
        f"rem {record['name']}",
        f"rem dbt {record['command']} on target {record['target']}",
        "rem Generated by dbt Studio. Edit the schedule in the UI, not this file:",
        "rem it is rewritten whenever the schedule is saved.",
        "setlocal",
        f'cd /d "{config.PROJECT_DIR}"',
        f'"{sys.executable}" "{entry}" {record["id"]}',
        "exit /b %ERRORLEVEL%",
        "",
    ]
    # Joined with \n and translated on write. Writing "\r\n" directly in text
    # mode would be translated again into "\r\r\n", which cmd.exe tolerates but
    # is wrong, and shows up as blank lines in any editor.
    path.write_text("\n".join(lines), encoding="utf-8", newline="\r\n")
    return path


# --------------------------------------------------------------------------
# Task Scheduler
# --------------------------------------------------------------------------

def schtasks_create_argv(record: Dict[str, Any]) -> List[str]:
    """The exact schtasks command line, as argv."""
    argv = [
        "schtasks", "/Create",
        "/TN", record["task_name"],
        "/TR", str(wrapper_path(record["id"])),
        "/SC", record["frequency"],
        "/F",
    ]
    if record.get("at"):
        argv += ["/ST", record["at"]]
    if record.get("day"):
        argv += ["/D", record["day"]]
    return argv


def schtasks_delete_argv(record: Dict[str, Any]) -> List[str]:
    return ["schtasks", "/Delete", "/TN", record["task_name"], "/F"]


def command_preview(record: Dict[str, Any]) -> Dict[str, str]:
    """
    Copy-pasteable forms of everything this schedule involves.

    list2cmdline is what Python itself uses to turn argv into a Windows command
    line, so the displayed string is the one that would actually run rather than
    an approximation assembled by hand.
    """
    return {
        "register": subprocess.list2cmdline(schtasks_create_argv(record)),
        "unregister": subprocess.list2cmdline(schtasks_delete_argv(record)),
        "dbt": subprocess.list2cmdline(record.get("argv") or []),
        "wrapper": str(wrapper_path(record["id"])),
        "run_now": subprocess.list2cmdline([str(wrapper_path(record["id"]))]),
    }


def _run_schtasks(argv: List[str], timeout: int = 60) -> Tuple[int, str]:
    if os.name != "nt":
        raise ScheduleError(
            "Task Scheduler is only available on Windows. On another platform, "
            "point cron at the .cmd wrapper's contents instead."
        )
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise ScheduleError(
            "Could not find schtasks.exe. It ships with Windows; if it is "
            "missing, register the task through the Task Scheduler UI using the "
            "command shown here."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ScheduleError("schtasks did not respond within 60 seconds.") from exc

    output = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, output


def register(record: Dict[str, Any]) -> Dict[str, Any]:
    """Create or replace the Windows task for this schedule."""
    write_wrapper(record)
    code, output = _run_schtasks(schtasks_create_argv(record))

    if code != 0:
        hint = ""
        if "Access is denied" in output:
            hint = (
                " Task Scheduler refused the request. Creating a task for your "
                "own account normally needs no elevation, so this usually means "
                "group policy is blocking it. Ask whoever manages the machine, "
                "or create the task by hand with the command shown here."
            )
        raise ScheduleError(
            f"schtasks could not create the task (exit {code}). "
            f"{output[:300]}{hint}"
        )

    return {"registered": True, "output": output,
            "task_name": record["task_name"]}


def unregister(record: Dict[str, Any]) -> Dict[str, Any]:
    code, output = _run_schtasks(schtasks_delete_argv(record))
    # A task that is already gone is the desired end state, not a failure.
    if code != 0 and "cannot find" not in output.lower():
        raise ScheduleError(
            f"schtasks could not delete the task (exit {code}). {output[:300]}"
        )
    return {"registered": False, "output": output}


def task_status(record: Dict[str, Any]) -> Dict[str, Any]:
    """
    What Task Scheduler itself says about this task.

    Queried live rather than cached, because the UI's schedules.json is only a
    record of intent: somebody can delete or disable the task in Task Scheduler
    and the UI would otherwise keep claiming it is scheduled.
    """
    if os.name != "nt":
        return {"registered": False, "detail": "not Windows"}

    argv = ["schtasks", "/Query", "/TN", record["task_name"], "/FO", "LIST"]
    try:
        code, output = _run_schtasks(argv, timeout=30)
    except ScheduleError as exc:
        return {"registered": False, "detail": str(exc)}

    if code != 0:
        return {"registered": False, "detail": "no such task"}

    fields: Dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition(":")
        if value.strip():
            fields[key.strip().lower()] = value.strip()

    return {
        "registered": True,
        "state": fields.get("status") or fields.get("scheduled task state"),
        "next_run": fields.get("next run time"),
        "last_run": fields.get("last run time"),
        "last_result": fields.get("last result"),
    }


# --------------------------------------------------------------------------
# run history
# --------------------------------------------------------------------------

_SUMMARY_RE = re.compile(
    r"PASS=(\d+)\s+WARN=(\d+)\s+ERROR=(\d+)\s+SKIP=(\d+)", re.IGNORECASE)


def parse_summary(text: str) -> Dict[str, int]:
    """Counts from dbt's closing 'Done.' line, if it got that far."""
    match = None
    for match in _SUMMARY_RE.finditer(text or ""):
        pass
    if not match:
        return {}
    return {
        "pass": int(match.group(1)),
        "warn": int(match.group(2)),
        "error": int(match.group(3)),
        "skip": int(match.group(4)),
    }


def record_run(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Append one run to the history, oldest trimmed first."""
    with _lock:
        data = _read_json(RUNS_PATH, {"runs": []})
        runs = data.get("runs") or []
        runs.append(entry)
        runs = runs[-MAX_RUN_RECORDS:]
        _write_json(RUNS_PATH, {"runs": runs, "updated_at": time.time()})
    return entry


def list_runs(schedule_id: Optional[str] = None,
              limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        data = _read_json(RUNS_PATH, {"runs": []})
    runs = data.get("runs") or []
    if schedule_id:
        runs = [r for r in runs if r.get("schedule_id") == schedule_id]
    runs.sort(key=lambda r: r.get("started_at") or 0, reverse=True)
    return runs[:limit]


def run_log(log_name: str) -> str:
    """
    The full output of one run.

    The name is resolved inside LOGS_DIR and checked, so a crafted value cannot
    read arbitrary files through this.
    """
    candidate = (LOGS_DIR / log_name).resolve()
    try:
        candidate.relative_to(LOGS_DIR.resolve())
    except ValueError as exc:
        raise ScheduleError("That log is not inside the schedule log directory.") from exc
    if not candidate.is_file():
        raise ScheduleError("That log no longer exists.")
    return candidate.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------
# describing it all to the UI
# --------------------------------------------------------------------------

def describe(record: Dict[str, Any], with_status: bool = True) -> Dict[str, Any]:
    out = dict(record)
    out["commands"] = command_preview(record)
    out["wrapper_exists"] = wrapper_path(record["id"]).is_file()
    out["schedule_text"] = human_schedule(record)
    if with_status:
        out["task"] = task_status(record)
    return out


def human_schedule(record: Dict[str, Any]) -> str:
    frequency = record.get("frequency")
    at = record.get("at")
    day = record.get("day")
    if frequency == "HOURLY":
        return "Every hour"
    if frequency == "DAILY":
        return f"Every day at {at}"
    if frequency == "WEEKLY":
        return f"Every {day.title() if day else 'week'} at {at}"
    if frequency == "ONLOGON":
        return "Every time you log on"
    return str(frequency)


def environment_notes() -> List[Dict[str, str]]:
    """
    The caveats that matter, stated once and shown in the UI.

    These are the things that make local scheduling different from an
    orchestrator, and each one has bitten somebody in production.
    """
    return [
        {
            "kind": "warn",
            "title": "The machine has to be awake",
            "body": "Task Scheduler cannot run dbt on a laptop that is asleep, "
                    "shut down or disconnected. A missed run is not retried and "
                    "nothing notifies you - the run history below simply has a "
                    "gap. If a schedule genuinely matters, it belongs on a server "
                    "or in Cloud Scheduler, not here.",
        },
        {
            "kind": "warn",
            "title": "It runs as you, with your credentials",
            "body": "Scheduled runs use the same Application Default Credentials "
                    "you authenticated with. If those expire or are revoked, every "
                    "run fails with an authentication error until you run "
                    "'gcloud auth application-default login' again.",
        },
        {
            "kind": "info",
            "title": "Gold is excluded, here as everywhere",
            "body": "Scheduled commands are assembled by the same code as manual "
                    "ones, so the gold layer is excluded and a selector naming it "
                    "is refused when you save rather than when it fires.",
        },
        {
            "kind": "info",
            "title": "Runs never overlap",
            "body": "A scheduled run that starts while another dbt process is "
                    "working will record itself as skipped and exit. Two dbt "
                    "processes writing one project's target directory can leave a "
                    "half-written manifest.",
        },
    ]

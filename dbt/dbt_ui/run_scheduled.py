"""
run_scheduled.py - the entry point Windows Task Scheduler invokes.

    python dbt_ui/run_scheduled.py <schedule_id>

Runs one saved schedule, writes the full dbt output to a log file, appends a
summary to schedule_runs.json, and exits with dbt's own exit code so Task
Scheduler's "Last Result" column means something.

Deliberately standalone: it imports the backend package but never starts the web
server, so a scheduled run does not depend on anyone having the UI open. It is
also runnable by hand, which is the fastest way to find out whether a schedule
works without waiting for its trigger.
"""

from __future__ import annotations

import datetime
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backend import config, runlock, runner, schedules  # noqa: E402


def _stamp(when: float) -> str:
    return datetime.datetime.fromtimestamp(when).strftime("%Y%m%d-%H%M%S")


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: run_scheduled.py <schedule_id>", file=sys.stderr)
        return 2

    schedule_id = argv[1].strip()
    record = schedules.get_schedule(schedule_id)

    if record is None:
        print(f"No schedule with id '{schedule_id}'. It may have been deleted "
              f"while its Task Scheduler entry survived.", file=sys.stderr)
        schedules.record_run({
            "schedule_id": schedule_id,
            "name": f"(deleted schedule {schedule_id})",
            "status": "error",
            "started_at": time.time(),
            "finished_at": time.time(),
            "duration": 0,
            "exit_code": 2,
            "error": "The schedule no longer exists. Delete the Windows task, "
                     "or recreate the schedule in dbt Studio.",
        })
        return 2

    if not record.get("enabled", True):
        print(f"Schedule '{record['name']}' is disabled; nothing to do.")
        schedules.record_run({
            "schedule_id": schedule_id,
            "name": record["name"],
            "command": record["command"],
            "target": record["target"],
            "status": "skipped",
            "started_at": time.time(),
            "finished_at": time.time(),
            "duration": 0,
            "exit_code": 0,
            "error": "Schedule is disabled in dbt Studio.",
        })
        return 0

    # Rebuild the command rather than trusting the argv stored on the record.
    # The stored copy is for display; regenerating it means a change to the
    # blocked-layer rules takes effect on the next run instead of needing every
    # schedule to be re-saved.
    try:
        argv_dbt = runner.build_argv(
            record["command"],
            target=record.get("target"),
            select=record.get("select") or None,
            exclude=record.get("exclude") or None,
            full_refresh=bool(record.get("full_refresh")),
            threads=record.get("threads"),
        )
    except ValueError as exc:
        print(f"Refusing to run: {exc}", file=sys.stderr)
        schedules.record_run({
            "schedule_id": schedule_id,
            "name": record["name"],
            "command": record["command"],
            "target": record.get("target"),
            "status": "error",
            "started_at": time.time(),
            "finished_at": time.time(),
            "duration": 0,
            "exit_code": 2,
            "error": f"The saved command is no longer valid: {exc}",
        })
        return 2

    started = time.time()

    # Skipping is the correct outcome, not a failure, so this exits 0. Task
    # Scheduler would otherwise flag a healthy machine as broken every time a
    # schedule happened to overlap a manual build.
    if not runlock.acquire(f"schedule:{record['name']}"):
        held = runlock.read() or {}
        message = (f"Another dbt process is already running "
                   f"({held.get('owner', 'unknown')}, pid {held.get('pid')}). "
                   f"Skipped to avoid two processes writing target/ at once.")
        print(message)
        schedules.record_run({
            "schedule_id": schedule_id,
            "name": record["name"],
            "command": record["command"],
            "target": record.get("target"),
            "status": "skipped",
            "started_at": started,
            "finished_at": time.time(),
            "duration": 0,
            "exit_code": 0,
            "error": message,
        })
        return 0

    schedules.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_name = f"{_stamp(started)}_{schedule_id}.log"
    log_path = schedules.LOGS_DIR / log_name

    header = [
        f"schedule : {record['name']}  ({schedule_id})",
        f"started  : {datetime.datetime.fromtimestamp(started).isoformat(timespec='seconds')}",
        f"command  : {subprocess.list2cmdline(argv_dbt)}",
        f"target   : {record.get('target')}",
        "-" * 78,
        "",
    ]

    collected: list = []
    exit_code = 1

    try:
        with subprocess.Popen(
            argv_dbt,
            cwd=str(config.PROJECT_DIR),
            env=runner._dbt_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        ) as process:
            for line in process.stdout:
                line = line.rstrip("\n")
                collected.append(line)
                # Echoed so Task Scheduler's own transcript, if the user has it
                # capturing output, is not empty.
                print(line)
            exit_code = process.wait()
    except FileNotFoundError as exc:
        collected.append(f"Could not start dbt: {exc}")
        exit_code = 127
    except Exception as exc:  # pragma: no cover
        collected.append(f"Unexpected failure while running dbt: {exc}")
        exit_code = 1
    finally:
        runlock.release()

    finished = time.time()
    body = "\n".join(header + collected) + "\n"
    try:
        log_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        print(f"Could not write the log: {exc}", file=sys.stderr)
        log_name = ""

    text = "\n".join(collected)
    counts = schedules.parse_summary(text)

    # dbt exits non-zero for a failed test as well as an outright error, and the
    # difference matters when reading a history. WARN alone is still a pass.
    if exit_code == 0:
        status = "success"
    elif counts.get("error"):
        status = "failed"
    else:
        status = "error"

    error_line = ""
    if exit_code != 0:
        candidates = [line for line in collected
                      if "Error" in line or "Failure" in line or "Compilation" in line]
        error_line = (candidates[-1] if candidates
                      else (collected[-1] if collected else "dbt produced no output."))

    schedules.record_run({
        "schedule_id": schedule_id,
        "name": record["name"],
        "command": record["command"],
        "target": record.get("target"),
        "select": record.get("select"),
        "status": status,
        "started_at": started,
        "finished_at": finished,
        "duration": round(finished - started, 2),
        "exit_code": exit_code,
        "counts": counts,
        "log": log_name,
        "line_count": len(collected),
        "error": error_line[:400],
    })

    print(f"\n{status} in {finished - started:.1f}s (exit {exit_code})")
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))

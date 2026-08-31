"""
Throwaway: schedules + runlock, including one real Task Scheduler registration.

Registers a task, queries it, runs the wrapper for real, then unregisters and
removes everything.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import config, runlock, schedules

passed = failed = 0
created_ids = []


def check(label, cond, extra=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'pass' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")


# Keep the user's real schedules out of harm's way.
BACKUP = None
if schedules.SCHEDULES_PATH.is_file():
    BACKUP = schedules.SCHEDULES_PATH.read_text(encoding="utf-8")
    print(f"backed up existing schedules.json ({len(BACKUP)} bytes)")

try:
    print("=== validation ===")
    good = {
        "name": "Nightly bronze build",
        "command": "build",
        "target": "dev",
        "select": "tag:bronze",
        "frequency": "DAILY",
        "at": "06:00",
    }
    record = schedules.validate(good)
    check("valid schedule accepted", record["name"] == "Nightly bronze build")
    check("id generated", len(record["id"]) == 12, record["id"])
    check("argv built", record["argv"][1] == "build", record["argv"][:3])
    check("gold excluded automatically",
          "tag:gold" in record["argv"], record["argv"])
    check("target passed through", "--target" in record["argv"])
    check("task name prefixed", record["task_name"].startswith("dbtStudio"),
          record["task_name"])
    check("human text", record["at"] in schedules.human_schedule(record),
          schedules.human_schedule(record))

    print("\n=== validation rejects bad input ===")
    bad_cases = [
        ({**good, "name": ""}, "empty name"),
        ({**good, "name": "x" * 70}, "over-long name"),
        ({**good, "command": "clean"}, "unschedulable command"),
        ({**good, "command": "nonsense"}, "unknown command"),
        ({**good, "frequency": "FORTNIGHTLY"}, "unknown frequency"),
        ({**good, "at": "25:00"}, "invalid hour"),
        ({**good, "at": "6:00"}, "unpadded time"),
        ({**good, "at": ""}, "missing time for DAILY"),
        ({**good, "frequency": "WEEKLY", "day": ""}, "missing day for WEEKLY"),
        ({**good, "frequency": "WEEKLY", "day": "FUNDAY"}, "bad day"),
        ({**good, "target": "nope"}, "unknown target"),
        ({**good, "select": "tag:gold"}, "selector naming gold"),
        ({**good, "select": "gold_gl_monthly_summary"}, "gold model by name"),
    ]
    for payload, label in bad_cases:
        try:
            schedules.validate(payload)
            check(label + " refused", False, "accepted")
        except schedules.ScheduleError as exc:
            check(label + " refused", True, str(exc)[:52])

    print("\n=== frequencies that need no time ===")
    for frequency in ("HOURLY", "ONLOGON"):
        rec = schedules.validate({**good, "frequency": frequency, "at": ""})
        check(f"{frequency} accepted without a time", rec["frequency"] == frequency)
        check(f"{frequency} clears the time field", rec["at"] == "")

    rec = schedules.validate({**good, "frequency": "WEEKLY", "day": "mon"})
    check("weekly day upper-cased", rec["day"] == "MON", rec["day"])

    print("\n=== save, list, wrapper ===")
    saved = schedules.save(good)
    created_ids.append(saved["id"])
    check("saved", saved["id"] in [s["id"] for s in schedules.list_schedules()])
    wrapper = schedules.wrapper_path(saved["id"])
    check("wrapper written", wrapper.is_file(), str(wrapper))
    body = wrapper.read_text(encoding="utf-8")
    check("wrapper cds to the project", str(config.PROJECT_DIR) in body)
    check("wrapper calls run_scheduled", "run_scheduled.py" in body)
    check("wrapper passes the id", saved["id"] in body)
    check("wrapper propagates the exit code", "exit /b %ERRORLEVEL%" in body)
    # Read as bytes: read_text would translate the line endings away.
    raw = wrapper.read_bytes()
    check("crlf line endings", b"\r\n" in raw)
    check("no doubled carriage returns", b"\r\r" not in raw,
          raw[:60])

    print("\n=== duplicate name refused ===")
    try:
        schedules.save({**good, "name": "nightly bronze build"})
        check("case-insensitive duplicate refused", False, "accepted")
    except schedules.ScheduleError as exc:
        check("case-insensitive duplicate refused", True, str(exc)[:50])

    print("\n=== update keeps the id ===")
    updated = schedules.save({**saved, "at": "07:30"})
    check("same id", updated["id"] == saved["id"])
    check("time changed", updated["at"] == "07:30")
    check("still one schedule", len(schedules.list_schedules()) == 1,
          len(schedules.list_schedules()))

    print("\n=== command preview ===")
    preview = schedules.command_preview(updated)
    for key in ("register", "unregister", "dbt", "wrapper", "run_now"):
        check(f"{key} rendered", bool(preview[key]), preview[key][:70])
    check("register uses schtasks /Create", "/Create" in preview["register"])
    check("quoted task name", '"' in preview["register"])
    check("dbt command shows the exclusion", "tag:gold" in preview["dbt"])

    print("\n=== runlock ===")
    runlock.release()
    check("starts free", runlock.read() is None)
    check("acquired", runlock.acquire("test") is True)
    held = runlock.read()
    check("holder recorded", held and held["owner"] == "test", held)
    check("pid is this process", held["pid"] == os.getpid())
    check("second acquire refused", runlock.acquire("other") is False)
    described = runlock.describe()
    check("describe reports held", described["held"] is True, described)
    runlock.release()
    check("released", runlock.read() is None)
    check("describe reports free", runlock.describe()["held"] is False)

    print("\n  stale lock (dead pid) is ignored:")
    runlock.LOCK_PATH.write_text(json.dumps({
        "pid": 999999, "owner": "ghost", "started_at": time.time(),
    }), encoding="utf-8")
    check("dead holder treated as free", runlock.read() is None)
    check("can acquire over it", runlock.acquire("live") is True)
    runlock.release()

    print("\n  aged-out lock is ignored:")
    runlock.LOCK_PATH.write_text(json.dumps({
        "pid": os.getpid(), "owner": "ancient",
        "started_at": time.time() - runlock.STALE_SECONDS - 60,
    }), encoding="utf-8")
    check("stale holder treated as free", runlock.read() is None)
    runlock.release()

    print("\n  corrupt lock does not wedge it:")
    runlock.LOCK_PATH.write_text("{ not json", encoding="utf-8")
    check("corrupt treated as free", runlock.read() is None)
    check("can acquire", runlock.acquire("after-corrupt") is True)
    runlock.release()

    print("\n=== summary parsing ===")
    counts = schedules.parse_summary(
        "04:41:42  Done. PASS=41 WARN=0 ERROR=0 SKIP=0 NO-OP=0 TOTAL=41")
    check("parsed", counts == {"pass": 41, "warn": 0, "error": 0, "skip": 0}, counts)
    counts = schedules.parse_summary("nothing useful here")
    check("no summary is empty, not an error", counts == {}, counts)
    counts = schedules.parse_summary(
        "Done. PASS=1 WARN=0 ERROR=0 SKIP=0\nDone. PASS=9 WARN=1 ERROR=2 SKIP=3")
    check("last summary wins", counts["pass"] == 9 and counts["error"] == 2, counts)

    print("\n=== run history ===")
    schedules.record_run({
        "schedule_id": saved["id"], "name": "probe", "status": "success",
        "started_at": time.time(), "exit_code": 0,
    })
    runs = schedules.list_runs(schedule_id=saved["id"])
    check("recorded", len(runs) >= 1)
    check("filtered by id", all(r["schedule_id"] == saved["id"] for r in runs))
    check("newest first", runs == sorted(runs, key=lambda r: -r["started_at"]))

    print("\n=== log path traversal is refused ===")
    for evil in ("../../schedules.json", "..\\..\\secrets.txt",
                 "C:/Windows/System32/drivers/etc/hosts"):
        try:
            schedules.run_log(evil)
            check(f"refused {evil[:26]}", False, "read it")
        except schedules.ScheduleError:
            check(f"refused {evil[:26]}", True)

    if os.name == "nt":
        print("\n=== REAL Task Scheduler registration ===")
        try:
            result = schedules.register(updated)
            check("registered", result["registered"] is True, result["output"][:70])

            status = schedules.task_status(updated)
            check("task is queryable", status["registered"] is True, status)
            check("next run reported", bool(status.get("next_run")),
                  status.get("next_run"))
            print(f"    state    : {status.get('state')}")
            print(f"    next run : {status.get('next_run')}")

            print("\n  --- running the wrapper for real ---")
            proc = subprocess.run([str(schedules.wrapper_path(updated["id"]))],
                                  capture_output=True, text=True, timeout=900)
            tail = [l for l in (proc.stdout or "").splitlines() if l.strip()]
            for line in tail[-10:]:
                print(f"    {line}")
            check("wrapper ran", proc.returncode is not None,
                  f"exit {proc.returncode}")

            history = schedules.list_runs(schedule_id=updated["id"], limit=3)
            latest = history[0]
            check("run was recorded", latest["name"] == updated["name"], latest.get("name"))
            check("status is meaningful",
                  latest["status"] in ("success", "failed", "error", "skipped"),
                  latest["status"])
            check("duration recorded", latest.get("duration", 0) >= 0)
            check("log file referenced", bool(latest.get("log")), latest.get("log"))
            if latest.get("log"):
                text = schedules.run_log(latest["log"])
                check("log readable", len(text) > 0, f"{len(text)} chars")
                check("log has the header", "schedule :" in text)
                check("log records the command", "--exclude" in text)
            if latest["status"] == "success":
                check("counts parsed from a real run", bool(latest.get("counts")),
                      latest.get("counts"))
            print(f"    status={latest['status']} exit={latest['exit_code']} "
                  f"counts={latest.get('counts')}")

            check("lock released after the run", runlock.read() is None)

        finally:
            print("\n  --- unregistering ---")
            out = schedules.unregister(updated)
            check("unregistered", out["registered"] is False)
            after = schedules.task_status(updated)
            check("task is gone", after["registered"] is False, after)
            check("unregister is idempotent",
                  schedules.unregister(updated)["registered"] is False)
    else:
        print("\n(not Windows: skipping the real registration)")

    print("\n=== delete cleans up ===")
    check("deleted", schedules.delete(updated["id"]) is True)
    check("gone from the list", not schedules.get_schedule(updated["id"]))
    check("wrapper removed", not schedules.wrapper_path(updated["id"]).is_file())
    check("deleting again is False", schedules.delete(updated["id"]) is False)
    created_ids.clear()

finally:
    print("\n=== restore ===")
    for schedule_id in created_ids:
        try:
            schedules.delete(schedule_id)
            print(f"  removed leftover schedule {schedule_id}")
        except Exception as exc:
            print(f"  could not remove {schedule_id}: {exc}")

    if BACKUP is not None:
        schedules.SCHEDULES_PATH.write_text(BACKUP, encoding="utf-8")
        print("  restored the original schedules.json")
    elif schedules.SCHEDULES_PATH.is_file():
        schedules.SCHEDULES_PATH.unlink()
        print("  removed the schedules.json created by this test")

    runlock.release()

print(f"\n{'=' * 62}\n  {passed} passed, {failed} failed\n{'=' * 62}")
sys.exit(1 if failed else 0)

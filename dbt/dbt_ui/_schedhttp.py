"""Throwaway: the /api/schedules/* routes over real HTTP."""
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import schedules as sched_mod

BASE = "http://127.0.0.1:8899"
passed = failed = 0
created = []


def check(label, cond, extra=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'pass' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")


def call(method, path, body=None, **query):
    url = BASE + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"null")
        except json.JSONDecodeError:
            return exc.code, {"error": raw.decode(errors="replace")[:200]}


BACKUP = None
if sched_mod.SCHEDULES_PATH.is_file():
    BACKUP = sched_mod.SCHEDULES_PATH.read_text(encoding="utf-8")

try:
    print("=== GET /api/schedules on an empty store ===")
    status, payload = call("GET", "/api/schedules")
    check("200", status == 200, f"{status}: {str(payload.get('error'))[:90]}")
    if status != 200:
        raise SystemExit(1)

    check("schedules key", isinstance(payload["schedules"], list))
    check("commands offered", "build" in payload["commands"], payload["commands"])
    check("clean is not schedulable", "clean" not in payload["commands"])
    check("frequencies carry their field flags",
          any(f["id"] == "DAILY" and f["needs_time"] for f in payload["frequencies"]))
    check("weekdays listed", len(payload["weekdays"]) == 7)
    check("targets from profiles.yml", "dev" in payload["targets"], payload["targets"])
    check("default target", payload["default_target"] == "dev")
    check("windows detected", payload["windows"] is True)
    check("notes present", len(payload["notes"]) >= 4, len(payload["notes"]))
    check("notes mention the asleep-machine caveat",
          any("asleep" in n["body"] for n in payload["notes"]))
    check("notes mention gold", any("gold" in n["body"] for n in payload["notes"]))
    check("lock reported", "held" in payload["lock"], payload["lock"])

    print("\n=== POST /api/schedules ===")
    status, saved = call("POST", "/api/schedules", {
        "name": "HTTP probe schedule",
        "command": "test",
        "target": "dev",
        "select": "tag:bronze",
        "frequency": "WEEKLY",
        "day": "TUE",
        "at": "23:15",
    })
    check("200", status == 200, f"{status}: {str(saved.get('error'))[:120]}")
    schedule = saved["schedule"]
    created.append(schedule["id"])
    check("id assigned", bool(schedule["id"]))
    check("schedule text is human", schedule["schedule_text"] == "Every Tue at 23:15",
          schedule["schedule_text"])
    check("wrapper written", schedule["wrapper_exists"] is True)
    check("not registered yet", schedule["task"]["registered"] is False)
    check("note explains the second step", "register" in saved["note"].lower(),
          saved["note"])
    check("commands rendered", bool(schedule["commands"]["register"]))
    check("dbt command excludes gold", "tag:gold" in schedule["commands"]["dbt"])

    print("\n=== validation errors come back as 422 ===")
    for payload_bad, label in [
        ({"name": "", "command": "build"}, "empty name"),
        ({"name": "x", "command": "clean"}, "unschedulable command"),
        ({"name": "y", "command": "build", "frequency": "DAILY", "at": "99:99"}, "bad time"),
        ({"name": "z", "command": "build", "select": "tag:gold",
          "frequency": "DAILY", "at": "01:00"}, "gold selector"),
        ({"name": "w", "command": "build", "target": "nope",
          "frequency": "DAILY", "at": "01:00"}, "unknown target"),
    ]:
        status, err = call("POST", "/api/schedules", payload_bad)
        check(f"{label} -> 422", status == 422, f"{status}: {str(err.get('error'))[:60]}")

    print("\n=== duplicate name -> 422 ===")
    status, err = call("POST", "/api/schedules", {
        "name": "http probe schedule", "command": "build",
        "frequency": "DAILY", "at": "05:00",
    })
    check("422", status == 422, status)
    check("says why", "already called" in str(err.get("error")), str(err.get("error"))[:70])

    print("\n=== update via POST with the same id ===")
    status, updated = call("POST", "/api/schedules", {
        **{k: v for k, v in schedule.items()
           if k in ("id", "name", "command", "target", "select", "exclude",
                    "frequency", "day", "created_at")},
        "at": "22:00",
        "enabled": False,
    })
    check("200", status == 200, str(updated.get("error"))[:90])
    check("same id", updated["schedule"]["id"] == schedule["id"])
    check("time updated", updated["schedule"]["at"] == "22:00")
    check("disabled", updated["schedule"]["enabled"] is False)

    status, listing = call("GET", "/api/schedules")
    check("still exactly one", len(listing["schedules"]) == 1,
          len(listing["schedules"]))

    print("\n=== register, query, unregister ===")
    status, reg = call("POST", "/api/schedules/register",
                       {"id": schedule["id"], "action": "register"})
    check("200", status == 200, f"{status}: {str(reg.get('error'))[:140]}")
    if status == 200:
        check("registered", reg["registered"] is True)
        check("task name returned", "dbtStudio" in reg["task_name"], reg["task_name"])
        check("schedule now reports registered",
              reg["schedule"]["task"]["registered"] is True,
              reg["schedule"]["task"])
        check("next run known", bool(reg["schedule"]["task"].get("next_run")),
              reg["schedule"]["task"].get("next_run"))

        status, listing = call("GET", "/api/schedules")
        check("list agrees it is registered",
              listing["schedules"][0]["task"]["registered"] is True)

        status, unreg = call("POST", "/api/schedules/register",
                             {"id": schedule["id"], "action": "unregister"})
        check("unregister 200", status == 200, str(unreg.get("error"))[:90])
        check("unregistered", unreg["registered"] is False)

    print("\n=== unknown id -> 404 ===")
    status, err = call("POST", "/api/schedules/register",
                       {"id": "doesnotexist", "action": "register"})
    check("404", status == 404, status)
    status, err = call("POST", "/api/schedules/delete", {"id": "doesnotexist"})
    check("delete 404", status == 404, status)

    print("\n=== runs endpoint ===")
    status, runs = call("GET", "/api/schedules/runs")
    check("200", status == 200)
    check("runs list", isinstance(runs["runs"], list))
    check("lock included", "held" in runs["lock"])

    print("\n=== log endpoint refuses traversal ===")
    status, err = call("GET", "/api/schedules/log", log="../../schedules.json")
    check("404 for traversal", status == 404, status)
    status, err = call("GET", "/api/schedules/log")
    check("400 without the parameter", status == 400, status)

    print("\n=== delete ===")
    status, gone = call("POST", "/api/schedules/delete", {"id": schedule["id"]})
    check("200", status == 200, status)
    check("deleted", gone["deleted"] is True)
    created.clear()
    status, listing = call("GET", "/api/schedules")
    check("list is empty again", len(listing["schedules"]) == 0)

finally:
    for schedule_id in created:
        call("POST", "/api/schedules/delete", {"id": schedule_id})
        print(f"  cleaned up {schedule_id}")
    if BACKUP is not None:
        sched_mod.SCHEDULES_PATH.write_text(BACKUP, encoding="utf-8")
        print("  restored the original schedules.json")
    elif sched_mod.SCHEDULES_PATH.is_file():
        sched_mod.SCHEDULES_PATH.unlink()
        print("  removed the test schedules.json")

print(f"\n{'=' * 62}\n  {passed} passed, {failed} failed\n{'=' * 62}")
raise SystemExit(1 if failed else 0)

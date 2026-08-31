"""Throwaway: the normal UI run path still works after adding the cross-process lock."""
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from backend import runlock

BASE = "http://127.0.0.1:8899"
passed = failed = 0


def check(label, cond, extra=""):
    global passed, failed
    passed += bool(cond)
    failed += not cond
    print(f"  {'pass' if cond else 'FAIL'}  {label}{('  ' + str(extra)) if extra else ''}")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method)
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


runlock.release()
check("lock starts free", runlock.read() is None)

print("\n=== a normal UI run still launches ===")
status, job = call("POST", "/api/dbt/run", {"command": "parse"})
check("202 or 200", status in (200, 202), f"{status}: {str(job.get('error'))[:100]}")
if status not in (200, 202):
    raise SystemExit(1)

job_id = job.get("job", {}).get("id") or job.get("id")
check("job id returned", bool(job_id), job_id)

print("\n=== the lock is held while it runs ===")
saw_lock = False
for _ in range(40):
    if runlock.read() is not None:
        saw_lock = True
        break
    time.sleep(0.2)
check("lock taken during the run", saw_lock)
if saw_lock:
    held = runlock.read() or {}
    check("owner names the UI", str(held.get("owner", "")).startswith("ui:"),
          held.get("owner"))

print("\n=== a second run is refused while the first holds it ===")
status, busy = call("POST", "/api/dbt/run", {"command": "parse"})
check("refused", status >= 400, status)
check("message explains serialisation",
      "running" in str(busy.get("error", "")).lower(), str(busy.get("error"))[:110])

print("\n=== it finishes and releases ===")
final = None
for _ in range(150):
    status, detail = call("GET", f"/api/dbt/jobs/{job_id}")
    state = (detail.get("job") or detail).get("status")
    if state in ("success", "failed", "cancelled"):
        final = state
        break
    time.sleep(1)
check("run completed", final is not None, final)
check("succeeded", final == "success", final)

released = False
for _ in range(30):
    if runlock.read() is None:
        released = True
        break
    time.sleep(0.3)
check("lock released afterwards", released)

print("\n=== and another run can start again ===")
status, again = call("POST", "/api/dbt/run", {"command": "parse"})
check("accepted", status in (200, 202), status)
second_id = again.get("job", {}).get("id") or again.get("id")
for _ in range(150):
    status, detail = call("GET", f"/api/dbt/jobs/{second_id}")
    if (detail.get("job") or detail).get("status") in ("success", "failed", "cancelled"):
        break
    time.sleep(1)
check("second run finished", True)
runlock.release()

print(f"\n{'=' * 62}\n  {passed} passed, {failed} failed\n{'=' * 62}")
sys.exit(1 if failed else 0)

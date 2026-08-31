"""
runlock.py - one dbt process per project, across processes.

The UI already refuses to start a second dbt run while one is in flight, but that
guard lives in a Python dict and can only see the server's own jobs. Once runs
can also be triggered by Task Scheduler, there are two unrelated processes able
to invoke dbt against the same project directory.

That matters because dbt writes target/ - manifest.json, run_results.json,
partial_parse.msgpack - and does not write them atomically. A scheduled build
firing halfway through a manual one can leave the UI reading a truncated
manifest, or leave partial_parse in a state that makes the next parse wrong in
ways that are very hard to attribute.

Not an OS-level file lock, on purpose. If the holder is killed - and a dbt run is
exactly the kind of thing people kill - an OS lock can persist and silently
prevent every future run with no way for the user to see why. A PID and a
timestamp can be tested for liveness and aged out, and the lock file is readable
by a human trying to work out what is going on.

This module is deliberately dependency-free apart from config, so both runner.py
and schedules.py can import it without a cycle.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, Optional

from . import config

LOCK_PATH: pathlib.Path = config.RUNTIME_DIR / "dbt_run.lock"

# Long enough that a genuinely slow full refresh is not mistaken for a dead
# process, short enough that a killed run does not block schedules all day.
STALE_SECONDS = 4 * 3600


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            # If liveness cannot be established, assume alive: releasing a lock
            # that is genuinely held is worse than waiting for it to age out.
            return True
        return f'"{pid}"' in (proc.stdout or "") or f",{pid}," in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def read() -> Optional[Dict[str, Any]]:
    """The live holder, or None when the lock is free, stale or abandoned."""
    if not LOCK_PATH.is_file():
        return None
    try:
        held = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    try:
        started = float(held.get("started_at") or 0)
        pid = int(held.get("pid") or 0)
    except (TypeError, ValueError):
        return None

    if time.time() - started > STALE_SECONDS:
        return None
    if not _process_alive(pid):
        return None

    held["age"] = round(time.time() - started, 1)
    return held


def acquire(owner: str) -> bool:
    """Take the lock, or return False if someone live already holds it."""
    config.ensure_runtime_dir()
    if read() is not None:
        return False
    try:
        LOCK_PATH.write_text(json.dumps({
            "pid": os.getpid(),
            "owner": str(owner),
            "started_at": time.time(),
        }), encoding="utf-8")
    except OSError:
        # An unwritable runtime directory should not stop dbt running; the worst
        # case is the overlap this was protecting against.
        return True
    return True


def release() -> None:
    """Clear the lock, but only if this process is the one holding it."""
    try:
        if not LOCK_PATH.is_file():
            return
        held = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if int(held.get("pid") or 0) == os.getpid():
            LOCK_PATH.unlink()
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        # A corrupt lock is worse than no lock: it would age out eventually but
        # block everything until then.
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass


def describe() -> Dict[str, Any]:
    """For the UI, so a blocked run can say who is holding things up."""
    held = read()
    if held is None:
        return {"held": False}
    return {
        "held": True,
        "owner": held.get("owner"),
        "pid": held.get("pid"),
        "age": held.get("age"),
        "started_at": held.get("started_at"),
    }

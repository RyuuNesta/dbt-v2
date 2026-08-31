#!/usr/bin/env python
"""
ASG dbt Studio - launcher.

    python dbt_ui/serve.py                  start on http://localhost:8777
    python dbt_ui/serve.py --port 9000      pick a port
    python dbt_ui/serve.py --no-browser     do not open a browser
    python dbt_ui/serve.py --check          verify the environment and exit

Runs on the Python that has dbt installed. No extra packages are required:
everything used here ships with dbt-core and dbt-bigquery.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

# Allow `python dbt_ui/serve.py` from anywhere by putting the UI directory on
# the path, so `backend` resolves as a package.
UI_DIR = pathlib.Path(__file__).resolve().parent
if str(UI_DIR) not in sys.path:
    sys.path.insert(0, str(UI_DIR))

from backend import config, server  # noqa: E402


def check_environment() -> int:
    """Print a readiness report. Exit code is non-zero if something is fatal."""
    problems: list[str] = []
    notes: list[str] = []

    print(f"project dir      {config.PROJECT_DIR}")

    if config.DBT_PROJECT_PATH.exists():
        print(f"dbt_project.yml  found ({config.project_name()})")
    else:
        problems.append(f"dbt_project.yml is missing at {config.DBT_PROJECT_PATH}")

    if config.PROFILES_PATH.exists():
        print(f"profiles.yml     found (project-local)")
    elif (pathlib.Path.home() / ".dbt" / "profiles.yml").exists():
        notes.append("profiles.yml found in ~/.dbt, not in the project. A "
                     "project-local copy is easier to share with the team.")
    else:
        problems.append("No profiles.yml in the project or in ~/.dbt")

    try:
        import dbt.version
        print(f"dbt-core         {dbt.version.get_installed_version().to_version_string(skip_matcher=True)}")
    except Exception as exc:
        problems.append(f"dbt-core is not importable: {exc}")

    try:
        from google.cloud import bigquery  # noqa: F401
        print("bigquery client  available")
    except Exception as exc:
        problems.append(f"google-cloud-bigquery is not importable: {exc}")

    try:
        targets = config.list_targets()
        print(f"profile          {config.profile_name()}")
        for target in targets:
            marker = "*" if target.is_default else " "
            print(f"  {marker} {target.name:<6} {target.project}."
                  f"{target.dataset} @ {target.location}")
            for warning in target.warnings:
                notes.append(f"target '{target.name}': {warning}")
        if not targets:
            problems.append(
                f"Profile '{config.profile_name()}' has no outputs defined."
            )
    except Exception as exc:
        problems.append(f"Could not read profiles.yml: {exc}")

    if config.MANIFEST_PATH.exists():
        print("manifest         found")
    else:
        notes.append("target/manifest.json is missing. Run 'dbt parse', or "
                     "click Refresh manifest once the UI is open.")

    if not config.FRONTEND_DIR.is_dir():
        problems.append(f"Frontend assets are missing: {config.FRONTEND_DIR}")

    if notes:
        print("\nnotes")
        for note in notes:
            print(f"  - {note}")

    if problems:
        print("\nproblems")
        for problem in problems:
            print(f"  ! {problem}")
        return 1

    print("\nReady.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="dbt-studio",
        description="Interactive UI for a dbt Core project on BigQuery.",
    )
    parser.add_argument("--host", default=None,
                        help="Interface to bind. Defaults to 127.0.0.1.")
    parser.add_argument("--port", type=int, default=None,
                        help="Port to bind. Defaults to 8777, or the next free "
                             "port after it.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a browser on start.")
    parser.add_argument("--check", action="store_true",
                        help="Verify the environment and exit.")
    args = parser.parse_args()

    if args.check:
        return check_environment()

    try:
        server.serve(
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except SystemExit as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
Compile ad-hoc SQL the way dbt would.

This is what makes the workbench "query through dbt" rather than "query
BigQuery". An analyst writes:

    select * from {{ ref('silver_gl_entries') }} where company_code = 1000

and never has to know the physical dataset. Because the relation is resolved
from the manifest, switching target from dev to prod repoints every ref with no
edit to the SQL, and a typo in a model name is caught before a byte is scanned.

Resolution is deliberately manifest-driven rather than a dbt subprocess: it is
instant, and manifest.json is a documented, stable artifact.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment

from . import config, manifest as manifest_mod


class CompileError(RuntimeError):
    """Jinja or reference resolution failure."""

    def __init__(self, message: str, *, detail: str = "",
                 unknown_refs: Optional[List[str]] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.unknown_refs = unknown_refs or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "detail": self.detail,
            "unknown_refs": self.unknown_refs,
        }


# --------------------------------------------------------------------------
# jinja context
# --------------------------------------------------------------------------

class _RefResolver:
    """Implements ref() / source() against a loaded manifest."""

    def __init__(self, mf: "manifest_mod.Manifest"):
        self.manifest = mf
        self.refs = mf.ref_map()
        self.sources = mf.source_map()
        self.used_refs: List[str] = []
        self.used_sources: List[str] = []
        self.unknown: List[str] = []

    def ref(self, *args: str, **kwargs: Any) -> str:
        # dbt allows ref('model'), ref('package', 'model') and
        # ref(..., version=n). Only the model name affects resolution here.
        parts = [str(a) for a in args if a is not None]
        if not parts:
            raise CompileError("ref() was called with no arguments.")
        name = parts[-1]

        relation = self.refs.get(name)
        if not relation:
            self.unknown.append(f"ref('{name}')")
            close = _suggest(name, self.refs.keys())
            suffix = f" Did you mean {close}?" if close else ""
            raise CompileError(
                f"ref('{name}') does not resolve to a model or seed in this "
                f"project.{suffix}",
                unknown_refs=[name],
            )

        if name not in self.used_refs:
            self.used_refs.append(name)
        return relation

    def source(self, source_name: str, table_name: str) -> str:
        key = f"{source_name}.{table_name}"
        relation = self.sources.get(key)
        if not relation:
            self.unknown.append(f"source('{source_name}', '{table_name}')")
            close = _suggest(key, self.sources.keys())
            suffix = f" Did you mean {close}?" if close else ""
            if not self.sources:
                suffix = (
                    " This project does not declare any sources yet; add a "
                    "sources: block in a models/*.yml file first."
                )
            raise CompileError(
                f"source('{source_name}', '{table_name}') is not declared in "
                f"this project.{suffix}",
                unknown_refs=[key],
            )

        if key not in self.used_sources:
            self.used_sources.append(key)
        return relation


def _suggest(needle: str, haystack: Any) -> str:
    import difflib
    matches = difflib.get_close_matches(needle, list(haystack), n=3, cutoff=0.55)
    return ", ".join(f"'{m}'" for m in matches)


def _build_env() -> SandboxedEnvironment:
    """
    Sandboxed Jinja.

    The workbench renders text that arrives over HTTP, so the sandbox is a real
    boundary, not decoration: it blocks attribute access that could reach the
    Python runtime. StrictUndefined turns a typo into a clear error instead of
    silently rendering an empty string into the middle of a query.
    """
    env = SandboxedEnvironment(
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        autoescape=False,
    )
    env.filters["as_number"] = lambda v: v
    env.filters["as_bool"] = lambda v: bool(v)
    env.filters["as_text"] = lambda v: "" if v is None else str(v)
    return env


def _project_vars() -> Dict[str, Any]:
    return dict((config.read_dbt_project().get("vars") or {}))


def _target_context(target: Optional[str]) -> Dict[str, Any]:
    cfg = config.target_config(target)
    return {
        "name": cfg.get("_target_name"),
        "profile_name": cfg.get("_profile_name"),
        "type": cfg.get("type"),
        "project": cfg.get("project"),
        "database": cfg.get("project"),
        "dataset": cfg.get("dataset"),
        "schema": cfg.get("dataset"),
        "location": cfg.get("location"),
        "threads": cfg.get("threads"),
    }


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def compile_sql(
    raw_sql: str,
    target: Optional[str] = None,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Render Jinja in `raw_sql` and return the compiled SQL plus what it touched.

    Supported in the workbench: ref, source, var, target, this, plus the
    log/print no-ops so a model body pasted in as-is still renders.
    """
    mf = manifest_mod.load()
    resolver = _RefResolver(mf)
    project_vars = _project_vars()

    def _var(name: str, default: Any = None) -> Any:
        return project_vars.get(name, default)

    context: Dict[str, Any] = {
        "ref": resolver.ref,
        "source": resolver.source,
        "var": _var,
        "env_var": lambda name, default="": default,
        "target": _target_context(target),
        "this": None,
        "log": lambda *a, **k: "",
        "print": lambda *a, **k: "",
        "run_started_at": None,
        "invocation_id": "dbt-ui-workbench",
        "project_name": config.project_name(),
    }

    # Project macros are registered as callables that explain themselves rather
    # than being left undefined. Pasting a model body into the workbench is a
    # normal thing to do, and "asg_audit_columns is undefined" gives the reader
    # nowhere to go, whereas naming it as a dbt macro does.
    context.update(_macro_stubs(mf))

    if extra_context:
        context.update(extra_context)

    env = _build_env()
    try:
        template = env.from_string(raw_sql)
        compiled = template.render(**context)
    except CompileError:
        raise
    except Exception as exc:
        raise CompileError(
            _clean_jinja_error(exc),
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc

    return {
        "compiled_sql": compiled.strip(),
        "raw_sql": raw_sql,
        "refs": resolver.used_refs,
        "sources": resolver.used_sources,
        "relations": [resolver.refs[r] for r in resolver.used_refs]
        + [resolver.sources[s] for s in resolver.used_sources],
        "target": _target_context(target),
    }


def _macro_stubs(mf: "manifest_mod.Manifest") -> Dict[str, Any]:
    """
    One stub per macro defined in this project or its packages.

    Expanding real dbt macros needs dbt's own Jinja environment, adapter context
    and dispatch machinery. Rather than half-reimplement that and risk producing
    SQL that differs from what dbt would generate, the workbench refuses clearly
    and points at `dbt compile`, which renders it correctly.
    """
    stubs: Dict[str, Any] = {}

    for unique_id, macro in (mf.macros or {}).items():
        name = macro.get("name")
        if not name or name in stubs:
            continue
        package = macro.get("package_name") or ""

        def _stub(*args: Any, _name: str = name, _package: str = package, **kwargs: Any) -> str:
            raise CompileError(
                f"'{_name}' is a dbt macro (from package '{_package}'). The "
                f"workbench renders ref, source, var and target only, so it "
                f"cannot expand it.",
                detail=(
                    "Macros are expanded by dbt itself, using the adapter "
                    "context and dispatch rules. To see this SQL fully "
                    "rendered, put it in a model file and run Compile from the "
                    "Run Console, then read the result under "
                    "target/compiled/.\n\n"
                    "For ad-hoc exploration, replace the macro call with the "
                    "SQL it produces."
                ),
            )

        stubs[name] = _stub

    return stubs


def _clean_jinja_error(exc: Exception) -> str:
    text = str(exc)
    if "is undefined" in text:
        name = re.sub(r"^'|'.*$", "", text.split(" is undefined")[0]).strip("' ")
        return (
            f"'{name}' is not available in the workbench context. Available: "
            f"ref, source, var, target, this."
        )
    if isinstance(exc, SyntaxError) or "unexpected" in text.lower():
        return f"Jinja syntax error: {text}"
    return f"Could not compile the Jinja in this statement: {text}"


def wrap_with_limit(sql: str, limit: int) -> str:
    """
    Apply a row cap without breaking a statement that already has its own.

    Wrapping in a subquery is safer than appending LIMIT, which would be a
    syntax error after an ORDER BY inside a CTE or attach to the wrong query in
    a UNION.
    """
    stripped = sql.strip().rstrip(";").strip()
    if not stripped:
        return stripped
    return f"select * from (\n{stripped}\n) as _dbt_ui_preview\nlimit {int(limit)}"


def _strip_sql_noise(sql: str) -> str:
    """Drop comments and a leading paren so the first keyword is visible."""
    text = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    return text.strip().lstrip("(").strip()


# The DDL the workbench is allowed to run, so a view or table can be created
# here the way it can in the BigQuery console. Everything else that changes or
# destroys existing data (DROP, DELETE, INSERT, MERGE, TRUNCATE, ALTER,
# CREATE FUNCTION/PROCEDURE, ...) stays blocked and belongs in a reviewed model.
_CREATE_VIEW_RE = re.compile(
    r"^create\s+(?:or\s+replace\s+)?(?:materialized\s+)?view\b",
    re.IGNORECASE,
)
# CREATE [OR REPLACE] TABLE [IF NOT EXISTS] ...   Also CTAS.
# Deliberately not matched: CREATE TABLE FUNCTION, which is a routine, not a
# table, so the negative lookahead keeps it on the blocked path.
_CREATE_TABLE_RE = re.compile(
    r"^create\s+(?:or\s+replace\s+)?(?:temp(?:orary)?\s+)?table\s+"
    r"(?!function\b)(?:if\s+not\s+exists\s+)?",
    re.IGNORECASE,
)


def is_view_ddl(sql: str) -> bool:
    """True when the statement is a CREATE ... VIEW."""
    return bool(_CREATE_VIEW_RE.match(_strip_sql_noise(sql)))


def is_table_ddl(sql: str) -> bool:
    """True when the statement is a CREATE ... TABLE (including CTAS)."""
    return bool(_CREATE_TABLE_RE.match(_strip_sql_noise(sql)))


def is_create_ddl(sql: str) -> bool:
    """True for the CREATE VIEW / CREATE TABLE statements the workbench runs."""
    return is_view_ddl(sql) or is_table_ddl(sql)


def is_read_only(sql: str) -> Tuple[bool, str]:
    """
    Reject anything that is not a read, with two deliberate exceptions:
    CREATE VIEW and CREATE TABLE are permitted so they can be defined here the
    way they are in the BigQuery console.

    Everything else that changes or destroys existing data (INSERT, UPDATE,
    DELETE, MERGE, DROP, TRUNCATE, ALTER, CREATE FUNCTION/PROCEDURE, ...) stays
    blocked: that work belongs in a model file that goes through review.
    Comments and CTEs are stripped before the check so a legitimate query
    starting with a comment is not misread.
    """
    text = _strip_sql_noise(sql)

    if not text:
        return False, "The statement is empty."

    # The sanctioned DDL exceptions. Checked before the generic 'create' block.
    if is_create_ddl(text):
        return True, ""

    first = text.split(None, 1)[0].lower().strip(";")

    allowed = {"select", "with", "table"}
    if first in allowed:
        return True, ""

    blocked = {
        "insert": "INSERT", "update": "UPDATE", "delete": "DELETE",
        "merge": "MERGE", "drop": "DROP", "create": "CREATE",
        "alter": "ALTER", "truncate": "TRUNCATE", "grant": "GRANT",
        "revoke": "REVOKE", "call": "CALL", "execute": "EXECUTE",
        "begin": "BEGIN", "declare": "DECLARE", "export": "EXPORT",
        "load": "LOAD", "replace": "REPLACE",
    }
    if first in blocked:
        detail = (
            " Only CREATE VIEW and CREATE TABLE are allowed here; other CREATE "
            "forms (functions, procedures) belong in the project."
            if first == "create" else ""
        )
        return False, (
            f"{blocked[first]} statements are blocked in the workbench. "
            f"Changes to data or schema belong in a dbt model or seed so they "
            f"go through review and land in the DAG.{detail}"
        )

    return False, (
        f"Only read statements are allowed here. '{first}' is not a SELECT, "
        f"WITH or TABLE query."
    )

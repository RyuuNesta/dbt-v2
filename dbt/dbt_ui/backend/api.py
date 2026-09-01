"""
JSON API.

A small explicit router rather than a framework. Handlers take a parsed request
and return (status, payload); the HTTP layer knows nothing about dbt and this
layer knows nothing about sockets.
"""

from __future__ import annotations

import csv
import io
import os
import pathlib
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (
    ai_docs,
    auth,
    codegen,
    config,
    erd as erd_mod,
    jinja_sql,
    manifest as manifest_mod,
    modelgen,
    profiling,
    recommend,
    runlock,
    runner,
    schedules,
    typing_map,
    warehouse,
    yamlpatch,
)

Handler = Callable[["Request"], Tuple[int, Any]]


class ApiError(Exception):
    """Handler-raised error carrying an HTTP status."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra

    def payload(self) -> Dict[str, Any]:
        return {"error": self.message, **self.extra}


class Request:
    def __init__(self, method: str, path: str,
                 query: Dict[str, List[str]], body: Dict[str, Any],
                 headers: Optional[Dict[str, str]] = None):
        self.method = method
        self.path = path
        self.query = query
        self.body = body if isinstance(body, dict) else {}
        self.headers = headers or {}
        # Populated by the auth gate in handle(). None only on the routes that
        # are reachable without signing in.
        self.user: Optional[Dict[str, Any]] = None
        # Response headers a handler wants set, e.g. Set-Cookie on login.
        self.response_headers: Dict[str, str] = {}

    @property
    def session_token(self) -> Optional[str]:
        cookies = auth.parse_cookies(self.headers.get("cookie"))
        return cookies.get(auth.COOKIE_NAME)

    @property
    def permissions(self) -> Dict[str, Any]:
        return config.role_permissions((self.user or {}).get("role"))

    # ------------------------------------------------------------------

    def q(self, key: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(key)
        return values[0] if values else default

    def q_int(self, key: str, default: int) -> int:
        try:
            return int(self.q(key) or default)
        except (TypeError, ValueError):
            return default

    def q_bool(self, key: str, default: bool = False) -> bool:
        raw = (self.q(key) or "").lower()
        if raw in ("1", "true", "yes"):
            return True
        if raw in ("0", "false", "no"):
            return False
        return default

    def need(self, key: str) -> Any:
        if key not in self.body or self.body[key] in (None, ""):
            raise ApiError(f"'{key}' is required in the request body.")
        return self.body[key]

    def opt(self, key: str, default: Any = None) -> Any:
        value = self.body.get(key, default)
        return default if value in (None, "") else value

    @property
    def target(self) -> str:
        return str(
            self.opt("target") or self.q("target") or config.default_target_name()
        )


# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

class Router:
    def __init__(self) -> None:
        self._routes: List[Tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = re.compile("^" + re.sub(r"<(\w+)>", r"(?P<\1>[^/]+)", pattern) + "$")
        self._routes.append((method.upper(), regex, handler))

    def get(self, pattern: str) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            self.add("GET", pattern, handler)
            return handler
        return decorate

    def post(self, pattern: str) -> Callable[[Handler], Handler]:
        def decorate(handler: Handler) -> Handler:
            self.add("POST", pattern, handler)
            return handler
        return decorate

    def dispatch(self, request: Request) -> Tuple[int, Any]:
        allowed_other_method = False
        for method, regex, handler in self._routes:
            match = regex.match(request.path)
            if not match:
                continue
            if method != request.method:
                allowed_other_method = True
                continue
            for key, value in (match.groupdict() or {}).items():
                request.body.setdefault(key, value)
            return handler(request)

        if allowed_other_method:
            return 405, {"error": f"{request.method} is not allowed on {request.path}."}
        return 404, {"error": f"No API route matches {request.path}."}


ROUTER = Router()


# --------------------------------------------------------------------------
# project / bootstrap
# --------------------------------------------------------------------------

@ROUTER.get("/api/health")
def _health(request: Request) -> Tuple[int, Any]:
    return 200, {"ok": True, "time": time.time()}


@ROUTER.get("/api/bootstrap")
def _bootstrap(request: Request) -> Tuple[int, Any]:
    """Everything the app needs on first paint, in one round trip."""
    mf = manifest_mod.try_load()

    payload: Dict[str, Any] = {
        "project": {
            "name": config.project_name(),
            "profile": config.profile_name(),
            "dir": str(config.PROJECT_DIR),
            "profiles_dir": config.profiles_dir(),
            "has_manifest": mf is not None,
        },
        "targets": [t.to_dict() for t in config.list_targets()],
        "default_target": config.default_target_name(),
        "layers": [
            {
                "key": layer.key,
                "label": layer.label,
                "blurb": layer.blurb,
                "order": layer.order,
                "materialization": layer.default_materialization,
            }
            for layer in config.LAYERS
        ],
        "settings": {
            "preview_row_limit": config.SETTINGS.preview_row_limit,
            "max_preview_row_limit": config.SETTINGS.max_preview_row_limit,
            "max_bytes_billed": config.SETTINGS.max_bytes_billed,
            "profile_sample_rows": config.SETTINGS.profile_sample_rows,
        },
        "commands": [
            {"key": key, "label": meta["label"], "writes": meta["writes"]}
            for key, meta in runner.ALLOWED_COMMANDS.items()
        ],
        "stats": mf.stats() if mf else None,
        "manifest_error": None if mf else (
            "No target/manifest.json yet. Click 'Refresh manifest' to run "
            "dbt parse."
        ),
        "last_run": manifest_mod.last_run_results(),
        "active_job": (runner.REGISTRY.active().summary()
                       if runner.REGISTRY.active() else None),
        "docs_available": (config.TARGET_DIR / "static_index.html").exists(),
        "ai": ai_docs.status(),
        "scope": config.scope_description(),
        # Who is signed in and what they may do. The frontend uses this to shape
        # the UI; the backend enforces the same matrix independently.
        "user": config.current_request_user(),
        "permissions": request.permissions,
        "roles": config.role_catalogue(),
        "manifest_target": mf.built_with_target() if mf else None,
    }
    return 200, payload


@ROUTER.get("/api/connection")
def _connection(request: Request) -> Tuple[int, Any]:
    return 200, warehouse.connection_check(request.q("target"))


@ROUTER.get("/api/stats")
def _stats(request: Request) -> Tuple[int, Any]:
    mf = _manifest()
    return 200, {"stats": mf.stats(), "last_run": manifest_mod.last_run_results()}


def _manifest() -> "manifest_mod.Manifest":
    try:
        return manifest_mod.load()
    except manifest_mod.ManifestNotFound as exc:
        raise ApiError(str(exc), status=409, needs_parse=True) from exc


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------

@ROUTER.get("/api/models")
def _models(request: Request) -> Tuple[int, Any]:
    mf = _manifest()
    nodes = mf.buildable_nodes()
    target = request.q("target") or config.default_target_name()

    # Annotate rather than hide. A model the UI cannot read still exists in the
    # DAG, and silently omitting it would make the lineage look wrong.
    for node in nodes:
        dataset = _dataset_of(node.get("relation_name") or "")
        node["dataset"] = dataset
        node["in_scope"] = config.dataset_allowed(dataset, target)

    if request.q_bool("in_scope_only", False):
        nodes = [n for n in nodes if n["in_scope"]]

    layer = request.q("layer")
    if layer:
        nodes = [n for n in nodes if n["layer"] == layer]

    search = (request.q("search") or "").strip().lower()
    if search:
        nodes = [
            n for n in nodes
            if search in n["name"].lower()
            or search in (n["description"] or "").lower()
        ]

    return 200, {
        "models": nodes,
        "count": len(nodes),
        "in_scope_count": sum(1 for n in nodes if n["in_scope"]),
        "scope": config.scope_description(target),
        "target_mismatch": mf.target_mismatch(target),
        "manifest_target": mf.built_with_target(),
    }


@ROUTER.get("/api/models/<name>")
def _model_detail(request: Request) -> Tuple[int, Any]:
    mf = _manifest()
    name = str(request.body.get("name"))
    detail = mf.node_detail(name)
    if detail is None:
        raise ApiError(f"No model or seed named '{name}' in this project.", 404)
    return 200, {"model": detail}


@ROUTER.get("/api/docs/site")
def _docs_site(request: Request) -> Tuple[int, Any]:
    """
    Status of the dbt-generated documentation site.

    The site is a single self-contained file that `dbt docs generate --static`
    writes to target/static_index.html and the server exposes at /dbt-docs. This
    reports whether it exists and how fresh it is, so the UI can say "generated
    3 hours ago" and offer to regenerate rather than guessing.
    """
    static_index = config.TARGET_DIR / "static_index.html"
    catalog = config.CATALOG_PATH
    exists = static_index.is_file()
    return 200, {
        "available": exists,
        "url": "/dbt-docs",
        "generated_at": (static_index.stat().st_mtime if exists else None),
        "size_bytes": (static_index.stat().st_size if exists else 0),
        "has_catalog": catalog.is_file(),
        "catalog_at": (catalog.stat().st_mtime if catalog.is_file() else None),
        # dbt docs generate is a dbt command, so running it needs the same
        # permission as any other run. The UI uses this to decide whether to
        # show the button enabled.
        "can_generate": request.permissions.get("can_run_dbt", False),
    }


@ROUTER.get("/api/sources")
def _sources(request: Request) -> Tuple[int, Any]:
    mf = _manifest()
    return 200, {"sources": mf.source_summaries()}


@ROUTER.get("/api/graph")
def _graph(request: Request) -> Tuple[int, Any]:
    return 200, _manifest().graph()


def _erd_options(request: Request) -> Dict[str, Any]:
    """Shared query parsing, so the diagram and its exports cannot disagree."""
    tables = request.q("tables")
    datasets = request.q("datasets")
    return {
        "target": request.q("target"),
        # Gold is shown by default, dimmed, exactly like the lineage graph and
        # the Pipeline board. in_scope_only is the export-time opt-in for a
        # deliberately bronze/silver-only diagram.
        "in_scope_only": request.q_bool("in_scope_only", False),
        "include_staging": request.q_bool("include_staging", True),
        "include_sources": request.q_bool("include_sources", True),
        "only_tables": [t for t in (tables or "").split(",") if t.strip()],
        "datasets": [d for d in (datasets or "").split(",") if d.strip()],
        # Both cost a query, so neither happens unless asked for.
        "with_counts": request.q_bool("counts", False),
        "with_constraints": request.q_bool("constraints", False),
    }


@ROUTER.get("/api/erd")
def _erd(request: Request) -> Tuple[int, Any]:
    """
    Entity relationship model derived from the manifest.

    Manifest-only by default: no warehouse call, so this works with expired
    credentials and costs nothing.
    """
    options = _erd_options(request)
    target = options.pop("target")
    return 200, erd_mod.build(_manifest(), target, **options)


@ROUTER.get("/api/erd/export")
def _erd_export(request: Request) -> Tuple[int, Any]:
    """
    Text exports of the diagram: Mermaid or DBML.

    Rebuilt from the same erd.build() the diagram uses rather than accepting a
    payload from the browser, so an export can never describe a diagram the
    project does not actually have. SVG, PNG and PDF are produced in the browser
    from the live DOM - they have no server-side equivalent.
    """
    fmt = (request.q("format") or "mermaid").lower()
    if fmt not in ("mermaid", "dbml"):
        raise ApiError(
            f"Unsupported export format '{fmt}'. Use 'mermaid' or 'dbml'.",
            400,
        )

    options = _erd_options(request)
    target = options.pop("target")
    model = erd_mod.build(_manifest(), target, **options)

    if fmt == "mermaid":
        content = erd_mod.to_mermaid(model, keys_only=request.q_bool("keys_only", False))
        filename = "erd.mmd"
    else:
        content = erd_mod.to_dbml(model)
        filename = "erd.dbml"

    return 200, {
        "format": fmt,
        "filename": filename,
        "content": content,
        "table_count": model["stats"]["table_count"],
        "relationship_count": model["stats"]["relationship_count"],
    }


@ROUTER.get("/api/refs")
def _refs(request: Request) -> Tuple[int, Any]:
    """Autocomplete feed for the SQL editor."""
    mf = _manifest()
    return 200, {
        "refs": [
            {"name": name, "relation": relation}
            for name, relation in sorted(mf.ref_map().items())
        ],
        "sources": [
            {"key": key, "relation": relation}
            for key, relation in sorted(mf.source_map().items())
        ],
    }


@ROUTER.get("/api/autocomplete/columns")
def _autocomplete_columns(request: Request) -> Tuple[int, Any]:
    """
    Columns for one model, from the manifest.

    Preferred over INFORMATION_SCHEMA when the relation is dbt-managed, because
    the manifest also carries the human description and costs nothing to read.
    """
    name = request.q("model")
    if not name:
        raise ApiError("'model' query parameter is required.")

    mf = _manifest()
    detail = mf.node_detail(str(name))
    if detail is None:
        raise ApiError(f"No model or seed named '{name}'.", 404)

    return 200, {
        "model": detail["name"],
        "source": "manifest",
        "relation": detail.get("relation_name"),
        "columns": [
            {
                "name": column["name"],
                "data_type": (column.get("data_type") or "").upper() or None,
                "data_type_yaml": (column.get("data_type") or "").lower() or None,
                "description": column.get("description") or "",
            }
            for column in detail.get("columns") or []
        ],
    }


@ROUTER.get("/api/autocomplete/schema")
def _autocomplete_schema(request: Request) -> Tuple[int, Any]:
    """
    Every table and column in a permitted dataset, via INFORMATION_SCHEMA.

    This is the fallback for relations dbt does not manage. Cached server-side,
    so repeated keystrokes do not re-query BigQuery.
    """
    dataset = request.q("dataset")
    if not dataset:
        raise ApiError("'dataset' query parameter is required.")

    try:
        return 200, warehouse.dataset_schema(
            dataset,
            target=request.q("target"),
            refresh=request.q_bool("refresh", False),
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc


@ROUTER.get("/api/autocomplete/catalog")
def _autocomplete_catalog(request: Request) -> Tuple[int, Any]:
    """
    Everything the editor needs to offer suggestions without a warehouse call.

    Model and source names plus their documented columns come straight from the
    manifest, which means the common case - completing a column on a model you
    just referenced - never touches BigQuery at all.
    """
    mf = _manifest()
    target = request.q("target") or config.default_target_name()
    allowed = set(config.allowed_datasets(target))

    models = []
    for node in mf.buildable_nodes():
        dataset = _dataset_of(node.get("relation_name") or "")
        detail_columns = []
        unique_id = node["unique_id"]
        raw = mf.nodes.get(unique_id) or {}
        for column_name, column in (raw.get("columns") or {}).items():
            detail_columns.append({
                "name": column_name,
                "data_type": (column.get("data_type") or "").lower() or None,
            })

        models.append({
            "name": node["name"],
            "layer": node["layer"],
            "relation": node.get("relation_name"),
            "dataset": dataset,
            "in_scope": config.dataset_allowed(dataset, target),
            "columns": detail_columns,
        })

    return 200, {
        "models": models,
        "sources": [
            {"key": key, "relation": relation}
            for key, relation in sorted(mf.source_map().items())
        ],
        "macros": sorted({
            macro.get("name")
            for macro in (mf.macros or {}).values()
            if macro.get("package_name") == mf.metadata.get("project_name")
            and macro.get("name")
            and not str(macro.get("name")).startswith("test_")
        }),
        "datasets": sorted(allowed),
        "target": target,
    }


# --------------------------------------------------------------------------
# workbench: compile / validate / run
# --------------------------------------------------------------------------

def _compile_or_raise(sql: str, target: str) -> Dict[str, Any]:
    try:
        return jinja_sql.compile_sql(sql, target=target)
    except jinja_sql.CompileError as exc:
        raise ApiError(exc.message, status=400, **{
            "detail": exc.detail,
            "unknown_refs": exc.unknown_refs,
            "stage": "compile",
        }) from exc
    except manifest_mod.ManifestNotFound as exc:
        raise ApiError(str(exc), status=409, needs_parse=True) from exc


@ROUTER.post("/api/query/compile")
def _query_compile(request: Request) -> Tuple[int, Any]:
    sql = str(request.need("sql"))
    payload = _compile_or_raise(sql, request.target)
    mf = manifest_mod.try_load()
    if mf:
        payload["target_mismatch"] = mf.target_mismatch(request.target)
    return 200, payload


@ROUTER.post("/api/query/validate")
def _query_validate(request: Request) -> Tuple[int, Any]:
    """
    Compile, then dry-run.

    Returns the exact output columns and types plus the bytes the query would
    scan, having executed nothing and billed nothing.
    """
    sql = str(request.need("sql"))
    compiled = _compile_or_raise(sql, request.target)

    ok, reason = jinja_sql.is_read_only(compiled["compiled_sql"])
    if not ok:
        raise ApiError(reason, status=400, stage="policy")

    try:
        result = warehouse.dry_run(compiled["compiled_sql"], target=request.target)
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc, sql=compiled["compiled_sql"]) from exc

    return 200, {
        "compiled": compiled,
        "result": result.to_dict(),
        "yaml": codegen.columns_yaml_fragment(result.columns),
    }


@ROUTER.post("/api/query/run")
def _query_run(request: Request) -> Tuple[int, Any]:
    sql = str(request.need("sql"))
    limit = int(request.opt("limit", config.SETTINGS.preview_row_limit))
    compiled = _compile_or_raise(sql, request.target)

    ok, reason = jinja_sql.is_read_only(compiled["compiled_sql"])
    if not ok:
        raise ApiError(reason, status=400, stage="policy")

    # CREATE VIEW / CREATE TABLE return no rows, so wrapping them in a
    # `select * from (...) limit N` would be a syntax error. Run the DDL
    # verbatim and skip the row cap.
    is_ddl = jinja_sql.is_create_ddl(compiled["compiled_sql"])
    is_table = jinja_sql.is_table_ddl(compiled["compiled_sql"])

    # Creating a view or a table is a write, not a read. Reading is open to every
    # role; this is not. Checked after compilation so the message names the
    # statement type rather than refusing anything that merely looks like DDL.
    if is_ddl:
        _require("can_write_files",
                 "create tables or views (this statement is DDL, not a query)")
    executed = (
        compiled["compiled_sql"]
        if is_ddl
        else jinja_sql.wrap_with_limit(compiled["compiled_sql"], limit)
    )
    try:
        result = warehouse.execute(
            executed, target=request.target, limit=limit, apply_limit=not is_ddl
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc, sql=executed) from exc

    return 200, {
        "compiled": compiled,
        "result": result.to_dict(),
        "yaml": codegen.columns_yaml_fragment(result.columns),
        "ddl": is_ddl,
        "ddl_kind": "table" if is_table else ("view" if is_ddl else None),
    }


# --------------------------------------------------------------------------
# schema + documentation generation
# --------------------------------------------------------------------------

def _warehouse_failure(exc: warehouse.WarehouseError, *, sql: str = "",
                       stage: str = "warehouse") -> ApiError:
    """
    Convert a warehouse failure into the right ApiError.

    ScopeError must be distinguished from its parent WarehouseError: a policy
    refusal is a 403 and carries the allowlist so the UI can explain the
    boundary, whereas a query failure is a 400. Because ScopeError subclasses
    WarehouseError, every `except WarehouseError` would otherwise silently
    downgrade it.
    """
    if isinstance(exc, warehouse.ScopeError):
        return ApiError(
            exc.message,
            status=403,
            stage="scope",
            detail=exc.detail,
            sql=exc.sql or sql,
            scope_violation=True,
            offending_datasets=exc.offending,
            allowed_datasets=exc.allowed,
        )
    return ApiError(
        exc.message, status=400, stage=stage, detail=exc.detail,
        sql=exc.sql or sql,
    )


def _dataset_of(relation: str) -> str:
    parts = [p.strip().strip("`") for p in str(relation).split(".")]
    return parts[1] if len(parts) == 3 else (parts[0] if len(parts) == 2 else "")


def _guard_relation(relation: str, target: str, label: str = "") -> None:
    """Refuse a relation outside the allowlist, with a 403."""
    dataset = _dataset_of(relation)
    if config.dataset_allowed(dataset, target):
        return

    allowed = config.allowed_datasets(target)
    subject = label or relation
    raise ApiError(
        f"'{subject}' lives in dataset '{dataset}', which is outside the "
        f"permitted scope.",
        status=403,
        scope_violation=True,
        offending_datasets=[dataset],
        allowed_datasets=allowed,
        detail=(
            f"This dbt Studio instance may only read: {', '.join(allowed)}.\n\n"
            f"The gold layer, seeds and every other dataset in the project are "
            f"refused before any query is sent."
        ),
    )


def _resolve_relation(request: Request) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Accept either a model name or a raw relation, and enforce dataset scope.

    Scope is checked here rather than only in warehouse.py so the refusal names
    the model the user clicked, which is more useful than naming a dataset they
    never typed.
    """
    model_name = request.opt("model")
    relation = request.opt("relation")

    if model_name:
        mf = _manifest()
        node = mf.node_detail(str(model_name))
        if node is None:
            raise ApiError(f"No model or seed named '{model_name}'.", 404)
        if not node.get("relation_name"):
            raise ApiError(
                f"'{model_name}' has no relation yet. Build it first so the "
                f"warehouse can report its real column types."
            )
        resolved = str(node["relation_name"])
        _guard_relation(resolved, request.target, label=str(model_name))
        return resolved, node

    if relation:
        _guard_relation(str(relation), request.target)
        return str(relation), None

    raise ApiError("Provide either 'model' or 'relation'.")


@ROUTER.post("/api/schema/generate")
def _schema_generate(request: Request) -> Tuple[int, Any]:
    """
    Produce name + data_type (+ description, + tests) for a relation or query.

    Three input modes:
      model / relation -> read the table definition (authoritative types)
      sql              -> dry-run the statement and use its output schema
    """
    sql = request.opt("sql")
    include_profile = bool(request.opt("profile", False))
    include_tests = bool(request.opt("include_tests", True))
    include_descriptions = bool(request.opt("include_descriptions", True))

    profiles: Dict[str, Dict[str, Any]] = {}
    existing: Dict[str, str] = {}
    described: Optional[Dict[str, Any]] = None
    node: Optional[Dict[str, Any]] = None

    if sql:
        compiled = _compile_or_raise(str(sql), request.target)
        ok, reason = jinja_sql.is_read_only(compiled["compiled_sql"])
        if not ok:
            raise ApiError(reason, status=400, stage="policy")
        try:
            result = warehouse.dry_run(compiled["compiled_sql"],
                                       target=request.target)
        except warehouse.WarehouseError as exc:
            raise _warehouse_failure(exc) from exc
        columns = result.columns
        name = str(request.opt("name", "my_new_model"))
        description = str(request.opt("description", ""))
        materialized = str(request.opt("materialized", ""))
        resource_type = "model"
        source_label = "dry run of the supplied SQL"
    else:
        relation, node = _resolve_relation(request)
        try:
            described = warehouse.describe_relation(relation, target=request.target)
        except warehouse.WarehouseError as exc:
            raise _warehouse_failure(exc) from exc
        columns = described["columns"]
        name = str(request.opt("name") or (node or {}).get("name")
                   or described["table"])
        description = str(request.opt("description")
                          or (node or {}).get("description") or "")
        materialized = str(request.opt("materialized")
                           or (node or {}).get("materialized") or "")
        resource_type = str((node or {}).get("resource_type") or "model")
        source_label = f"table definition of {relation}"

        for col in (node or {}).get("columns", []) or []:
            if col.get("description"):
                existing[col["name"]] = col["description"]

        if include_profile:
            profile = profiling.profile_relation(
                relation, target=request.target,
                sample_rows=config.SETTINGS.profile_sample_rows,
            )
            for col in profile["columns"]:
                profiles[col["name"]] = col
            _attach_top_values(profiles, relation, request.target)

    # ---- engine: pattern rules, or Gemini ------------------------------
    engine = str(request.opt("engine", "pattern")).lower()
    ai_result: Optional[Dict[str, Any]] = None
    ai_descriptions: Dict[str, str] = {}

    if engine == "ai" and include_descriptions:
        flat_columns = typing_map.flatten_columns(columns)
        try:
            ai_result = ai_docs.describe_columns(
                table_name=name,
                columns=flat_columns,
                profiles=profiles,
                model=str(request.opt("ai_model", ai_docs.DEFAULT_MODEL)),
                layer=str((node or {}).get("layer") or ""),
                row_count=(described or {}).get("row_count"),
                existing_description=description,
                upstream=[
                    parent.get("name")
                    for parent in ((node or {}).get("parents") or [])
                    if parent.get("name")
                ],
                send_sample_values=bool(request.opt("send_sample_values", False)),
            )
        except ai_docs.AiError as exc:
            payload = exc.to_dict()
            payload["engine"] = "ai"
            raise ApiError(exc.message, status=502, **{
                k: v for k, v in payload.items() if k != "error"
            }) from exc

        ai_descriptions = ai_result["descriptions"]
        if ai_result.get("table_description") and not description:
            description = ai_result["table_description"]

    generated = codegen.schema_yaml(
        name=name,
        columns=columns,
        resource_type=resource_type,
        description=description,
        profiles=profiles,
        existing_descriptions=existing,
        include_tests=include_tests,
        include_descriptions=include_descriptions,
        materialized=materialized,
        ai_descriptions=ai_descriptions,
    )

    return 200, {
        "name": name,
        "source": source_label,
        "engine": engine,
        "ai": (
            {
                key: value for key, value in ai_result.items()
                if key != "descriptions"
            }
            if ai_result else None
        ),
        "columns": _blank_if_undocumented([
            _column_payload(c, profiles, existing, ai_descriptions)
            for c in typing_map.flatten_columns(columns)
        ], include_descriptions),
        "yaml": generated["yaml"],
        "fragment": codegen.columns_yaml_fragment(columns),
        "markdown": codegen.markdown_table(columns, profiles),
        "stats": {
            "column_count": generated["column_count"],
            "documented": generated["documented"],
            "needs_review": generated["needs_review"],
        },
        "table": described,
        "suggested_path": _suggested_yaml_path(node, name),
    }


@ROUTER.post("/api/schema/rebuild")
def _schema_rebuild(request: Request) -> Tuple[int, Any]:
    """
    Re-render the schema YAML from columns already produced by /generate, with
    the descriptions the user edited in the proposal.

    This never touches the warehouse: the caller already has the column shapes
    and profiles from the generate step, so editing a description and seeing the
    YAML update is instant. Edited descriptions are passed as `ai_descriptions`
    because those take top precedence in codegen.schema_yaml, which is exactly
    the "the human overrode the draft" semantics we want.
    """
    name = str(request.need("name"))
    columns = request.need("columns")
    if not isinstance(columns, list):
        raise ApiError("'columns' must be a list of column objects.")

    descriptions = request.opt("descriptions", {}) or {}
    if not isinstance(descriptions, dict):
        raise ApiError("'descriptions' must be a name -> text mapping.")

    profiles = request.opt("profiles", {}) or {}
    resource_type = str(request.opt("resource_type", "model"))
    description = str(request.opt("description", ""))
    materialized = str(request.opt("materialized", ""))
    include_tests = bool(request.opt("include_tests", True))
    include_descriptions = bool(request.opt("include_descriptions", True))

    generated = codegen.schema_yaml(
        name=name,
        columns=columns,
        resource_type=resource_type,
        description=description,
        profiles=profiles,
        include_tests=include_tests,
        include_descriptions=include_descriptions,
        materialized=materialized,
        # Edited text wins over any drafted description.
        ai_descriptions={k: v for k, v in descriptions.items() if str(v).strip()},
    )

    return 200, {
        "name": name,
        "yaml": generated["yaml"],
        "markdown": codegen.markdown_table(columns, profiles),
        "stats": {
            "column_count": generated["column_count"],
            "documented": generated["documented"],
            "needs_review": generated["needs_review"],
        },
    }


@ROUTER.post("/api/schema/source")
def _schema_source(request: Request) -> Tuple[int, Any]:
    """
    Declare a table dbt does not build as a dbt source.

    Reads the physical table's schema from BigQuery (the free get_table
    metadata call), drafts descriptions the same way the model generator does
    (pattern rules by default, Gemini when engine=ai), and returns a dbt
    `sources:` block. Writing that block and re-parsing is what makes dbt aware
    of the table: it then appears in dbt docs, gets lineage, and can be
    source()-referenced.

    Input is a fully-qualified relation, e.g. bronze_dbt.bronze_workspace or
    project.bronze_dbt.bronze_workspace. The dataset must be in scope.
    """
    raw = str(request.need("relation")).strip().replace("`", "")
    include_profile = bool(request.opt("profile", False))
    include_tests = bool(request.opt("include_tests", True))
    include_descriptions = bool(request.opt("include_descriptions", True))

    # Normalise to project.dataset.table. A two-part dataset.table gets the
    # target's project prepended so describe_relation can resolve it.
    parts = [p for p in raw.split(".") if p]
    cfg = config.target_config(request.target)
    project = str(cfg.get("project") or "")
    if len(parts) == 2:
        dataset, table = parts
    elif len(parts) == 3:
        project, dataset, table = parts
    else:
        raise ApiError(
            "Give the table as dataset.table (or project.dataset.table), for "
            "example bronze_dbt.bronze_workspace_analytics_combined.",
            status=400,
        )

    relation = f"{project}.{dataset}.{table}"
    _guard_relation(relation, request.target, label=relation)

    try:
        described = warehouse.describe_relation(relation, target=request.target)
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc

    columns = described["columns"]
    # A readable default source name: the dataset it lives in.
    source_name = str(request.opt("source_name") or dataset)
    description = str(request.opt("description") or "")

    profiles: Dict[str, Dict[str, Any]] = {}
    if include_profile:
        profile = profiling.profile_relation(
            relation, target=request.target,
            sample_rows=config.SETTINGS.profile_sample_rows,
        )
        for col in profile["columns"]:
            profiles[col["name"]] = col
        _attach_top_values(profiles, relation, request.target)

    # ---- descriptions: pattern rules, or Gemini --------------------------
    engine = str(request.opt("engine", "pattern")).lower()
    ai_result: Optional[Dict[str, Any]] = None
    ai_descriptions: Dict[str, str] = {}

    if engine == "ai" and include_descriptions:
        flat_columns = typing_map.flatten_columns(columns)
        try:
            ai_result = ai_docs.describe_columns(
                table_name=table,
                columns=flat_columns,
                profiles=profiles,
                model=str(request.opt("ai_model", ai_docs.DEFAULT_MODEL)),
                layer="",
                row_count=described.get("row_count"),
                existing_description=description,
                upstream=[],
                send_sample_values=bool(request.opt("send_sample_values", False)),
            )
        except ai_docs.AiError as exc:
            payload = exc.to_dict()
            payload["engine"] = "ai"
            raise ApiError(exc.message, status=502, **{
                k: v for k, v in payload.items() if k != "error"
            }) from exc
        ai_descriptions = ai_result["descriptions"]
        if ai_result.get("table_description") and not description:
            description = ai_result["table_description"]

    generated = codegen.source_yaml(
        source_name=source_name,
        database=project,
        schema=dataset,
        table=table,
        columns=columns,
        table_description=description,
        profiles=profiles,
        include_tests=include_tests,
        include_descriptions=include_descriptions,
        ai_descriptions=ai_descriptions,
    )

    return 200, {
        "name": table,
        "source_name": source_name,
        "database": project,
        "schema": dataset,
        "relation": relation,
        "reference": generated["reference"],
        "engine": engine,
        "ai": (
            {k: v for k, v in ai_result.items() if k != "descriptions"}
            if ai_result else None
        ),
        "columns": _blank_if_undocumented([
            _column_payload(c, profiles, {}, ai_descriptions)
            for c in typing_map.flatten_columns(columns)
        ], include_descriptions),
        "yaml": generated["yaml"],
        "markdown": codegen.markdown_table(columns, profiles),
        "stats": {
            "column_count": generated["column_count"],
            "documented": generated["documented"],
            "needs_review": generated["needs_review"],
        },
        # Where the block should be written. A shared sources file per project.
        "suggested_path": "models/_sources.yml",
        "table": described,
    }


@ROUTER.post("/api/schema/source/rebuild")
def _schema_source_rebuild(request: Request) -> Tuple[int, Any]:
    """
    Re-render the source YAML from edited descriptions, no warehouse call.

    The companion to /api/schema/rebuild, for the source declaration flow: the
    caller already holds the columns and profiles, so editing a description and
    seeing the YAML update is instant.
    """
    source_name = str(request.need("source_name"))
    database = str(request.need("database"))
    schema = str(request.need("schema"))
    table = str(request.need("table"))
    columns = request.need("columns")
    if not isinstance(columns, list):
        raise ApiError("'columns' must be a list of column objects.")

    descriptions = request.opt("descriptions", {}) or {}
    profiles = request.opt("profiles", {}) or {}

    generated = codegen.source_yaml(
        source_name=source_name,
        database=database,
        schema=schema,
        table=table,
        columns=columns,
        table_description=str(request.opt("description", "")),
        profiles=profiles,
        include_tests=bool(request.opt("include_tests", True)),
        include_descriptions=bool(request.opt("include_descriptions", True)),
        ai_descriptions={k: v for k, v in descriptions.items() if str(v).strip()},
    )
    return 200, {
        "name": table,
        "yaml": generated["yaml"],
        "markdown": codegen.markdown_table(columns, profiles),
        "reference": generated["reference"],
        "stats": {
            "column_count": generated["column_count"],
            "documented": generated["documented"],
            "needs_review": generated["needs_review"],
        },
    }


def _blank_if_undocumented(columns: List[Dict[str, Any]],
                           include_descriptions: bool) -> List[Dict[str, Any]]:
    """
    When the caller asked for no descriptions (the 'None' engine), the editable
    proposal should open blank rather than pre-filled with pattern drafts. The
    YAML already omits them; this keeps the on-screen table consistent so the
    user is starting from a clean slate to type their own.
    """
    if include_descriptions:
        return columns
    for column in columns:
        column["description"] = ""
        column["needs_review"] = False
        column["description_source"] = "none"
    return columns


def _column_payload(
    column: Dict[str, Any],
    profiles: Dict[str, Dict[str, Any]],
    existing: Dict[str, str],
    ai_descriptions: Dict[str, str],
) -> Dict[str, Any]:
    """
    One column as the Documentation page consumes it.

    Description precedence: an AI description if one was generated, then a
    description already committed in the project's YAML, then the pattern
    engine's draft. `source` records which won so the UI can label it.
    """
    name = column["name"]
    profile = profiles.get(name)

    ai_text = (ai_descriptions.get(name) or "").strip()
    existing_text = (existing.get(name) or "").strip()

    if ai_text:
        description = ai_text
        needs_review = ai_text.lower().startswith("unclear")
        source = "ai"
    elif existing_text:
        description = existing_text
        needs_review = False
        source = "existing"
    else:
        drafted = codegen.describe(column, profile, "")
        description = drafted["description"]
        needs_review = drafted["needs_review"]
        source = drafted["source"]

    return {
        "name": name,
        "data_type": column["data_type"],
        "data_type_yaml": column["data_type"].lower(),
        "category": column["category"],
        "mode": column.get("mode"),
        "nullable": column.get("nullable"),
        "description": description,
        "needs_review": needs_review,
        "description_source": source,
        "profile": profile,
    }


def _attach_top_values(profiles: Dict[str, Dict[str, Any]],
                       relation: str, target: str) -> None:
    """Fetch top values for low-cardinality text columns, for accepted_values."""
    for name, prof in profiles.items():
        distinct = prof.get("distinct_count") or 0
        if (0 < distinct <= 10
                and prof.get("category") == "text"
                and (prof.get("distinct_pct") or 100) < 50):
            try:
                dist = profiling.value_distribution(
                    relation, name, target=target, top_n=distinct
                )
                prof["top_values"] = dist["values"]
            except warehouse.WarehouseError:
                prof["top_values"] = []


def _suggested_yaml_path(node: Optional[Dict[str, Any]], name: str) -> str:
    if node and node.get("patch_path"):
        # dbt stores this as 'package_name://models\bronze\_bronze__models.yml'
        # on Windows. Strip the package prefix and normalise the separators.
        patch = str(node["patch_path"])
        if "://" in patch:
            patch = patch.split("://", 1)[1]
        return patch.replace("\\", "/")
    if node and node.get("original_file_path"):
        path = pathlib.PurePosixPath(str(node["original_file_path"]).replace("\\", "/"))
        layer = path.parent.name or "models"
        return f"models/{layer}/_{layer}__models.yml"
    return f"models/_{name}.yml"


# --------------------------------------------------------------------------
# profiling + silver advisor
# --------------------------------------------------------------------------

@ROUTER.post("/api/profile")
def _profile(request: Request) -> Tuple[int, Any]:
    relation, node = _resolve_relation(request)
    sample = request.opt("sample_rows", config.SETTINGS.profile_sample_rows)
    try:
        profile = profiling.profile_relation(
            relation, target=request.target,
            sample_rows=int(sample) if sample else None,
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc

    profile["model"] = (node or {}).get("name")
    return 200, profile


@ROUTER.post("/api/advisor/analyse")
def _advisor_analyse(request: Request) -> Tuple[int, Any]:
    """
    Profile a bronze relation and recommend the silver work it implies.

    Deduplication advice needs a candidate key, so the key is inferred from the
    profile and then verified with a real group-by before anything is claimed.
    """
    relation, node = _resolve_relation(request)
    sample = request.opt("sample_rows", config.SETTINGS.profile_sample_rows)

    try:
        profile = profiling.profile_relation(
            relation, target=request.target,
            sample_rows=int(sample) if sample else None,
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc

    key_columns = request.opt("key_columns") or recommend._key_candidates(
        [c for c in profile["columns"] if not recommend._is_audit(c["name"])],
        profile["row_count"],
    )

    duplicate = None
    if key_columns:
        try:
            duplicate = profiling.duplicate_check(
                relation, list(key_columns), target=request.target
            )
        except warehouse.WarehouseError:
            duplicate = None

    analysis = recommend.analyse(profile, duplicate)
    analysis["model"] = (node or {}).get("name")
    analysis["layer"] = (node or {}).get("layer")
    analysis["duplicate_check"] = duplicate
    analysis["profile"] = profile
    analysis["suggested_model_name"] = codegen._silver_name(
        str((node or {}).get("name") or profile["table"]["table"])
    )
    return 200, analysis


def _advisor_context(
    request: Request,
) -> Tuple[str, str, Dict[str, Any], Dict[str, Any]]:
    """
    Re-profile a source (model or raw relation) and rebuild its analysis.

    Returns (source_model, relation, analysis, profile). source_model is empty
    for a foreign table; relation is always the physical relation that was
    profiled.

    Shared by the preview and the generator so the two can never disagree about
    what the recommendations are. Both re-measure rather than trusting a payload
    posted back by the browser: the accepted *ids* come from the client, the
    facts behind them do not.
    """
    # Accept either a dbt model or a raw relation, exactly like /analyse. A
    # foreign table has no model name; silver generation then reads the table by
    # its physical relation instead of ref().
    relation, node = _resolve_relation(request)
    source_model = str((node or {}).get("name") or "")

    try:
        profile = profiling.profile_relation(
            relation, target=request.target,
            sample_rows=config.SETTINGS.profile_sample_rows,
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc

    key_columns = request.opt("key_columns") or recommend._key_candidates(
        [c for c in profile["columns"] if not recommend._is_audit(c["name"])],
        profile["row_count"],
    )
    duplicate = None
    if key_columns:
        try:
            duplicate = profiling.duplicate_check(
                relation, list(key_columns), target=request.target
            )
        except warehouse.WarehouseError:
            duplicate = None

    analysis = recommend.analyse(profile, duplicate)
    # silver_plan reads the duplicate check to estimate the output row count,
    # and analyse() does not put it on the result.
    analysis["duplicate_check"] = duplicate
    return source_model, relation, analysis, profile


@ROUTER.post("/api/advisor/preview")
def _advisor_preview(request: Request) -> Tuple[int, Any]:
    """
    Explain how the silver model would be built, without building it.

    Nothing here writes, and nothing here queries the warehouse beyond the
    profile the analysis needs anyway.
    """
    source_model, relation, analysis, profile = _advisor_context(request)
    plan = codegen.silver_plan(
        source_model=source_model,
        source_relation=relation,
        analysis=analysis,
        profile=profile,
        accepted_ids=request.opt("accepted_ids"),
        model_name=str(request.opt("model_name", "")),
        materialized=str(request.opt("materialized", "view")),
    )
    return 200, plan


@ROUTER.post("/api/advisor/generate")
def _advisor_generate(request: Request) -> Tuple[int, Any]:
    """Turn the accepted recommendations into a silver model."""
    source_model, relation, analysis, profile = _advisor_context(request)
    generated = codegen.silver_model(
        source_model=source_model,
        source_relation=relation,
        analysis=analysis,
        profile=profile,
        accepted_ids=request.opt("accepted_ids"),
        model_name=str(request.opt("model_name", "")),
        materialized=str(request.opt("materialized", "view")),
    )
    return 200, generated


# --------------------------------------------------------------------------
# warehouse browsing
# --------------------------------------------------------------------------

@ROUTER.get("/api/access/settings")
def _access_settings(request: Request) -> Tuple[int, Any]:
    """
    Every dataset the credentials can see, flagged with whether the UI is
    currently allowed to use it.

    This is what makes the boundary configurable instead of hardcoded: the list
    comes from BigQuery, the ticks come from the saved settings, and saving
    writes them back to dbt_ui/.runtime/access.json.
    """
    target = request.q("target")
    allowed = set(config.allowed_datasets(target))

    visible: List[Dict[str, Any]] = []
    error: Optional[str] = None
    try:
        # all_datasets bypasses the allowlist on purpose: you cannot tick a
        # dataset you are not allowed to see in the picker.
        for entry in warehouse.list_all_datasets(target):
            name = str(entry.get("dataset_id") or "")
            visible.append({
                "dataset": name,
                "location": entry.get("location"),
                "allowed": name.lower() in allowed,
            })
    except warehouse.WarehouseError as exc:
        error = exc.message

    # A dataset can be allowed but no longer exist (renamed, dropped, or the
    # credentials lost sight of it). Surface those rather than dropping them
    # silently, so the saved list can be cleaned up.
    seen = {d["dataset"].lower() for d in visible}
    missing = sorted(name for name in allowed if name not in seen)

    return 200, {
        "datasets": sorted(visible, key=lambda d: d["dataset"].lower()),
        "missing": missing,
        "scope": config.scope_description(target),
        "user": config.current_request_user(),
        "permissions": request.permissions,
        "error": error,
    }


@ROUTER.post("/api/access/settings")
def _access_settings_save(request: Request) -> Tuple[int, Any]:
    """
    Persist the project-wide dataset allowlist.

    Roles are not settable here any more: they are per-user and live in the
    users table. See /api/users/role.
    """
    _require("can_modify_datasets", "change dataset access")

    datasets = request.opt("datasets")
    if datasets is None:
        raise ApiError("Provide 'datasets'.")
    if not isinstance(datasets, list):
        raise ApiError("'datasets' must be a list of dataset names.")
    if os.environ.get("DBT_UI_ALLOWED_DATASETS"):
        raise ApiError(
            "DBT_UI_ALLOWED_DATASETS is set in the environment, which takes "
            "precedence. Unset it to manage access from here.",
            status=409,
        )

    config.write_access_settings(datasets=list(datasets))

    # The allowlist feeds cached warehouse metadata, so those caches are now
    # stale in both directions (a newly allowed dataset was never fetched, a
    # revoked one must stop being served).
    warehouse.clear_schema_cache()
    warehouse.clear_inventory_cache()
    manifest_mod.invalidate()

    target = request.q("target")
    return 200, {
        "saved": True,
        "scope": config.scope_description(target),
        "note": "Reload the project so every screen picks up the new boundary.",
    }


@ROUTER.get("/api/warehouse/datasets")
def _datasets(request: Request) -> Tuple[int, Any]:
    try:
        return 200, {"datasets": warehouse.list_datasets(request.q("target"))}
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc


@ROUTER.get("/api/warehouse/tables")
def _tables(request: Request) -> Tuple[int, Any]:
    dataset = request.q("dataset")
    if not dataset:
        raise ApiError("'dataset' query parameter is required.")
    try:
        return 200, {
            "dataset": dataset,
            "tables": warehouse.list_tables(dataset, request.q("target")),
        }
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc


# --------------------------------------------------------------------------
# scheduled runs
# --------------------------------------------------------------------------

@ROUTER.get("/api/schedules")
def _schedules_list(request: Request) -> Tuple[int, Any]:
    """
    Every saved schedule, each with what Task Scheduler currently says about it.

    The live query matters: schedules.json records intent, but somebody can
    disable or delete the task in Task Scheduler and the UI would otherwise keep
    claiming it is scheduled.
    """
    records = schedules.list_schedules()
    return 200, {
        "schedules": [schedules.describe(record) for record in records],
        "runs": schedules.list_runs(limit=40),
        "lock": runlock.describe(),
        "commands": list(schedules.SCHEDULABLE_COMMANDS),
        "frequencies": [
            {"id": key, **value} for key, value in schedules.FREQUENCIES.items()
        ],
        "weekdays": list(schedules.WEEKDAYS),
        "targets": [target.to_dict()["name"] for target in config.list_targets()],
        "default_target": config.default_target_name(),
        "notes": schedules.environment_notes(),
        "windows": os.name == "nt",
        "scope": config.scope_description(request.q("target")),
    }


@ROUTER.post("/api/schedules")
def _schedules_save(request: Request) -> Tuple[int, Any]:
    """Create or update a schedule. Does not register it with Windows."""
    _require("can_configure", "create or change schedules")
    try:
        record = schedules.save(dict(request.body or {}))
    except schedules.ScheduleError as exc:
        raise ApiError(str(exc), status=422) from exc

    return 200, {
        "schedule": schedules.describe(record),
        "note": (
            "Saved. It will not run until you register it with Task Scheduler - "
            "that is a separate step because it makes dbt run unattended."
        ),
    }


@ROUTER.post("/api/schedules/delete")
def _schedules_delete(request: Request) -> Tuple[int, Any]:
    _require("can_configure", "delete schedules")
    schedule_id = str(request.need("id"))
    removed = schedules.delete(schedule_id)
    if not removed:
        raise ApiError(f"No schedule with id '{schedule_id}'.", status=404)
    return 200, {"deleted": True, "id": schedule_id}


@ROUTER.post("/api/schedules/register")
def _schedules_register(request: Request) -> Tuple[int, Any]:
    """
    Hand the schedule to Windows Task Scheduler, or take it back.

    Kept as an explicit action rather than folded into save: this is the moment
    dbt gains the ability to run against a real warehouse with nobody watching.
    """
    _require("can_configure", "register scheduled runs")
    schedule_id = str(request.need("id"))
    record = schedules.get_schedule(schedule_id)
    if record is None:
        raise ApiError(f"No schedule with id '{schedule_id}'.", status=404)

    action = str(request.opt("action") or "register").lower()

    try:
        if action == "unregister":
            result = schedules.unregister(record)
        else:
            result = schedules.register(record)
    except schedules.ScheduleError as exc:
        raise ApiError(str(exc), status=422) from exc

    return 200, {**result, "schedule": schedules.describe(record)}


@ROUTER.get("/api/schedules/runs")
def _schedules_runs(request: Request) -> Tuple[int, Any]:
    return 200, {
        "runs": schedules.list_runs(
            schedule_id=request.q("id"),
            limit=int(request.q("limit") or 50),
        ),
        "lock": runlock.describe(),
    }


@ROUTER.get("/api/schedules/log")
def _schedules_log(request: Request) -> Tuple[int, Any]:
    name = request.q("log")
    if not name:
        raise ApiError("'log' query parameter is required.")
    try:
        return 200, {"log": name, "text": schedules.run_log(str(name))}
    except schedules.ScheduleError as exc:
        raise ApiError(str(exc), status=404) from exc


@ROUTER.post("/api/models/scaffold")
def _model_scaffold(request: Request) -> Tuple[int, Any]:
    """
    Preview the model file a workbench query would become. Writes nothing.

    Separated from the write on purpose: the ref() rewriting has to be reviewed
    before it lands, because a rewrite that matched on table name alone is a
    judgement call the user should see rather than discover in a diff. The actual
    write reuses /api/files/write, which already keeps a .bak.
    """
    sql = request.need("sql")

    try:
        payload = modelgen.scaffold(
            _manifest(),
            name=str(request.opt("name") or ""),
            sql=str(sql),
            layer=str(request.opt("layer") or "silver"),
            materialized=str(request.opt("materialized") or ""),
            description=str(request.opt("description") or ""),
            rewrite=request.opt("rewrite", True) is not False,
        )
    except ValueError as exc:
        raise ApiError(str(exc), status=422) from exc

    payload["allowed_layers"] = [
        layer for layer in config.ALLOWED_LAYERS
        if layer not in config.blocked_build_layers()
    ]
    payload["materializations"] = list(modelgen.MATERIALIZATIONS)
    return 200, payload


@ROUTER.get("/api/warehouse/inventory")
def _inventory(request: Request) -> Tuple[int, Any]:
    """
    Every in-scope physical table with its row count, size and last-modified,
    cross-referenced against the manifest.

    The cross-reference is the point. The warehouse knows what physically exists
    and how big it is; the manifest knows which of those things dbt builds and
    which layer they belong to. A picker needs both, and joining them here means
    the frontend gets one list instead of reconciling two.
    """
    target = request.q("target")
    refresh = request.q("refresh") in ("1", "true", "yes")

    try:
        data = warehouse.inventory(target, refresh=refresh)
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc

    # Index the manifest by the physical relation each node builds. dbt's
    # `schema` is the BigQuery dataset and `alias` is the table name, which is
    # what makes this a reliable join even when the model name differs from the
    # table name.
    by_table: Dict[str, Dict[str, Any]] = {}
    try:
        for node in _manifest().buildable_nodes():
            dataset = str(node.get("schema") or "").lower()
            alias = str(node.get("alias") or node.get("name") or "").lower()
            if dataset and alias:
                by_table[f"{dataset}.{alias}"] = node
    except ApiError:
        # No manifest parsed yet. The physical inventory still stands on its own,
        # so carry on without the cross-reference rather than failing the page.
        pass

    tables = []
    for table in data.get("tables", []):
        node = by_table.get(f"{table['dataset']}.{table['table']}".lower())
        tables.append({
            **table,
            "model": node.get("name") if node else None,
            "layer": node.get("layer") if node else None,
            "resource_type": node.get("resource_type") if node else None,
            "materialized": node.get("materialized") if node else None,
            "column_count": node.get("column_count") if node else None,
            "documented_columns": node.get("documented_columns") if node else None,
            "test_count": node.get("test_count") if node else None,
            # Anything dbt does not build is a foreign table: safe to read and
            # document, but the UI must never offer to rebuild it.
            "managed_by_dbt": node is not None,
        })

    return 200, {
        "project": data.get("project"),
        "datasets": data.get("datasets", []),
        "tables": tables,
        "table_count": len(tables),
        "managed_count": sum(1 for t in tables if t["managed_by_dbt"]),
        "view_count": sum(1 for t in tables if t["is_view"]),
        "fetched_at": data.get("fetched_at"),
        "cache_ttl": warehouse.INVENTORY_CACHE_TTL,
        "error": data.get("error"),
        "scope": config.scope_description(target),
    }


@ROUTER.post("/api/warehouse/describe")
def _describe(request: Request) -> Tuple[int, Any]:
    relation, _ = _resolve_relation(request)
    try:
        return 200, warehouse.describe_relation(relation, target=request.target)
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc


@ROUTER.post("/api/warehouse/preview")
def _preview(request: Request) -> Tuple[int, Any]:
    """Preview rows of a relation, bounded and read-only."""
    relation, node = _resolve_relation(request)
    limit = int(request.opt("limit", config.SETTINGS.preview_row_limit))
    sql = f"select * from {relation}"
    try:
        result = warehouse.execute(
            jinja_sql.wrap_with_limit(sql, limit),
            target=request.target, limit=limit,
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc) from exc
    return 200, {
        "relation": relation,
        "model": (node or {}).get("name"),
        "result": result.to_dict(),
        "yaml": codegen.columns_yaml_fragment(result.columns),
    }


# --------------------------------------------------------------------------
# dbt commands
# --------------------------------------------------------------------------

@ROUTER.post("/api/dbt/run")
def _dbt_run(request: Request) -> Tuple[int, Any]:
    _require("can_run_dbt", "run dbt commands")

    command = str(request.need("command"))
    if command not in runner.ALLOWED_COMMANDS:
        raise ApiError(
            f"'{command}' is not an allowed dbt command. Allowed: "
            f"{', '.join(sorted(runner.ALLOWED_COMMANDS))}."
        )

    target = request.target
    meta = runner.ALLOWED_COMMANDS[command]
    if meta["writes"] and target == "prod" and not request.opt("confirm_prod"):
        raise ApiError(
            "This command writes tables and the selected target is prod. "
            "Re-send with confirm_prod: true if that is really intended.",
            status=428,
            requires_confirmation=True,
        )

    try:
        job = runner.launch(
            command,
            target=target,
            select=request.opt("select"),
            exclude=request.opt("exclude"),
            full_refresh=bool(request.opt("full_refresh", False)),
            threads=request.opt("threads"),
        )
    except runner.JobBusyError as exc:
        raise ApiError(str(exc), status=409, busy=True) from exc
    except runner.BlockedLayerError as exc:
        raise ApiError(
            str(exc),
            status=403,
            scope_violation=True,
            blocked_layers=config.blocked_build_layers(),
            detail=(
                f"dbt runs from this UI always exclude "
                f"{', '.join('tag:' + l for l in config.blocked_build_layers())}. "
                f"Building those layers is the orchestrator's job, from the "
                f"command line."
            ),
        ) from exc
    except ValueError as exc:
        raise ApiError(str(exc)) from exc

    return 202, {"job": job.summary()}


@ROUTER.get("/api/dbt/jobs")
def _dbt_jobs(request: Request) -> Tuple[int, Any]:
    active = runner.REGISTRY.active()
    return 200, {
        "jobs": runner.REGISTRY.list(),
        "active": active.summary() if active else None,
    }


@ROUTER.get("/api/dbt/jobs/<job_id>")
def _dbt_job(request: Request) -> Tuple[int, Any]:
    job_id = str(request.body.get("job_id"))
    job = runner.REGISTRY.get(job_id)
    if job is None:
        raise ApiError(f"No job with id {job_id}.", 404)

    cursor = request.q_int("cursor", 0)
    lines = job.lines_after(cursor)
    payload = {
        "job": job.summary(),
        "lines": lines,
        "cursor": lines[-1]["seq"] if lines else cursor,
    }
    if not job.summary()["is_active"]:
        # The run is over, so hand back the refreshed project state with the
        # final log lines. Saves the frontend a second round trip.
        payload["last_run"] = manifest_mod.last_run_results()
        refreshed = manifest_mod.try_load()
        payload["stats"] = refreshed.stats() if refreshed else None
        payload["docs_available"] = (
            config.TARGET_DIR / "static_index.html"
        ).exists()
    return 200, payload


@ROUTER.post("/api/dbt/jobs/<job_id>/cancel")
def _dbt_cancel(request: Request) -> Tuple[int, Any]:
    _require("can_run_dbt", "cancel dbt runs")
    job_id = str(request.body.get("job_id"))
    if not runner.cancel(job_id):
        raise ApiError("That job is not running, so there is nothing to cancel.")
    return 200, {"cancelled": True, "job_id": job_id}


# --------------------------------------------------------------------------
# editable documentation
# --------------------------------------------------------------------------

def _schema_path_for(model_name: str) -> pathlib.Path:
    """
    Locate the YAML file that documents a model.

    Uses the manifest's patch_path, which is dbt's own record of which file
    supplied the model's documentation. Falls back to the conventional
    per-layer filename when the model has no YAML yet.
    """
    mf = _manifest()
    node = mf.node_detail(model_name)
    if node is None:
        raise ApiError(f"No model or seed named '{model_name}'.", 404)

    patch = node.get("patch_path")
    if patch:
        relative = str(patch).split("://", 1)[-1].replace("\\", "/")
        candidate = (config.PROJECT_DIR / relative).resolve()
        if candidate.is_file():
            return candidate

    raise ApiError(
        f"'{model_name}' has no schema YAML yet, so there is nothing to edit. "
        f"Generate one first from the Documentation page.",
        status=409,
        needs_generate=True,
        suggested_path=_suggested_yaml_path(node, model_name),
    )


@ROUTER.get("/api/docs/editable")
def _docs_editable(request: Request) -> Tuple[int, Any]:
    """
    The model's committed documentation, with the file mtime.

    The mtime is the conflict token: the client sends it back on save and the
    write is refused if the file moved on in the meantime.
    """
    name = request.q("model")
    if not name:
        raise ApiError("'model' query parameter is required.")

    path = _schema_path_for(str(name))

    try:
        docs = yamlpatch.read_doc(path, str(name))
    except yamlpatch.PatchError as exc:
        raise ApiError(str(exc), status=422) from exc

    doc = docs[0]
    mf = _manifest()
    node = mf.node_detail(str(name)) or {}

    return 200, {
        "model": doc.name,
        "path": str(path.relative_to(config.PROJECT_DIR)).replace("\\", "/"),
        "mtime": path.stat().st_mtime,
        "layer": node.get("layer"),
        "resource_type": node.get("resource_type"),
        "model_description": doc.description,
        "model_has_description": doc.has_description,
        "columns": [
            {
                "name": column.name,
                "data_type": (column.data_type or "").lower() or None,
                "description": column.description,
                "has_description": column.has_description,
            }
            for column in doc.columns
        ],
        "documented": sum(1 for c in doc.columns if c.has_description),
        "column_count": len(doc.columns),
    }


@ROUTER.post("/api/docs/patch")
def _docs_patch(request: Request) -> Tuple[int, Any]:
    """
    Write edited descriptions back into the schema YAML.

    Only description values are touched. Comments, tests, config and key order
    survive byte for byte, and the patcher refuses to write if a structural
    comparison shows anything else changed.
    """
    _require("can_write_files", "edit documentation")

    name = str(request.need("model"))
    path = _schema_path_for(name)

    columns = request.opt("columns") or {}
    if not isinstance(columns, dict):
        raise ApiError("'columns' must be an object of column name to description.")

    model_description = request.body.get("model_description")
    expected = request.opt("mtime")

    try:
        result = yamlpatch.patch_descriptions(
            path,
            name,
            model_description=(
                str(model_description) if model_description is not None else None
            ),
            column_descriptions={str(k): str(v) for k, v in columns.items()},
            expected_mtime=float(expected) if expected else None,
            allow_clearing=bool(request.opt("allow_clearing", False)),
        )
    except yamlpatch.ConflictError as exc:
        raise ApiError(
            exc.message,
            status=409,
            conflict=True,
            disk_mtime=exc.disk_mtime,
            expected_mtime=exc.expected_mtime,
            current=exc.current,
            detail=(
                "Someone or something else changed this file - a git pull, an "
                "editor, or another browser tab. Reload to see the current "
                "text, then reapply your edit."
            ),
        ) from exc
    except yamlpatch.PatchError as exc:
        raise ApiError(str(exc), status=422) from exc

    # The manifest still holds the old descriptions until dbt parses again.
    if result["written"]:
        manifest_mod.invalidate()

    if result.get("backup"):
        result["backup"] = str(
            pathlib.Path(result["backup"]).relative_to(config.PROJECT_DIR)
        ).replace("\\", "/")
    result["path"] = str(path.relative_to(config.PROJECT_DIR)).replace("\\", "/")
    result["note"] = (
        "Saved to the file. Reload the project so dbt and the rest of the UI "
        "pick it up."
    )
    return 200, result


@ROUTER.get("/api/docs/export")
def _docs_export(request: Request) -> Tuple[int, Any]:
    """
    The committed documentation in a downloadable shape.

    Returns every format in one response so the client can offer .yml, .json,
    .md and .csv without four round trips. The .yml is the file exactly as it is
    on disk, so a download always matches what dbt would read.
    """
    name = request.q("model")
    if not name:
        raise ApiError("'model' query parameter is required.")

    path = _schema_path_for(str(name))
    raw = path.read_text(encoding="utf-8")

    try:
        docs = yamlpatch.read_doc(path, str(name))
    except yamlpatch.PatchError as exc:
        raise ApiError(str(exc), status=422) from exc

    doc = docs[0]
    columns = [
        {
            "name": column.name,
            "data_type": column.data_type,
            "description": column.description,
        }
        for column in doc.columns
    ]

    markdown_rows = [
        "| Column | Type | Description |",
        "| --- | --- | --- |",
    ] + [
        f"| `{c['name']}` | {(c['data_type'] or '').lower()} | "
        f"{(c['description'] or '').replace('|', chr(92) + '|')} |"
        for c in columns
    ]
    markdown = (
        f"# {doc.name}\n\n{doc.description or '_No description._'}\n\n"
        f"## Columns\n\n" + "\n".join(markdown_rows) + "\n"
    )

    # CSV for the people who want the documentation in a spreadsheet. Written
    # here rather than in the browser so every export format comes from the same
    # parsed file and cannot drift. RFC 4180 quoting via the csv module.
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer, lineterminator="\n")
    writer.writerow(["model", "model_description", "column", "data_type",
                     "description"])
    for column in columns:
        writer.writerow([
            doc.name,
            doc.description or "",
            column["name"],
            (column["data_type"] or "").lower(),
            column["description"] or "",
        ])

    return 200, {
        "model": doc.name,
        "path": str(path.relative_to(config.PROJECT_DIR)).replace("\\", "/"),
        "mtime": path.stat().st_mtime,
        "yml": raw,
        "json": {
            "model": doc.name,
            "description": doc.description,
            "columns": columns,
        },
        "markdown": markdown,
        "csv": csv_buffer.getvalue(),
    }


# --------------------------------------------------------------------------
# AI documentation
# --------------------------------------------------------------------------

@ROUTER.get("/api/ai/status")
def _ai_status(request: Request) -> Tuple[int, Any]:
    """Whether AI documentation is usable, and which models are on offer."""
    return 200, ai_docs.status()


@ROUTER.post("/api/ai/key")
def _ai_key(request: Request) -> Tuple[int, Any]:
    """
    Save or clear the Gemini API key.

    The key is only ever written to dbt_ui/.runtime/ai.json (gitignored) and is
    never returned to the browser except as a masked prefix.
    """
    _require("can_configure", "change the API key")
    action = str(request.opt("action", "save")).lower()

    try:
        if action == "clear":
            result = ai_docs.clear_key()
        else:
            result = ai_docs.save_key(str(request.need("api_key")))
    except ai_docs.AiError as exc:
        raise _warehouse_failure(exc) from exc

    result["status"] = ai_docs.status()
    return 200, result


@ROUTER.post("/api/manifest/refresh")
def _manifest_refresh(request: Request) -> Tuple[int, Any]:
    """Run dbt parse to rebuild the manifest."""
    _require("can_run_dbt", "run dbt parse")
    manifest_mod.invalidate()
    try:
        job = runner.launch("parse", target=request.target)
    except runner.JobBusyError as exc:
        raise ApiError(str(exc), status=409, busy=True) from exc
    return 202, {"job": job.summary()}


# --------------------------------------------------------------------------
# file writing
# --------------------------------------------------------------------------

WRITABLE_SUFFIXES = {".sql", ".yml", ".yaml", ".md", ".csv"}


def _require(permission: str, what: str) -> None:
    """
    Refuse an action the signed-in user's role does not carry.

    This is the real enforcement point, not a UI convenience. Hiding a button in
    the frontend stops an honest mistake; this stops a hand-rolled curl. The role
    comes from the users table via the per-request context, so a role change
    takes effect on the very next request even for an existing session.
    """
    user = config.current_request_user()
    if user is None:
        raise ApiError("Sign in to do that.", status=401, unauthenticated=True)

    perms = config.role_permissions(user.get("role"))
    if perms.get(permission):
        return
    raise ApiError(
        f"The {perms.get('label')} role cannot {what}.",
        status=403,
        role=perms.get("role"),
        needed=permission,
    )


# --------------------------------------------------------------------------
# authentication
#
# Every /api route requires a valid session except the few listed here: the
# login endpoint itself, the health probe, and the role matrix (which the login
# screen shows before anyone has signed in). Everything else is refused with 401
# so the frontend can send the user to the login screen.
# --------------------------------------------------------------------------

PUBLIC_ROUTES = frozenset({
    "/api/health",
    "/api/auth/login",
    "/api/auth/session",
    "/api/auth/roles",
})


@ROUTER.post("/api/auth/login")
def _auth_login(request: Request) -> Tuple[int, Any]:
    """Exchange email and password for a session cookie."""
    email = str(request.need("email"))
    password = str(request.need("password"))

    try:
        token, user = auth.login(email, password)
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc

    request.response_headers["Set-Cookie"] = auth.session_cookie(token)
    return 200, {
        "user": user,
        "permissions": config.role_permissions(user["role"]),
    }


@ROUTER.post("/api/auth/logout")
def _auth_logout(request: Request) -> Tuple[int, Any]:
    auth.logout(request.session_token)
    request.response_headers["Set-Cookie"] = auth.clear_cookie()
    return 200, {"ok": True}


@ROUTER.get("/api/auth/session")
def _auth_session(request: Request) -> Tuple[int, Any]:
    """
    Who am I? Public on purpose: the frontend calls this on load to decide
    between the app and the login screen, and it must not 401-loop.
    """
    user = auth.resolve_session(request.session_token)
    if user is None:
        return 200, {"authenticated": False}
    return 200, {
        "authenticated": True,
        "user": user,
        "permissions": config.role_permissions(user["role"]),
    }


@ROUTER.get("/api/auth/roles")
def _auth_roles(request: Request) -> Tuple[int, Any]:
    """The permission matrix. Read-only reference, safe to show pre-login."""
    return 200, config.role_catalogue()


@ROUTER.post("/api/roles/permission")
def _role_permission(request: Request) -> Tuple[int, Any]:
    """
    Flip one cell of the permission matrix: role + permission -> true/false.

    Manager-only (can_modify_roles). Persisted as an override merged over the
    built-in defaults, so it survives a restart and applies to every user
    holding that role on their next request. Guardrails in config reject pinned
    permissions and any change that would leave no role able to edit the matrix.
    """
    _require("can_modify_roles", "change role permissions")

    role = str(request.need("role"))
    permission = str(request.need("permission"))
    value = bool(request.opt("value", False))

    try:
        catalogue = config.write_role_permission(role, permission, value)
    except ValueError as exc:
        raise ApiError(str(exc), status=409) from exc

    return 200, {
        "saved": True,
        "roles": catalogue,
        "note": "The change applies to that role on the next request.",
    }


@ROUTER.post("/api/auth/password")
def _auth_password(request: Request) -> Tuple[int, Any]:
    """
    Change your own password. Requires the current one, so a borrowed session
    cannot be used to lock the real owner out.
    """
    user = config.current_request_user()
    if user is None:
        raise ApiError("Sign in to do that.", status=401, unauthenticated=True)

    current = str(request.need("current_password"))
    new = str(request.need("new_password"))

    try:
        auth.login(user["email"], current)      # verifies, and prunes sessions
        auth.set_password(int(user["id"]), new)
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc

    return 200, {"ok": True, "note": "Password updated."}


# --------------------------------------------------------------------------
# user management - Manager only
# --------------------------------------------------------------------------

@ROUTER.get("/api/users")
def _users_list(request: Request) -> Tuple[int, Any]:
    """
    Every registered user with their role and dataset grants.

    Gated on can_manage_access, so an Admin or Analyst calling this directly
    gets a 403 rather than a user list.
    """
    _require("can_manage_access", "view the user list")
    return 200, {
        "users": auth.list_users(),
        "roles": config.role_catalogue(),
        "stats": auth.stats(),
    }


@ROUTER.post("/api/users/create")
def _users_create(request: Request) -> Tuple[int, Any]:
    _require("can_modify_roles", "create users")
    try:
        user = auth.create_user(
            str(request.need("email")),
            str(request.need("password")),
            str(request.need("role")),
        )
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc
    return 200, {"user": user}


@ROUTER.post("/api/users/role")
def _users_set_role(request: Request) -> Tuple[int, Any]:
    """Change a user's role. Written to the database immediately."""
    _require("can_modify_roles", "change user roles")

    actor = config.current_request_user() or {}
    user_id = int(request.need("user_id"))
    role = str(request.need("role"))

    # A Manager demoting themselves would immediately lose the ability to undo
    # it. auth.set_role already refuses to remove the last Manager; this catches
    # the self-demotion case with a clearer message.
    if int(actor.get("id") or 0) == user_id and role != auth.ROLE_MANAGER:
        raise ApiError(
            "You cannot remove your own Manager role. Ask another Manager to "
            "change it, so you are not locked out of user management.",
            status=409,
        )

    try:
        user = auth.set_role(user_id, role, acting_user_id=actor.get("id"))
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc

    return 200, {
        "user": user,
        "note": "Saved. The new role applies to that user's next request.",
    }


@ROUTER.post("/api/users/active")
def _users_set_active(request: Request) -> Tuple[int, Any]:
    _require("can_modify_roles", "enable or disable users")
    actor = config.current_request_user() or {}
    user_id = int(request.need("user_id"))
    is_active = bool(request.opt("is_active", True))

    if int(actor.get("id") or 0) == user_id and not is_active:
        raise ApiError("You cannot disable your own account.", status=409)

    try:
        user = auth.set_active(user_id, is_active)
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc
    return 200, {"user": user}


@ROUTER.post("/api/users/datasets")
def _users_set_datasets(request: Request) -> Tuple[int, Any]:
    """
    Restrict one user to a subset of the project's datasets.

    An empty list removes the per-user restriction, so the project-wide
    allowlist applies. A grant can only ever narrow that list.
    """
    _require("can_manage_access", "manage user dataset access")
    user_id = int(request.need("user_id"))
    datasets = request.opt("datasets", [])
    if not isinstance(datasets, list):
        raise ApiError("'datasets' must be a list of dataset names.")

    try:
        saved = auth.set_user_datasets(user_id, list(datasets))
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc

    return 200, {
        "user_id": user_id,
        "datasets": saved,
        "note": ("Saved. An empty list means the project-wide allowlist applies."
                 if not saved else "Saved."),
    }


@ROUTER.post("/api/users/password")
def _users_set_password(request: Request) -> Tuple[int, Any]:
    """Reset another user's password. Manager only."""
    _require("can_modify_roles", "reset user passwords")
    user_id = int(request.need("user_id"))
    try:
        auth.set_password(user_id, str(request.need("password")))
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc
    return 200, {"ok": True}


@ROUTER.post("/api/users/delete")
def _users_delete(request: Request) -> Tuple[int, Any]:
    _require("can_modify_roles", "delete users")
    actor = config.current_request_user() or {}
    user_id = int(request.need("user_id"))
    if int(actor.get("id") or 0) == user_id:
        raise ApiError("You cannot delete your own account.", status=409)
    try:
        auth.delete_user(user_id)
    except auth.AuthError as exc:
        raise ApiError(exc.message, status=exc.status, **exc.extra) from exc
    return 200, {"ok": True}


def _safe_project_path(raw: str) -> pathlib.Path:
    """
    Resolve a user-supplied path inside the project, refusing escapes.

    Both the extension allow-list and the containment check matter: the browser
    can ask to write a model or a schema file, and nothing else, anywhere else.
    """
    candidate = str(raw).strip().replace("\\", "/").lstrip("/")
    if not candidate:
        raise ApiError("'path' is required.")

    root = config.PROJECT_DIR.resolve()
    resolved = (root / candidate).resolve()

    if resolved == root or root not in resolved.parents:
        raise ApiError(
            f"Refusing to write outside the dbt project: {candidate}", status=403
        )
    if resolved.suffix.lower() not in WRITABLE_SUFFIXES:
        raise ApiError(
            f"Only {', '.join(sorted(WRITABLE_SUFFIXES))} files can be written "
            f"from the UI.", status=403,
        )
    for part in resolved.relative_to(root).parts:
        if part in ("target", "dbt_packages", "logs", ".git"):
            raise ApiError(
                f"'{part}/' is a generated directory and is not writable.",
                status=403,
            )
    return resolved


@ROUTER.post("/api/files/write")
def _file_write(request: Request) -> Tuple[int, Any]:
    _require("can_write_files", "write files into the project")

    path = _safe_project_path(str(request.need("path")))
    content = str(request.need("content"))
    mode = str(request.opt("mode", "overwrite"))

    existed = path.exists()
    if existed and mode == "fail":
        raise ApiError(f"{path.name} already exists.", status=409, exists=True)

    backup: Optional[str] = None
    if existed and mode == "overwrite":
        # Keep one backup so an accidental overwrite is recoverable without git.
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        backup = str(backup_path.relative_to(config.PROJECT_DIR))

    path.parent.mkdir(parents=True, exist_ok=True)
    if existed and mode == "append":
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write("\n" + content.rstrip() + "\n")
    else:
        path.write_text(content.rstrip() + "\n", encoding="utf-8", newline="\n")

    manifest_mod.invalidate()
    return 200, {
        "written": str(path.relative_to(config.PROJECT_DIR)).replace("\\", "/"),
        "absolute": str(path),
        "existed": existed,
        "backup": backup,
        "bytes": path.stat().st_size,
        "note": "Run 'Refresh manifest' (dbt parse) so dbt picks up the change.",
    }


@ROUTER.post("/api/files/read")
def _file_read(request: Request) -> Tuple[int, Any]:
    path = _safe_project_path(str(request.need("path")))
    if not path.exists():
        raise ApiError(f"{path.name} does not exist.", 404)
    return 200, {
        "path": str(path.relative_to(config.PROJECT_DIR)).replace("\\", "/"),
        "content": path.read_text(encoding="utf-8"),
    }


# --------------------------------------------------------------------------
# entry point used by the HTTP layer
# --------------------------------------------------------------------------

def handle(method: str, path: str, query: Dict[str, List[str]],
           body: Dict[str, Any],
           headers: Optional[Dict[str, str]] = None,
           ) -> Tuple[int, Any, Dict[str, str]]:
    """
    Dispatch one API call.

    Returns (status, payload, response_headers). The auth gate runs here rather
    than in each handler so a new route cannot accidentally be left unprotected:
    anything not explicitly listed in PUBLIC_ROUTES requires a valid session.
    """
    request = Request(method, path, query, body, headers)

    try:
        # ---- authenticate ------------------------------------------------
        user = auth.resolve_session(request.session_token)
        request.user = user
        config.set_request_user(user)

        if path not in PUBLIC_ROUTES and user is None:
            return 401, {
                "error": "Sign in to use dbt Studio.",
                "unauthenticated": True,
            }, {}

        status, payload = ROUTER.dispatch(request)
        return status, payload, request.response_headers

    except ApiError as exc:
        return exc.status, exc.payload(), request.response_headers
    except auth.AuthError as exc:
        return exc.status, {"error": exc.message, **exc.extra}, {}
    except warehouse.ScopeError as exc:
        # A policy refusal, not a bad request. Must precede WarehouseError.
        return 403, exc.to_dict(), {}
    except warehouse.WarehouseError as exc:
        return 400, exc.to_dict(), {}
    except jinja_sql.CompileError as exc:
        return 400, exc.to_dict(), {}
    except manifest_mod.ManifestNotFound as exc:
        return 409, {"error": str(exc), "needs_parse": True}, {}
    except Exception as exc:  # pragma: no cover
        import traceback
        return 500, {
            "error": f"Unhandled server error: {exc}",
            "type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        }, {}
    finally:
        # Never let one request's identity leak into the next on this thread.
        config.clear_request_user()

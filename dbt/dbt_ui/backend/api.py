"""
JSON API.

A small explicit router rather than a framework. Handlers take a parsed request
and return (status, payload); the HTTP layer knows nothing about dbt and this
layer knows nothing about sockets.
"""

from __future__ import annotations

import pathlib
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (
    ai_docs,
    codegen,
    config,
    jinja_sql,
    manifest as manifest_mod,
    modelgen,
    profiling,
    recommend,
    runner,
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
                 query: Dict[str, List[str]], body: Dict[str, Any]):
        self.method = method
        self.path = path
        self.query = query
        self.body = body if isinstance(body, dict) else {}

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


@ROUTER.get("/api/sources")
def _sources(request: Request) -> Tuple[int, Any]:
    mf = _manifest()
    return 200, {"sources": mf.source_summaries()}


@ROUTER.get("/api/graph")
def _graph(request: Request) -> Tuple[int, Any]:
    return 200, _manifest().graph()


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

    executed = jinja_sql.wrap_with_limit(compiled["compiled_sql"], limit)
    try:
        result = warehouse.execute(
            executed, target=request.target, limit=limit, apply_limit=True
        )
    except warehouse.WarehouseError as exc:
        raise _warehouse_failure(exc, sql=executed) from exc

    return 200, {
        "compiled": compiled,
        "result": result.to_dict(),
        "yaml": codegen.columns_yaml_fragment(result.columns),
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
        "columns": [
            _column_payload(c, profiles, existing, ai_descriptions)
            for c in typing_map.flatten_columns(columns)
        ],
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


@ROUTER.post("/api/advisor/generate")
def _advisor_generate(request: Request) -> Tuple[int, Any]:
    """Turn the accepted recommendations into a silver model."""
    source_model = str(request.need("model"))
    accepted = request.opt("accepted_ids")
    model_name = str(request.opt("model_name", ""))
    materialized = str(request.opt("materialized", "view"))

    mf = _manifest()
    node = mf.node_detail(source_model)
    if node is None:
        raise ApiError(f"No model named '{source_model}'.", 404)
    if not node.get("relation_name"):
        raise ApiError(f"Build '{source_model}' before generating from it.")

    relation = str(node["relation_name"])
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
    generated = codegen.silver_model(
        source_model=source_model,
        analysis=analysis,
        profile=profile,
        accepted_ids=accepted,
        model_name=model_name,
        materialized=materialized,
    )
    return 200, generated


# --------------------------------------------------------------------------
# warehouse browsing
# --------------------------------------------------------------------------

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

    Returns all three formats in one response so the client can offer .yml,
    .json and .md without three round trips. The .yml is the file exactly as it
    is on disk, so a download always matches what dbt would read.
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
           body: Dict[str, Any]) -> Tuple[int, Any]:
    request = Request(method, path, query, body)
    try:
        return ROUTER.dispatch(request)
    except ApiError as exc:
        return exc.status, exc.payload()
    except warehouse.ScopeError as exc:
        # A policy refusal, not a bad request. Must precede WarehouseError.
        return 403, exc.to_dict()
    except warehouse.WarehouseError as exc:
        return 400, exc.to_dict()
    except jinja_sql.CompileError as exc:
        return 400, exc.to_dict()
    except manifest_mod.ManifestNotFound as exc:
        return 409, {"error": str(exc), "needs_parse": True}
    except Exception as exc:  # pragma: no cover
        import traceback
        return 500, {
            "error": f"Unhandled server error: {exc}",
            "type": type(exc).__name__,
            "traceback": traceback.format_exc(limit=8),
        }

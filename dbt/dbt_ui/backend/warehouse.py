"""
BigQuery access, driven entirely by the dbt profile.

Nobody using this UI ever types a project id or a dataset name into a
connection dialog. The connection is whatever `profiles.yml` says for the
selected target, so the UI and `dbt run` are guaranteed to be pointing at the
same place.

Two guardrails are always on:

  * maximum_bytes_billed  - BigQuery refuses the job rather than running it, so
                            a careless `select *` on a huge table costs nothing.
  * a row limit           - wrapped around preview queries.

Type introspection uses a dry run. BigQuery returns the full result schema for
a dry run without executing anything and without billing a byte, which is what
lets the UI answer "what columns and types will this produce" instantly.
"""

from __future__ import annotations

import datetime
import decimal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import config, sql_scope, typing_map


class WarehouseError(RuntimeError):
    """Connection or query failure, already carrying a human-readable message."""

    def __init__(self, message: str, *, detail: str = "", sql: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.sql = sql

    def to_dict(self) -> Dict[str, Any]:
        return {"error": self.message, "detail": self.detail, "sql": self.sql}


class ScopeError(WarehouseError):
    """
    The request reached outside the configured dataset allowlist.

    Separate from WarehouseError so the API can answer 403 rather than 400: this
    is a policy refusal, not a malformed query, and the UI presents it
    differently.
    """

    def __init__(self, message: str, *, detail: str = "", sql: str = "",
                 offending: Optional[List[str]] = None,
                 allowed: Optional[List[str]] = None):
        super().__init__(message, detail=detail, sql=sql)
        self.offending = offending or []
        self.allowed = allowed or []

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update({
            "scope_violation": True,
            "offending_datasets": self.offending,
            "allowed_datasets": self.allowed,
        })
        return payload


# --------------------------------------------------------------------------
# dataset scope enforcement
# --------------------------------------------------------------------------

def assert_dataset_allowed(dataset: str, target: Optional[str] = None,
                           context: str = "") -> None:
    """Refuse a single dataset that is outside the allowlist."""
    allowed = config.allowed_datasets(target)
    name = str(dataset or "").strip().lower()

    if name in set(allowed):
        return

    where = f" while {context}" if context else ""
    raise ScopeError(
        f"Dataset '{dataset}' is outside the permitted scope{where}.",
        detail=(
            f"This dbt Studio instance is restricted to: {', '.join(allowed)}.\n\n"
            f"Everything else in the project, including the gold layer, seeds "
            f"and the production datasets, is refused before any query runs.\n\n"
            f"To change the boundary, set DBT_UI_ALLOWED_DATASETS or edit "
            f"BASE_ALLOWED_DATASETS in dbt_ui/backend/config.py, then restart."
        ),
        offending=[str(dataset)],
        allowed=allowed,
    )


def _assert_sql_in_scope(sql: str, target: Optional[str] = None) -> None:
    """
    Syntactic half of the guard: every dataset the statement names must be
    allowed. Runs before the query is sent, so a refused statement costs
    nothing at all.
    """
    cfg = config.target_config(target)
    named = sql_scope.datasets_named(sql, default_project=str(cfg.get("project") or ""))
    allowed = set(config.allowed_datasets(target))

    offending = sorted(name for name in named if name not in allowed)
    if not offending:
        return

    raise ScopeError(
        f"This statement reads {', '.join(offending)}, which "
        f"{'is' if len(offending) == 1 else 'are'} outside the permitted scope.",
        detail=(
            f"Permitted datasets: {', '.join(sorted(allowed))}.\n\n"
            f"Nothing was sent to BigQuery. Rewrite the statement to read only "
            f"the permitted datasets, ideally through ref() so the target "
            f"decides the physical location."
        ),
        sql=sql,
        offending=offending,
        allowed=sorted(allowed),
    )


def _assert_referenced_in_scope(job: Any, sql: str,
                                target: Optional[str] = None) -> None:
    """
    Semantic half of the guard, using BigQuery's own plan.

    `referenced_tables` lists the physical tables the query would actually
    scan, with views expanded. That is the only way to catch an allowed view
    that selects from a forbidden dataset: the SQL text never names it, but the
    data would still land on screen.
    """
    references = getattr(job, "referenced_tables", None) or []
    if not references:
        return

    allowed = set(config.allowed_datasets(target))
    offending = sorted({
        reference.dataset_id.lower()
        for reference in references
        if reference.dataset_id.lower() not in allowed
    })
    if not offending:
        return

    resolved = ", ".join(
        f"{reference.dataset_id}.{reference.table_id}"
        for reference in references
    )
    raise ScopeError(
        f"This statement resolves to data in {', '.join(offending)}, which "
        f"{'is' if len(offending) == 1 else 'are'} outside the permitted scope.",
        detail=(
            f"Permitted datasets: {', '.join(sorted(allowed))}.\n\n"
            f"BigQuery reports that the query would physically read: "
            f"{resolved}.\n\n"
            f"This usually means a view inside a permitted dataset selects from "
            f"a dataset that is not permitted. The query was planned but never "
            f"executed, so nothing was billed and no rows were returned."
        ),
        sql=sql,
        offending=offending,
        allowed=sorted(allowed),
    )


# --------------------------------------------------------------------------
# client cache, one per (project, location)
# --------------------------------------------------------------------------

_clients: Dict[Tuple[str, str], Any] = {}
_client_lock = threading.Lock()

_BQ_SCOPES = [
    "https://www.googleapis.com/auth/bigquery",
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/drive",  # external tables over Sheets
]


def _import_bigquery():
    try:
        from google.cloud import bigquery  # noqa: WPS433
        return bigquery
    except ImportError as exc:  # pragma: no cover
        raise WarehouseError(
            "google-cloud-bigquery is not importable. It ships with "
            "dbt-bigquery, so this usually means the UI is running on a "
            "different interpreter than dbt.",
            detail=str(exc),
        ) from exc


def _credentials(cfg: Dict[str, Any]):
    """
    Build credentials the same way dbt-bigquery would for this target.

    oauth      -> Application Default Credentials (gcloud login)
    service-account / -json -> the keyfile named in the profile
    """
    method = str(cfg.get("method", "oauth")).lower()

    if method in ("service-account", "service_account"):
        from google.oauth2 import service_account
        keyfile = cfg.get("keyfile")
        if not keyfile:
            raise WarehouseError(
                "Profile uses method: service-account but no keyfile is set."
            )
        return service_account.Credentials.from_service_account_file(
            str(keyfile), scopes=_BQ_SCOPES
        )

    if method in ("service-account-json", "service_account_json"):
        from google.oauth2 import service_account
        info = cfg.get("keyfile_json")
        if not info:
            raise WarehouseError(
                "Profile uses method: service-account-json but no keyfile_json "
                "is set."
            )
        return service_account.Credentials.from_service_account_info(
            info, scopes=_BQ_SCOPES
        )

    import google.auth
    try:
        creds, _ = google.auth.default(scopes=_BQ_SCOPES)
    except Exception as exc:
        raise WarehouseError(
            "No Google credentials found. Run "
            "'gcloud auth application-default login' and try again.",
            detail=str(exc),
        ) from exc
    return _without_quota_project(creds)


# Set when an ADC quota project was dropped, so connection_check can explain it.
_dropped_quota_project: Optional[str] = None


def _without_quota_project(creds):
    """
    Drop any quota project that ADC supplied.

    A quota project makes the client send an x-goog-user-project header, and
    Google then requires serviceusage.services.use on *that* project. This UI
    only ever talks to the single project named in profiles.yml, and BigQuery
    already attributes the job to it, so a quota project cannot buy anything
    here - it can only add a permission requirement.

    That requirement is not hypothetical. 'gcloud auth application-default
    login' sets a quota project as a matter of course, and for anyone without
    roles/serviceusage.serviceUsageConsumer that is enough to turn every single
    query into a 403 whose message says nothing about quota projects.

    dbt-bigquery does not set one either, so dropping it here keeps the UI
    behaving like the CLI it wraps.
    """
    global _dropped_quota_project

    existing = getattr(creds, "quota_project_id", None)
    if not existing or not hasattr(creds, "with_quota_project"):
        return creds

    try:
        stripped = creds.with_quota_project(None)
    except Exception:
        # Not every credential type supports this. Better to try the query and
        # let it fail with a real error than to fail here.
        return creds

    _dropped_quota_project = str(existing)
    return stripped


def client_for(target: Optional[str] = None):
    """Cached BigQuery client for a dbt target."""
    cfg = config.target_config(target)
    if not cfg or not cfg.get("project"):
        raise WarehouseError(
            f"Target '{target or 'default'}' is not defined in profiles.yml, "
            f"or has no project set."
        )
    if str(cfg.get("type", "")).lower() != "bigquery":
        raise WarehouseError(
            f"Target '{cfg['_target_name']}' has type "
            f"'{cfg.get('type')}'. This UI implements the BigQuery adapter only."
        )

    project = str(cfg["project"])
    location = str(cfg.get("location") or "")
    key = (project, location)

    with _client_lock:
        existing = _clients.get(key)
        if existing is not None:
            return existing, cfg

        bigquery = _import_bigquery()
        try:
            created = bigquery.Client(
                project=project,
                credentials=_credentials(cfg),
                location=location or None,
            )
        except WarehouseError:
            raise
        except Exception as exc:
            raise WarehouseError(
                f"Could not open a BigQuery client for project '{project}'.",
                detail=str(exc),
            ) from exc

        _clients[key] = created
        return created, cfg


def reset_clients() -> None:
    with _client_lock:
        _clients.clear()


# --------------------------------------------------------------------------
# JSON-safe value coercion
# --------------------------------------------------------------------------

def _coerce(value: Any) -> Any:
    """Make a BigQuery cell safe for json.dumps without losing fidelity."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        # NaN / Infinity are not valid JSON.
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, decimal.Decimal):
        # NUMERIC as a string keeps every digit. Formatting is the UI's job.
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _coerce(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_coerce(v) for v in value]
    return str(value)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------

@dataclass
class QueryResult:
    columns: List[Dict[str, Any]] = field(default_factory=list)
    rows: List[List[Any]] = field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    bytes_processed: int = 0
    bytes_billed: int = 0
    cache_hit: bool = False
    duration_ms: int = 0
    job_id: str = ""
    location: str = ""
    target: str = ""
    executed_sql: str = ""
    dry_run: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "total_rows": self.total_rows,
            "truncated": self.truncated,
            "bytes_processed": self.bytes_processed,
            "bytes_billed": self.bytes_billed,
            "cache_hit": self.cache_hit,
            "duration_ms": self.duration_ms,
            "job_id": self.job_id,
            "location": self.location,
            "target": self.target,
            "executed_sql": self.executed_sql,
            "dry_run": self.dry_run,
            "column_count": len(self.columns),
        }


def _friendly_bq_error(exc: Exception, sql: str) -> WarehouseError:
    """Turn BigQuery's error text into something actionable."""
    text = str(exc)
    hint = ""

    if "was not found in location" in text or "Not found: Dataset" in text:
        hint = (
            "This is usually a region mismatch. Check that the target's "
            "location in profiles.yml matches the dataset's actual region."
        )
    elif "Query exceeded limit for bytes billed" in text:
        hint = (
            "The query was blocked by the UI's spend guard before it ran, so "
            "nothing was billed. Narrow the scan with a filter on the "
            "partition column, or raise DBT_UI_MAX_BYTES_BILLED."
        )
    elif "serviceusage.services.use" in text or "required permission to use project" in text:
        # This one reads like a data-access problem and is not one. It means the
        # credentials name a quota project the account may not consume, which is
        # what 'gcloud auth application-default set-quota-project' configures.
        hint = (
            "This is a quota-project problem, not a dataset-access problem. "
            "Your credentials name a quota project that this account lacks "
            "roles/serviceusage.serviceUsageConsumer on. Remove the "
            "'quota_project_id' line from "
            "%APPDATA%\\gcloud\\application_default_credentials.json and retry. "
            "Nothing needs that quota project: BigQuery bills the job to the "
            "project in the request."
        )
    elif "Permission denied" in text or "Access Denied" in text:
        hint = (
            "The signed-in account cannot read this relation. Confirm the "
            "dataset grant, or re-run "
            "'gcloud auth application-default login'."
        )
    elif "Unrecognized name" in text or "not found inside" in text:
        hint = "Check the column name against the Schema tab for this relation."
    elif "Syntax error" in text:
        hint = "GoogleSQL syntax error. The position BigQuery reports is below."

    return WarehouseError(
        text.strip().split("\n")[0][:400] or "BigQuery rejected the query.",
        detail=(text if not hint else f"{text}\n\nHint: {hint}"),
        sql=sql,
    )


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def dry_run(sql: str, target: Optional[str] = None) -> QueryResult:
    """
    Validate a statement and read back its result schema without running it.

    Free: BigQuery plans the query, reports the bytes it *would* scan and the
    exact output schema, and bills nothing. This powers both the "Validate"
    button and the column/data_type generator.
    """
    bigquery = _import_bigquery()

    # Refuse on the text before opening a connection.
    _assert_sql_in_scope(sql, target)

    client, cfg = client_for(target)

    started = datetime.datetime.now()
    job_config = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)

    try:
        job = client.query(sql, job_config=job_config)
    except Exception as exc:
        raise _friendly_bq_error(exc, sql) from exc

    # Now that BigQuery has resolved views, check what would really be read.
    _assert_referenced_in_scope(job, sql, target)

    elapsed = int((datetime.datetime.now() - started).total_seconds() * 1000)

    return QueryResult(
        columns=typing_map.schema_to_columns(job.schema),
        rows=[],
        total_rows=0,
        bytes_processed=int(job.total_bytes_processed or 0),
        bytes_billed=0,
        duration_ms=elapsed,
        job_id="(dry run)",
        location=str(cfg.get("location") or ""),
        target=str(cfg.get("_target_name") or ""),
        executed_sql=sql,
        dry_run=True,
    )


def execute(
    sql: str,
    target: Optional[str] = None,
    limit: Optional[int] = None,
    apply_limit: bool = True,
) -> QueryResult:
    """
    Run a statement and return rows plus the real result schema.

    `apply_limit` wraps the statement so a preview cannot pull an unbounded
    result into the browser. It is turned off for internal profiling queries
    that already aggregate down to a handful of rows.
    """
    bigquery = _import_bigquery()

    # Text check first: a refused statement never reaches BigQuery.
    _assert_sql_in_scope(sql, target)

    client, cfg = client_for(target)

    row_cap = min(
        int(limit or config.SETTINGS.preview_row_limit),
        config.SETTINGS.max_preview_row_limit,
    )

    # Plan the query before running it, so views are expanded and the physical
    # tables can be checked. A dry run is free, so this costs nothing and closes
    # the hole where an allowed view reads a forbidden dataset.
    try:
        plan = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
    except Exception as exc:
        raise _friendly_bq_error(exc, sql) from exc
    _assert_referenced_in_scope(plan, sql, target)

    job_config = bigquery.QueryJobConfig(
        maximum_bytes_billed=config.SETTINGS.max_bytes_billed,
        use_query_cache=True,
    )
    if config.SETTINGS.max_bytes_billed:
        job_config.maximum_bytes_billed = config.SETTINGS.max_bytes_billed

    started = datetime.datetime.now()
    try:
        job = client.query(sql, job_config=job_config)
        # Fetch one extra row so we can tell "exactly at the cap" from
        # "there was more".
        iterator = job.result(max_results=row_cap + 1 if apply_limit else None)
    except Exception as exc:
        raise _friendly_bq_error(exc, sql) from exc

    schema = list(iterator.schema or [])
    columns = typing_map.schema_to_columns(schema)
    names = [f.name for f in schema]

    rows: List[List[Any]] = []
    for record in iterator:
        if apply_limit and len(rows) >= row_cap:
            break
        rows.append([_coerce(record.get(name)) for name in names])

    elapsed = int((datetime.datetime.now() - started).total_seconds() * 1000)
    total_rows = int(iterator.total_rows or len(rows))

    return QueryResult(
        columns=columns,
        rows=rows,
        total_rows=total_rows,
        truncated=apply_limit and total_rows > len(rows),
        bytes_processed=int(job.total_bytes_processed or 0),
        bytes_billed=int(job.total_bytes_billed or 0),
        cache_hit=bool(job.cache_hit),
        duration_ms=elapsed,
        job_id=str(job.job_id or ""),
        location=str(cfg.get("location") or ""),
        target=str(cfg.get("_target_name") or ""),
        executed_sql=sql,
    )


# --------------------------------------------------------------------------
# metadata
# --------------------------------------------------------------------------

def _split_relation(relation: str) -> Tuple[str, str, str]:
    """`proj`.`dataset`.`table` (or unquoted) -> (proj, dataset, table)."""
    parts = [p.strip().strip("`") for p in relation.split(".")]
    if len(parts) != 3:
        raise WarehouseError(
            f"Expected a fully qualified project.dataset.table, got "
            f"'{relation}'."
        )
    return parts[0], parts[1], parts[2]


def describe_relation(relation: str, target: Optional[str] = None) -> Dict[str, Any]:
    """
    Authoritative column list and physical metadata for an existing relation.

    This reads the table definition rather than inferring from a query, so it
    also returns partitioning, clustering, row count and storage size.
    """
    _import_bigquery()
    project, dataset, table_name = _split_relation(relation)
    assert_dataset_allowed(dataset, target, context="reading a table definition")

    client, cfg = client_for(target)

    try:
        table = client.get_table(f"{project}.{dataset}.{table_name}")
    except Exception as exc:
        raise _friendly_bq_error(exc, f"-- describe {relation}") from exc

    partitioning = None
    if table.time_partitioning:
        partitioning = {
            "kind": "time",
            "field": table.time_partitioning.field or "_PARTITIONTIME",
            "granularity": (table.time_partitioning.type_ or "DAY").lower(),
            "require_filter": bool(table.require_partition_filter),
        }
    elif table.range_partitioning:
        rng = table.range_partitioning
        partitioning = {
            "kind": "range",
            "field": rng.field,
            "start": getattr(rng.range_, "start", None),
            "end": getattr(rng.range_, "end", None),
            "interval": getattr(rng.range_, "interval", None),
        }

    return {
        "relation": relation,
        "project": project,
        "dataset": dataset,
        "table": table_name,
        "table_type": (table.table_type or "TABLE"),
        "columns": typing_map.schema_to_columns(table.schema),
        "row_count": int(table.num_rows or 0),
        "size_bytes": int(table.num_bytes or 0),
        "created": table.created.isoformat() if table.created else None,
        "last_modified": table.modified.isoformat() if table.modified else None,
        "partitioning": partitioning,
        "clustering": list(table.clustering_fields or []),
        "description": table.description or "",
        "labels": dict(table.labels or {}),
        "location": str(cfg.get("location") or ""),
        "target": str(cfg.get("_target_name") or ""),
    }


def list_datasets(target: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Only the datasets in scope.

    Filtered rather than listed-then-marked: the browser never receives the
    names of the datasets it cannot read, so the warehouse browser cannot be
    used to enumerate the rest of the project.
    """
    _import_bigquery()
    client, cfg = client_for(target)
    allowed = set(config.allowed_datasets(target))

    try:
        datasets = list(client.list_datasets())
    except Exception as exc:
        raise _friendly_bq_error(exc, "-- list datasets") from exc

    return [
        {"dataset_id": ds.dataset_id, "project": ds.project,
         "full_id": f"{ds.project}.{ds.dataset_id}"}
        for ds in sorted(datasets, key=lambda d: d.dataset_id.lower())
        if ds.dataset_id.lower() in allowed
    ]


def list_tables(dataset: str, target: Optional[str] = None) -> List[Dict[str, Any]]:
    _import_bigquery()
    assert_dataset_allowed(dataset, target, context="listing tables")

    client, cfg = client_for(target)
    project = str(cfg.get("project"))
    try:
        tables = list(client.list_tables(f"{project}.{dataset}"))
    except Exception as exc:
        raise _friendly_bq_error(exc, f"-- list tables in {dataset}") from exc
    return [
        {
            "table_id": t.table_id,
            "table_type": (t.table_type or "TABLE"),
            "relation": f"`{project}`.`{dataset}`.`{t.table_id}`",
            "dataset": dataset,
        }
        for t in sorted(tables, key=lambda t: t.table_id.lower())
    ]


# --------------------------------------------------------------------------
# schema catalogue for autocomplete
# --------------------------------------------------------------------------

# dataset -> (fetched_at, payload). Cached because autocomplete asks for this
# on the first keystroke and a per-request INFORMATION_SCHEMA query would put a
# BigQuery round trip in the typing path.
_schema_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_schema_cache_lock = threading.Lock()
SCHEMA_CACHE_TTL = 600.0  # seconds


def dataset_schema(dataset: str, target: Optional[str] = None,
                   refresh: bool = False) -> Dict[str, Any]:
    """
    Every table and column in a dataset, from INFORMATION_SCHEMA.

    One query for the whole dataset rather than a metadata call per table: a
    dataset with 200 tables becomes a single request instead of 200, and the
    result feeds autocomplete for all of them.

    INFORMATION_SCHEMA.COLUMNS is authoritative for tables dbt does not manage,
    which is the case this exists to serve - the manifest only knows about
    models and declared sources.
    """
    assert_dataset_allowed(dataset, target, context="reading the column catalogue")

    key = f"{target or 'default'}::{dataset.lower()}"
    now = time.time()

    if not refresh:
        with _schema_cache_lock:
            cached = _schema_cache.get(key)
            if cached and (now - cached[0]) < SCHEMA_CACHE_TTL:
                return cached[1]

    _import_bigquery()
    client, cfg = client_for(target)
    project = str(cfg.get("project"))

    # ordinal_position keeps columns in their declared order, which is what a
    # reader expects. Views are included: they are queryable like tables.
    sql = f"""
select
  c.table_name,
  c.column_name,
  c.data_type,
  c.is_nullable,
  c.ordinal_position,
  t.table_type
from `{project}`.`{dataset}`.INFORMATION_SCHEMA.COLUMNS as c
left join `{project}`.`{dataset}`.INFORMATION_SCHEMA.TABLES as t
  using (table_name)
order by c.table_name, c.ordinal_position
"""

    try:
        result = execute(sql, target=target, apply_limit=False)
    except ScopeError:
        raise
    except WarehouseError as exc:
        # A dataset with no tables, or one the account cannot introspect, should
        # degrade to "no suggestions" rather than breaking the editor.
        payload = {
            "dataset": dataset,
            "tables": [],
            "error": exc.message,
            "fetched_at": now,
        }
        with _schema_cache_lock:
            _schema_cache[key] = (now, payload)
        return payload

    columns_by_table: Dict[str, Dict[str, Any]] = {}
    names = [c["name"] for c in result.columns]

    for row in result.rows:
        record = dict(zip(names, row))
        table_name = str(record.get("table_name") or "")
        if not table_name:
            continue

        entry = columns_by_table.setdefault(table_name, {
            "table": table_name,
            "table_type": (record.get("table_type") or "BASE TABLE"),
            "relation": f"`{project}`.`{dataset}`.`{table_name}`",
            "columns": [],
        })
        std = typing_map.standard_type(str(record.get("data_type") or ""))
        entry["columns"].append({
            "name": record.get("column_name"),
            "data_type": std,
            "data_type_yaml": std.lower(),
            "category": typing_map.category(std),
            "nullable": str(record.get("is_nullable") or "YES").upper() == "YES",
        })

    payload = {
        "dataset": dataset,
        "project": project,
        "tables": sorted(columns_by_table.values(), key=lambda t: t["table"].lower()),
        "table_count": len(columns_by_table),
        "column_count": sum(len(t["columns"]) for t in columns_by_table.values()),
        "bytes_processed": result.bytes_processed,
        "fetched_at": now,
        "error": None,
    }

    with _schema_cache_lock:
        _schema_cache[key] = (now, payload)
    return payload


def clear_schema_cache() -> int:
    with _schema_cache_lock:
        count = len(_schema_cache)
        _schema_cache.clear()
    return count


# --------------------------------------------------------------------------
# physical inventory: row counts, size, last modified
# --------------------------------------------------------------------------

_inventory_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_inventory_cache_lock = threading.Lock()
INVENTORY_CACHE_TTL = 300.0  # seconds

# __TABLES__.type is an integer code, not a name.
_TABLE_TYPE_CODES = {1: "TABLE", 2: "VIEW", 3: "EXTERNAL", 4: "MODEL", 5: "MATERIALIZED_VIEW"}


def inventory(target: Optional[str] = None,
              refresh: bool = False) -> Dict[str, Any]:
    """
    Row count, byte size and last-modified for every table in every in-scope
    dataset.

    Reads `<dataset>.__TABLES__`, which is a free metadata table: BigQuery bills
    zero bytes for it, verified against this project. That is why this does not
    use INFORMATION_SCHEMA.TABLE_STORAGE (region-scoped, so it would reach past
    the dataset allowlist) or a get_table() per table (one HTTP request each -
    46 requests for bronze_dbt alone).

    All in-scope datasets are UNION ALLed into a single job rather than one job
    per dataset, so the whole picker costs one round trip.

    Caveat that matters for the UI: a view reports row_count 0 and size_bytes 0.
    That is 'not applicable', not 'empty'. The row carries is_view so the
    frontend can say so instead of printing a misleading zero.
    """
    key = str(target or "default")
    now = time.time()

    if not refresh:
        with _inventory_cache_lock:
            cached = _inventory_cache.get(key)
            if cached and (now - cached[0]) < INVENTORY_CACHE_TTL:
                return cached[1]

    _import_bigquery()
    client, cfg = client_for(target)
    project = str(cfg.get("project"))

    # Only datasets that exist AND are permitted. A missing dataset in the
    # UNION would fail the entire job, and list_datasets already intersects
    # what is really there with the allowlist.
    datasets = [d["dataset_id"] for d in list_datasets(target)]

    if not datasets:
        payload = {
            "project": project,
            "datasets": [],
            "tables": [],
            "table_count": 0,
            "fetched_at": now,
            "error": None,
        }
        with _inventory_cache_lock:
            _inventory_cache[key] = (now, payload)
        return payload

    union = "\nunion all\n".join(
        f"select '{ds}' as dataset_id, table_id, row_count, size_bytes, "
        f"last_modified_time, type from `{project}.{ds}.__TABLES__`"
        for ds in datasets
    )
    sql = f"select * from (\n{union}\n) order by dataset_id, table_id"

    try:
        result = execute(sql, target=target, apply_limit=False)
    except ScopeError:
        raise
    except WarehouseError as exc:
        # Never let a metadata hiccup take out the page that uses this. An empty
        # inventory degrades to "counts unavailable", which is honest.
        payload = {
            "project": project,
            "datasets": datasets,
            "tables": [],
            "table_count": 0,
            "fetched_at": now,
            "error": exc.message,
        }
        with _inventory_cache_lock:
            _inventory_cache[key] = (now, payload)
        return payload

    names = [c["name"] for c in result.columns]
    tables: List[Dict[str, Any]] = []

    for row in result.rows:
        record = dict(zip(names, row))
        dataset_id = str(record.get("dataset_id") or "")
        table_id = str(record.get("table_id") or "")
        if not table_id:
            continue

        type_code = record.get("type")
        try:
            table_type = _TABLE_TYPE_CODES.get(int(type_code), "TABLE")
        except (TypeError, ValueError):
            table_type = "TABLE"
        is_view = table_type in ("VIEW", "MATERIALIZED_VIEW")

        # last_modified_time is epoch milliseconds. Kept as ms so the frontend
        # can hand it straight to new Date() without a unit conversion bug.
        modified = record.get("last_modified_time")
        try:
            modified_ms = int(modified) if modified is not None else None
        except (TypeError, ValueError):
            modified_ms = None

        def _int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        tables.append({
            "dataset": dataset_id,
            "table": table_id,
            "relation": f"`{project}`.`{dataset_id}`.`{table_id}`",
            "qualified": f"{dataset_id}.{table_id}",
            "table_type": table_type,
            "is_view": is_view,
            # Null rather than 0 for a view, so the UI cannot print "0 rows"
            # about something that has no row count.
            "row_count": None if is_view else _int(record.get("row_count")),
            "size_bytes": None if is_view else _int(record.get("size_bytes")),
            "last_modified": modified_ms,
        })

    payload = {
        "project": project,
        "datasets": datasets,
        "tables": tables,
        "table_count": len(tables),
        "bytes_processed": result.bytes_processed,
        "fetched_at": now,
        "error": None,
    }

    with _inventory_cache_lock:
        _inventory_cache[key] = (now, payload)
    return payload


def clear_inventory_cache() -> int:
    with _inventory_cache_lock:
        count = len(_inventory_cache)
        _inventory_cache.clear()
    return count


def connection_check(target: Optional[str] = None) -> Dict[str, Any]:
    """Cheap liveness probe used by the header status pill."""
    try:
        client, cfg = client_for(target)
        result = execute("select 1 as ok", target=target, limit=1)
        payload = {
            "ok": True,
            "target": str(cfg.get("_target_name")),
            "project": str(cfg.get("project")),
            "dataset": str(cfg.get("dataset")),
            "location": str(cfg.get("location")),
            "method": str(cfg.get("method")),
            "duration_ms": result.duration_ms,
        }
        # Worth saying out loud: the UI works, but the dbt CLI reads the same
        # ADC file and will still fail until the quota project is removed there.
        if _dropped_quota_project:
            payload["quota_project_dropped"] = _dropped_quota_project
            payload["quota_project_note"] = (
                f"Your Application Default Credentials name "
                f"'{_dropped_quota_project}' as a quota project. This UI ignores "
                f"it, because it forces a serviceusage.services.use permission "
                f"check that adds nothing here. The dbt CLI does not ignore it, "
                f"so 'dbt run' and 'dbt build' will fail with a 403 until you "
                f"remove it from "
                f"%APPDATA%\\gcloud\\application_default_credentials.json."
            )
        return payload
    except WarehouseError as exc:
        payload = exc.to_dict()
        payload["ok"] = False
        payload["target"] = target or config.default_target_name()
        return payload
    except Exception as exc:  # pragma: no cover
        return {
            "ok": False,
            "error": "Unexpected failure while testing the connection.",
            "detail": str(exc),
            "target": target or config.default_target_name(),
        }

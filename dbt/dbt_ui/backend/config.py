"""
Project discovery and runtime settings.

The UI lives at <project>/dbt_ui, so the dbt project root is simply the parent
of the parent of this file. Everything else is derived from dbt_project.yml and
profiles.yml so there is no second source of truth to keep in sync.
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

BACKEND_DIR = pathlib.Path(__file__).resolve().parent
UI_DIR = BACKEND_DIR.parent
PROJECT_DIR = UI_DIR.parent
FRONTEND_DIR = UI_DIR / "frontend"
RUNTIME_DIR = UI_DIR / ".runtime"

TARGET_DIR = PROJECT_DIR / "target"
MANIFEST_PATH = TARGET_DIR / "manifest.json"
RUN_RESULTS_PATH = TARGET_DIR / "run_results.json"
CATALOG_PATH = TARGET_DIR / "catalog.json"

DBT_PROJECT_PATH = PROJECT_DIR / "dbt_project.yml"
PROFILES_PATH = PROJECT_DIR / "profiles.yml"

MODELS_DIR = PROJECT_DIR / "models"
SEEDS_DIR = PROJECT_DIR / "seeds"
MACROS_DIR = PROJECT_DIR / "macros"

# Where the Silver Advisor writes generated models. Kept inside models/ so dbt
# picks them up, but under a name that makes their origin obvious in review.
GENERATED_SUBDIR = "_generated"


# --------------------------------------------------------------------------
# Medallion layers
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Layer:
    key: str
    label: str
    blurb: str
    order: int
    default_materialization: str


LAYERS: List[Layer] = [
    Layer(
        key="seed",
        label="Seeds",
        blurb="CSV files version-controlled alongside the code. Reference data "
              "and fixtures, not production feeds.",
        order=0,
        default_materialization="seed",
    ),
    Layer(
        key="bronze",
        label="Bronze",
        blurb="Raw landing zone. One row in, one row out. Light typing and "
              "audit columns only, no business logic.",
        order=1,
        default_materialization="table",
    ),
    Layer(
        key="silver",
        label="Silver",
        blurb="Cleaned and conformed. Deduplication, null handling, type "
              "discipline, derived categories and quality flags.",
        order=2,
        default_materialization="view",
    ),
    Layer(
        key="gold",
        label="Gold",
        blurb="Business-facing aggregates and facts. Grain is explicit and "
              "stable, measures are ready for BI.",
        order=3,
        default_materialization="table",
    ),
]

LAYER_BY_KEY: Dict[str, Layer] = {layer.key: layer for layer in LAYERS}
LAYER_ORDER: Dict[str, int] = {layer.key: layer.order for layer in LAYERS}


def layer_of(tags: Any, fqn: Any = None, resource_type: str = "model") -> str:
    """
    Work out which medallion layer a node belongs to.

    Tags win because dbt_project.yml assigns them per folder. The fqn path is
    the fallback for nodes configured some other way.
    """
    if resource_type == "seed":
        return "seed"

    tag_list = [str(t).lower() for t in (tags or [])]
    for layer in LAYERS:
        if layer.key in tag_list:
            return layer.key

    for part in [str(p).lower() for p in (fqn or [])]:
        if part in LAYER_BY_KEY:
            return part

    return "other"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Dataset scope
# --------------------------------------------------------------------------
# Hard boundary on what the UI may read, profile, preview or document.
#
# Two parts, because the medallion layers exist under different physical names
# depending on the target (see macros/generate_schema_name.sql):
#
#   BASE_ALLOWED_DATASETS  the production layer datasets, named exactly
#   ALLOWED_LAYERS         which layers are in scope; for a non-prod target the
#                          equivalent sandbox datasets are derived as
#                          <target.dataset>_<layer>
#
# So with the defaults below and target=dev, the allowed set resolves to
#   bronze_dbt, silver_dbt, dbt_dev_bronze, dbt_dev_silver
# and everything else in the project - gold, seeds, ASG_DATALAKE, the VS_*
# datasets, all 46 others - is refused.
#
# Override entirely with DBT_UI_ALLOWED_DATASETS as a comma separated list.

BASE_ALLOWED_DATASETS: List[str] = ["bronze_dbt", "silver_dbt"]
ALLOWED_LAYERS: List[str] = ["bronze", "silver"]

# Layers this UI may not BUILD, as distinct from may not read.
#
# The dataset allowlist above cannot police dbt itself: `dbt build` runs as a
# subprocess and issues its own SQL, which never passes through warehouse.py.
# So a second, independent control is needed - every dbt invocation from the UI
# is forced to exclude these layers by tag.
#
# The orchestrator running dbt from the command line is deliberately unaffected;
# production still needs to build gold. This only constrains the UI.
BLOCKED_BUILD_LAYERS: List[str] = ["gold"]


def blocked_build_layers() -> List[str]:
    override = os.environ.get("DBT_UI_BLOCKED_LAYERS")
    if override is not None:
        return [p.strip().lower() for p in override.split(",") if p.strip()]
    return [layer.lower() for layer in BLOCKED_BUILD_LAYERS]


def blocked_exclude_selectors() -> List[str]:
    """dbt selector strings that remove the blocked layers from any run."""
    return [f"tag:{layer}" for layer in blocked_build_layers()]


# --------------------------------------------------------------------------
# Runtime access settings
#
# The allowlist above is the built-in default. It can now also be managed from
# the Settings screen, which persists the choice to dbt_ui/.runtime/access.json
# so it survives a restart without anyone editing Python.
#
# Precedence, highest first:
#   1. DBT_UI_ALLOWED_DATASETS   env var, an operator override nobody in the UI
#                                can loosen
#   2. access.json               what the Settings screen saved
#   3. BASE_ALLOWED_DATASETS     the built-in default below
# --------------------------------------------------------------------------

ACCESS_FILE = "access.json"


def _access_path() -> pathlib.Path:
    return ensure_runtime_dir() / ACCESS_FILE


def read_access_settings() -> Dict[str, Any]:
    """The saved access settings, or an empty dict when nothing is saved."""
    path = RUNTIME_DIR / ACCESS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        # A corrupt settings file must not take the app down; fall back to the
        # built-in default, which is the safer, narrower list.
        return {}


def write_access_settings(datasets: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Persist the project-wide dataset allowlist. Only the keys passed change.

    A user's *role* does not live here - that is per-user in the users table.
    Per-role *permission overrides* (which permission each role carries) do live
    here, because they apply to the whole role, not one user. See
    read_role_overrides / write_role_permission below.
    """
    current = read_access_settings()
    current.pop("role", None)   # migrate away from the old single-role field

    if datasets is not None:
        cleaned = [str(name).strip().lower() for name in datasets if str(name).strip()]
        current["datasets"] = list(dict.fromkeys(cleaned))

    current["updated_at"] = time.time()
    _access_path().write_text(json.dumps(current, indent=2), encoding="utf-8")
    return current


# --------------------------------------------------------------------------
# Per-role permission overrides
#
# ROLES above is the built-in default matrix. The Settings screen can now flip
# individual cells, and those changes are stored here so they survive a restart
# and apply to every user holding that role. Overrides are merged *over* the
# defaults, so anything left untouched keeps its built-in value and a future
# change to the defaults still shows through for cells nobody overrode.
# --------------------------------------------------------------------------

# Permission keys that are never editable, with the value they are pinned to.
# can_login stays on for every role: a role that cannot log in is a foot-gun
# with no legitimate use here, and turning it off in the matrix would silently
# lock people out.
_PINNED_PERMISSIONS: Dict[str, bool] = {"can_login": True}


def read_role_overrides() -> Dict[str, Dict[str, bool]]:
    """Saved per-role permission overrides: {role: {perm_key: bool}}."""
    data = read_access_settings().get("role_overrides")
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, bool]] = {}
    for role, perms in data.items():
        if isinstance(perms, dict):
            out[str(role).lower()] = {
                str(k): bool(v) for k, v in perms.items()
            }
    return out


def write_role_permission(role: str, permission: str, value: bool) -> Dict[str, Any]:
    """
    Persist one role/permission override and return the resulting role matrix.

    Raises ValueError on an unknown role or permission, on an attempt to change
    a pinned permission, and on a change that would leave no role able to modify
    roles (which would make the matrix uneditable forever).
    """
    role = str(role).lower()
    permission = str(permission)

    if role not in ROLES:
        raise ValueError(f"Unknown role '{role}'.")
    valid_keys = {p["key"] for p in PERMISSIONS}
    if permission not in valid_keys:
        raise ValueError(f"Unknown permission '{permission}'.")
    if permission in _PINNED_PERMISSIONS:
        raise ValueError(
            f"'{permission}' cannot be changed; every role keeps it."
        )

    value = bool(value)

    # Guard: never let the last role that can modify roles lose that power, or
    # nobody could ever edit the matrix again.
    if permission == "can_modify_roles" and value is False:
        still_able = [
            r for r in ROLES
            if (role_permissions(r)["can_modify_roles"] if r != role else False)
        ]
        if not still_able:
            raise ValueError(
                "At least one role must keep 'Modify user roles', otherwise the "
                "matrix could never be edited again."
            )

    settings = read_access_settings()
    overrides = settings.get("role_overrides")
    if not isinstance(overrides, dict):
        overrides = {}
    role_map = overrides.get(role)
    if not isinstance(role_map, dict):
        role_map = {}
    role_map[permission] = value
    overrides[role] = role_map
    settings["role_overrides"] = overrides
    settings["updated_at"] = time.time()
    _access_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return role_catalogue()


# --------------------------------------------------------------------------
# Per-request user context
#
# warehouse.py enforces the dataset boundary deep inside its query paths, and
# threading a user object through every one of those calls would touch a lot of
# code for no benefit. The server is thread-per-request (ThreadingHTTPServer), so
# a thread-local holds the authenticated user for the life of one request and
# allowed_datasets() can consult it wherever it is called from.
#
# Set and cleared by the API layer around each request. If nothing is set - a CLI
# call, a test, a background job - the project-wide allowlist applies unchanged.
# --------------------------------------------------------------------------

_request_ctx = threading.local()


def set_request_user(user: Optional[Dict[str, Any]]) -> None:
    _request_ctx.user = user


def current_request_user() -> Optional[Dict[str, Any]]:
    return getattr(_request_ctx, "user", None)


def clear_request_user() -> None:
    _request_ctx.user = None


def allowed_datasets(target: Optional[str] = None) -> List[str]:
    """
    Resolve the dataset allowlist.

    The permitted set is the bronze and silver layers **across every target in
    the profile**, not just the selected one.

    That is deliberate. The requirement is "bronze and silver, nothing else",
    which is a statement about layers, not about environments. Scoping the list
    to the selected target caused a real false refusal: manifest.json bakes
    relation_name at parse time, so with the manifest parsed on dev and the
    dropdown on prod, ref() pointed at dbt_dev_silver while the allowlist only
    contained silver_dbt, and a perfectly legitimate query was rejected.

    Widening to every target keeps the layer boundary exactly as strict - gold,
    seeds and the other 46 datasets are still refused - while removing a failure
    mode that had nothing to do with the boundary being enforced.
    """
    names = _project_allowed_datasets()

    # Narrow to the signed-in user's grants, when they have any. Intersection
    # rather than replacement: a per-user grant can only ever restrict, never
    # widen. Nobody can grant themselves a dataset the project boundary excludes.
    user = current_request_user()
    grants = [
        str(name).strip().lower()
        for name in ((user or {}).get("datasets") or [])
        if str(name).strip()
    ]
    if grants:
        allowed = set(names)
        return [name for name in grants if name in allowed]

    return names


def _project_allowed_datasets() -> List[str]:
    """The project-wide allowlist, before any per-user narrowing."""
    override = os.environ.get("DBT_UI_ALLOWED_DATASETS")
    if override:
        names = [part.strip().lower() for part in override.split(",") if part.strip()]
        return list(dict.fromkeys(names))

    # What the Settings screen saved, if anything. This is an explicit choice by
    # the operator, so it replaces the built-in list outright rather than being
    # merged with it - otherwise unticking a default dataset could never take
    # effect.
    saved = read_access_settings().get("datasets")
    if isinstance(saved, list) and saved:
        return list(dict.fromkeys(str(n).strip().lower() for n in saved if str(n).strip()))

    names = [name.lower() for name in BASE_ALLOWED_DATASETS]

    # Every target's sandbox equivalent of the permitted layers.
    profiles = read_profiles()
    block = profiles.get(profile_name()) or {}
    for target_name, cfg in (block.get("outputs") or {}).items():
        dataset = str((cfg or {}).get("dataset") or "").strip().lower()
        if not dataset or str(target_name).lower() == "prod":
            continue
        for layer in ALLOWED_LAYERS:
            names.append(f"{dataset}_{layer}")

    return list(dict.fromkeys(names))


def dataset_allowed(dataset: str, target: Optional[str] = None) -> bool:
    return str(dataset or "").strip().lower() in set(allowed_datasets(target))


# --------------------------------------------------------------------------
# Roles
#
# The permission matrix, and nothing else. This module deliberately knows
# nothing about sessions or users: which role is active is decided by the
# authenticated user in the database (see auth.py), and the API layer resolves
# it per request. Keeping the matrix pure means there is exactly one place to
# read to know what a role may do.
#
# Note the shape of it. Manager is the privileged role, not Admin. Admin is a
# broad *read and inspect* role: it can see every screen, including the database
# configuration interfaces, but it cannot change tables, users, roles or
# configuration. Only Manager can. That is unusual enough to be worth stating
# out loud, because the names imply the opposite.
#
# These roles govern this application. They do not change what the underlying
# Google credentials can reach - BigQuery IAM decides that, and a Manager here
# cannot grant access the signed-in account does not already have.
# --------------------------------------------------------------------------

# The permissions the matrix is expressed in, in the order the UI shows them.
PERMISSIONS: List[Dict[str, str]] = [
    {"key": "can_login", "label": "Login"},
    {"key": "can_view_studio", "label": "View Data Studio"},
    {"key": "can_view_tables", "label": "View tables"},
    {"key": "can_read_data", "label": "Read data"},
    {"key": "can_view_config", "label": "View database configuration"},
    {"key": "can_write_files", "label": "Modify tables"},
    {"key": "can_manage_access", "label": "Manage user access"},
    {"key": "can_modify_roles", "label": "Modify user roles"},
    {"key": "can_configure", "label": "Configure database"},
    {"key": "can_modify_datasets", "label": "Modify datasets"},
    {"key": "can_run_dbt", "label": "Write/delete data (run dbt)"},
]

ROLES: Dict[str, Dict[str, Any]] = {
    "admin": {
        "label": "Admin",
        "blurb": "Sees everything, changes nothing. Full visibility across Data "
                 "Studio including the configuration screens, but no write, "
                 "user-management or configuration rights.",
        "can_login": True,
        "can_view_studio": True,
        "can_view_tables": True,
        "can_read_data": True,
        "can_view_config": True,
        "can_write_files": False,
        "can_manage_access": False,
        "can_modify_roles": False,
        "can_configure": False,
        "can_modify_datasets": False,
        "can_run_dbt": False,
    },
    "manager": {
        "label": "Manager",
        "blurb": "The privileged role. Modifies tables, manages users, roles and "
                 "dataset access, and configures the database.",
        "can_login": True,
        "can_view_studio": True,
        "can_view_tables": True,
        "can_read_data": True,
        "can_view_config": True,
        "can_write_files": True,
        "can_manage_access": True,
        "can_modify_roles": True,
        "can_configure": True,
        "can_modify_datasets": True,
        "can_run_dbt": True,
    },
    "analyst": {
        "label": "Analyst",
        "blurb": "Read only. Views the datasets and tables they have been granted, "
                 "their schemas and documentation, and queries data. No writes of "
                 "any kind.",
        "can_login": True,
        "can_view_studio": True,
        "can_view_tables": True,
        "can_read_data": True,
        "can_view_config": False,
        "can_write_files": False,
        "can_manage_access": False,
        "can_modify_roles": False,
        "can_configure": False,
        "can_modify_datasets": False,
        "can_run_dbt": False,
    },
}

# Used only when describing the matrix to an unauthenticated caller. Nothing is
# authorized against this - an unauthenticated request is refused outright.
FALLBACK_ROLE = "analyst"


def role_permissions(role: Optional[str] = None) -> Dict[str, Any]:
    """
    The permission set for a role name, with any saved overrides applied.

    Unknown or missing roles collapse to the least privileged entry rather than
    the most privileged one, so a typo or a corrupt row fails closed. Saved
    overrides from the Settings matrix are merged over the built-in defaults;
    pinned permissions (can_login) always win regardless of what was saved.
    """
    name = str(role or FALLBACK_ROLE).lower()
    resolved = name if name in ROLES else FALLBACK_ROLE
    meta = dict(ROLES[resolved])

    override = read_role_overrides().get(resolved) or {}
    valid_keys = {p["key"] for p in PERMISSIONS}
    for key, value in override.items():
        if key in valid_keys and key not in _PINNED_PERMISSIONS:
            meta[key] = bool(value)
    for key, value in _PINNED_PERMISSIONS.items():
        meta[key] = value

    meta["role"] = resolved
    return meta


def role_catalogue() -> Dict[str, Any]:
    """The whole matrix, for the UI to render and explain. Reflects overrides."""
    overrides = read_role_overrides()
    return {
        "permissions": PERMISSIONS,
        # Non-editable permission keys, so the UI can show them as locked.
        "pinned": list(_PINNED_PERMISSIONS.keys()),
        "roles": [
            {"key": key, **role_permissions(key)}
            for key in ROLES
        ],
        "customised": bool(overrides),
        "note": (
            "Manager is the privileged role. Admin has full visibility but no "
            "write, user-management or configuration rights. These roles govern "
            "this application only; BigQuery IAM still decides what the "
            "underlying credentials can reach."
        ),
    }


def scope_description(target: Optional[str] = None) -> Dict[str, Any]:
    """Everything the UI needs to explain the boundary to the user."""
    names = allowed_datasets(target)
    project_names = _project_allowed_datasets()
    blocked = blocked_build_layers()
    saved = read_access_settings().get("datasets")
    env_override = bool(os.environ.get("DBT_UI_ALLOWED_DATASETS"))
    user = current_request_user()
    user_grants = [n for n in ((user or {}).get("datasets") or [])]
    return {
        "allowed_datasets": names,
        "project_datasets": project_names,
        "user_restricted": bool(user_grants),
        "layers": [layer for layer in ALLOWED_LAYERS],
        "blocked_layers": blocked,
        "overridden": env_override,
        "source": (
            "user" if user_grants
            else "environment" if env_override
            else "settings" if isinstance(saved, list) and saved
            else "default"
        ),
        "env_locked": env_override,
        "summary": (
            f"Read access is limited to {len(names)} dataset"
            f"{'' if len(names) == 1 else 's'}: {', '.join(names)}."
        ),
        "build_summary": (
            f"dbt runs from this UI always exclude: "
            f"{', '.join('tag:' + layer for layer in blocked)}."
            if blocked else "dbt runs from this UI are unrestricted."
        ),
    }


@dataclass
class Settings:
    """Runtime configuration, overridable by environment variable."""

    host: str = "127.0.0.1"
    port: int = 8777

    # Row cap applied to every preview query the UI issues. Keeps ad-hoc
    # exploration from turning into an expensive scan.
    preview_row_limit: int = 200
    max_preview_row_limit: int = 5000

    # Hard ceiling on bytes a single UI query may bill. BigQuery rejects the job
    # rather than running it, so this is a real spend guard and not just advice.
    # 20 GiB.
    max_bytes_billed: int = 20 * 1024 * 1024 * 1024

    # Rows to sample when profiling a relation for silver recommendations.
    profile_sample_rows: int = 50_000

    dbt_command_timeout: int = 3600

    def __post_init__(self) -> None:
        self.host = os.environ.get("DBT_UI_HOST", self.host)
        self.port = int(os.environ.get("DBT_UI_PORT", self.port))
        if os.environ.get("DBT_UI_MAX_BYTES_BILLED"):
            self.max_bytes_billed = int(os.environ["DBT_UI_MAX_BYTES_BILLED"])


SETTINGS = Settings()


# --------------------------------------------------------------------------
# dbt_project.yml / profiles.yml readers
# --------------------------------------------------------------------------

def _read_yaml(path: pathlib.Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def read_dbt_project() -> Dict[str, Any]:
    return _read_yaml(DBT_PROJECT_PATH)


def project_name() -> str:
    return str(read_dbt_project().get("name") or "dbt_project")


def profile_name() -> str:
    return str(read_dbt_project().get("profile") or "default")


def read_profiles() -> Dict[str, Any]:
    """
    Read profiles.yml, preferring the copy inside the project.

    A project-local profiles.yml is the pattern that makes dbt Core shareable:
    one definition the whole team pulls from git, instead of every laptop
    hand-maintaining ~/.dbt/profiles.yml.
    """
    data = _read_yaml(PROFILES_PATH)
    if data:
        return data
    return _read_yaml(pathlib.Path.home() / ".dbt" / "profiles.yml")


def profiles_dir() -> str:
    """Directory to hand dbt via --profiles-dir."""
    if PROFILES_PATH.exists():
        return str(PROJECT_DIR)
    return str(pathlib.Path.home() / ".dbt")


@dataclass
class TargetInfo:
    name: str
    type: str = ""
    project: str = ""
    dataset: str = ""
    location: str = ""
    method: str = ""
    threads: int = 0
    priority: str = ""
    is_default: bool = False
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "project": self.project,
            "dataset": self.dataset,
            "location": self.location,
            "method": self.method,
            "threads": self.threads,
            "priority": self.priority,
            "is_default": self.is_default,
            "warnings": self.warnings,
        }


def list_targets(profile: Optional[str] = None) -> List[TargetInfo]:
    """Every target in the active profile, with sanity warnings attached."""
    profiles = read_profiles()
    name = profile or profile_name()
    block = profiles.get(name) or {}
    outputs = block.get("outputs") or {}
    default_target = block.get("target")

    # Regions present across the profile, used to flag cross-region targets.
    locations = {
        str((cfg or {}).get("location", "")).lower()
        for cfg in outputs.values()
        if (cfg or {}).get("location")
    }

    result: List[TargetInfo] = []
    for target_name, cfg in outputs.items():
        cfg = cfg or {}
        info = TargetInfo(
            name=target_name,
            type=str(cfg.get("type", "")),
            project=str(cfg.get("project", "")),
            dataset=str(cfg.get("dataset", "")),
            location=str(cfg.get("location", "")),
            method=str(cfg.get("method", "")),
            threads=int(cfg.get("threads", 0) or 0),
            priority=str(cfg.get("priority", "")),
            is_default=(target_name == default_target),
        )
        if info.name == "prod":
            info.warnings.append(
                "Writes production datasets. Intended for the orchestrator, "
                "not for interactive use."
            )
        if len(locations) > 1 and info.location.lower() != "":
            others = sorted(locations - {info.location.lower()})
            if others:
                info.warnings.append(
                    f"This profile mixes regions ({info.location} here, "
                    f"{', '.join(others)} elsewhere). BigQuery cannot join "
                    f"across regions."
                )
        result.append(info)

    result.sort(key=lambda t: (not t.is_default, t.name))
    return result


def default_target_name(profile: Optional[str] = None) -> str:
    profiles = read_profiles()
    block = profiles.get(profile or profile_name()) or {}
    target = block.get("target")
    if target:
        return str(target)
    outputs = list((block.get("outputs") or {}).keys())
    return outputs[0] if outputs else "dev"


def target_config(target: Optional[str] = None,
                  profile: Optional[str] = None) -> Dict[str, Any]:
    """Raw connection block for a target."""
    profiles = read_profiles()
    prof = profile or profile_name()
    block = profiles.get(prof) or {}
    outputs = block.get("outputs") or {}
    name = target or block.get("target") or (next(iter(outputs), None))
    cfg = dict(outputs.get(name) or {})
    cfg["_target_name"] = name
    cfg["_profile_name"] = prof
    return cfg


def ensure_runtime_dir() -> pathlib.Path:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    return RUNTIME_DIR

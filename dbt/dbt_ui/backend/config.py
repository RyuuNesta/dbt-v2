"""
Project discovery and runtime settings.

The UI lives at <project>/dbt_ui, so the dbt project root is simply the parent
of the parent of this file. Everything else is derived from dbt_project.yml and
profiles.yml so there is no second source of truth to keep in sync.
"""

from __future__ import annotations

import os
import pathlib
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
    override = os.environ.get("DBT_UI_ALLOWED_DATASETS")
    if override:
        names = [part.strip().lower() for part in override.split(",") if part.strip()]
        return list(dict.fromkeys(names))

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


def scope_description(target: Optional[str] = None) -> Dict[str, Any]:
    """Everything the UI needs to explain the boundary to the user."""
    names = allowed_datasets(target)
    blocked = blocked_build_layers()
    return {
        "allowed_datasets": names,
        "layers": [layer for layer in ALLOWED_LAYERS],
        "blocked_layers": blocked,
        "overridden": bool(os.environ.get("DBT_UI_ALLOWED_DATASETS")),
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

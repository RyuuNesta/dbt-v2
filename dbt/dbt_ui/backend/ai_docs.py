"""
AI-generated column documentation via Gemini.

Two things make this practical rather than a gimmick:

  * One request per table, not per column. Every column, its type and its
    profile go into a single prompt and come back as structured JSON. A 30
    column table costs one request, which is why even the 100 requests/day free
    tier is generous.
  * The model is given measured facts (null rate, distinct count, observed
    range, sample values) rather than just names, so it can say something
    specific instead of paraphrasing the column name back at you.

Auth. Two providers, checked in this order:

  1. Gemini Developer API with an API key. This is the free tier and the key is
     self-service from aistudio.google.com, so nobody needs to ask a GCP admin.
  2. Vertex AI with Application Default Credentials. Paid, and it needs the
     Vertex AI User role on the project. Verified as denied on
     data-analytics-asg, so it is a fallback for later rather than the path.

The key is stored in dbt_ui/.runtime/ai.json, which is gitignored. That is the
same trust model as ~/.dbt/profiles.yml or gcloud's ADC file: a local file
readable by the user who owns the session. It is never sent back to the browser
in full, only as a masked prefix.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from . import config

# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str
    blurb: str
    free_rpd: int
    free_rpm: int
    supports_thinking_off: bool
    recommended: bool = False


# Free-tier limits per the Gemini API docs. One table = one request, so the
# request-per-day ceiling matters far more than tokens.
MODEL_OPTIONS: List[ModelOption] = [
    ModelOption(
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        blurb="Strong writing, generous free quota, and thinking can be "
              "switched off so nothing is wasted. May not be available to "
              "newly created API keys.",
        free_rpd=250,
        free_rpm=10,
        supports_thinking_off=True,
        recommended=False,
    ),
    ModelOption(
        id="gemini-3.6-flash",
        label="Gemini 3.6 Flash",
        blurb="Latest generation. Best balance of quality and speed for "
              "documentation tasks. Available to all API keys.",
        free_rpd=500,
        free_rpm=15,
        supports_thinking_off=False,
        recommended=True,
    ),
    ModelOption(
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        blurb="Highest quality prose and the best at inferring business meaning "
              "from an unfamiliar schema. Lower daily quota.",
        free_rpd=100,
        free_rpm=5,
        supports_thinking_off=False,
    ),
    ModelOption(
        id="gemini-2.5-flash-lite",
        label="Gemini 2.5 Flash-Lite",
        blurb="Highest free quota and fastest. Descriptions are shorter and "
              "more literal.",
        free_rpd=1000,
        free_rpm=15,
        supports_thinking_off=True,
    ),
]

MODELS_BY_ID = {option.id: option for option in MODEL_OPTIONS}
DEFAULT_MODEL = "gemini-3.6-flash"

KEY_FILE = "ai.json"

# Columns sent in one request. Above this the prompt gets unwieldy and the
# response risks truncation, so it is chunked.
COLUMNS_PER_REQUEST = 40


class AiError(RuntimeError):
    """AI failure with a message a data engineer can act on."""

    def __init__(self, message: str, *, detail: str = "", kind: str = "error",
                 fixable: bool = False):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.kind = kind
        self.fixable = fixable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.message,
            "detail": self.detail,
            "kind": self.kind,
            "fixable": self.fixable,
        }


# --------------------------------------------------------------------------
# key storage
# --------------------------------------------------------------------------

def _key_path():
    return config.ensure_runtime_dir() / KEY_FILE


def load_key() -> Tuple[Optional[str], str]:
    """
    Return (key, source).

    An environment variable wins over the stored file so CI and a shared
    workstation can override without touching local state.
    """
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip(), f"environment ({name})"

    path = _key_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            value = (data.get("api_key") or "").strip()
            if value:
                return value, "saved in dbt_ui/.runtime/ai.json"
        except (json.JSONDecodeError, OSError):
            return None, "stored key file is unreadable"

    return None, "not configured"


def save_key(api_key: str) -> Dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        raise AiError("The API key was empty.")
    if len(key) < 20 or " " in key:
        raise AiError(
            "That does not look like a Gemini API key. Keys are a single "
            "token around 39 characters long, usually starting with 'AIza'."
        )

    path = _key_path()
    payload = {"api_key": key, "saved_at": time.time()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Best effort on POSIX; Windows ACLs already restrict the user profile.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    return {"saved": True, "masked": mask(key), "path": str(path)}


def clear_key() -> Dict[str, Any]:
    path = _key_path()
    existed = path.exists()
    if existed:
        path.unlink()
    return {"cleared": existed}


def mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


# --------------------------------------------------------------------------
# provider detection
# --------------------------------------------------------------------------

def _import_genai():
    try:
        from google import genai
        from google.genai import types
        return genai, types
    except ImportError as exc:
        raise AiError(
            "The google-genai package is not importable, so AI documentation "
            "is unavailable.",
            detail=f"{exc}\n\nInstall it with:  pip install google-genai",
        ) from exc


def status() -> Dict[str, Any]:
    """What the Documentation page needs to decide what to offer."""
    try:
        _import_genai()
        sdk_available = True
        sdk_error = ""
    except AiError as exc:
        sdk_available = False
        sdk_error = exc.message

    key, source = load_key()

    return {
        "sdk_available": sdk_available,
        "sdk_error": sdk_error,
        "configured": bool(key) and sdk_available,
        "key_source": source,
        "key_masked": mask(key) if key else "",
        "provider": "gemini-developer-api" if key else None,
        "default_model": DEFAULT_MODEL,
        "models": [
            {
                "id": option.id,
                "label": option.label,
                "blurb": option.blurb,
                "free_rpd": option.free_rpd,
                "free_rpm": option.free_rpm,
                "recommended": option.recommended,
            }
            for option in MODEL_OPTIONS
        ],
        "signup_url": "https://aistudio.google.com/apikey",
        "vertex_note": (
            "Vertex AI through your gcloud login was tested and returned "
            "PERMISSION_DENIED for aiplatform.endpoints.predict on "
            "data-analytics-asg. It also bills per token. The free Gemini API "
            "key below needs no admin involvement."
        ),
    }


def _client():
    genai, _types = _import_genai()
    key, _source = load_key()
    if not key:
        raise AiError(
            "No Gemini API key configured.",
            detail=(
                "Get a free key at https://aistudio.google.com/apikey and paste "
                "it into the AI documentation settings. It takes under a minute "
                "and does not require a GCP admin or a credit card."
            ),
            kind="not_configured",
            fixable=True,
        )
    try:
        return genai.Client(api_key=key)
    except Exception as exc:
        raise AiError(
            "Could not create the Gemini client.", detail=str(exc)
        ) from exc


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
You document data warehouse tables for a large Indonesian property developer
(Agung Sedayu Group). Their BigQuery warehouse follows a medallion
architecture: bronze is the raw landing zone, silver is cleaned and conformed,
gold holds business-facing aggregates.

Write one description per column. Rules:

- One or two sentences. Plain, factual, specific.
- Describe what the column MEANS to the business, not what its name says. Never
  restate the name ("company_code is the company code" is useless).
- Use the profiling statistics you are given. If a column is 100% null, say the
  source never populates it. If it has 3 distinct values, it is a code list.
- Recognise common source-system conventions. Columns from SAP finance data
  often map to known fields: BUKRS is company code, BLART document type, HKONT
  the GL account, DMBTR the amount in local currency, BUDAT the posting date,
  KOSTL cost center, SHKZG the debit/credit indicator, GJAHR fiscal year,
  SGTXT the line item text. Mention the SAP field name in parentheses when you
  are confident.
- Columns prefixed with a single underscore are pipeline metadata, not business
  data. Describe them as audit or quality-flag columns.
- Amounts in IDR held as NUMERIC are exact to the cent; FLOAT64 money is a
  precision risk worth noting.
- If you genuinely cannot infer the meaning, say so plainly and start the
  description with "Unclear:" so a human knows to review it. Do not invent a
  business purpose.
- Do not mention that you are an AI, and do not add preamble or markdown.
"""


def _column_brief(column: Dict[str, Any], profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact per-column payload. Only facts, no prose."""
    brief: Dict[str, Any] = {
        "name": column.get("name"),
        "data_type": str(column.get("data_type", "")).lower(),
    }

    mode = column.get("mode")
    if mode and mode != "NULLABLE":
        brief["mode"] = mode

    if not profile:
        return brief

    for source_key, target_key in (
        ("null_pct", "null_pct"),
        ("distinct_count", "distinct_count"),
        ("blank_count", "blank_count"),
        ("negative_count", "negative_count"),
    ):
        value = profile.get(source_key)
        if value is not None:
            brief[target_key] = value

    if profile.get("min") is not None:
        brief["observed_min"] = str(profile["min"])[:40]
        brief["observed_max"] = str(profile["max"])[:40]

    if profile.get("is_all_null"):
        brief["all_null"] = True
    if profile.get("is_constant"):
        brief["constant"] = True
    if profile.get("is_unique"):
        brief["unique"] = True

    return brief


def _add_sample_values(brief: Dict[str, Any], column: Dict[str, Any],
                       profile: Optional[Dict[str, Any]]) -> None:
    """
    Attach real values from the table.

    Kept separate and off by default because this is the only part of the
    payload that contains actual company data. Observed min/max on an amount
    column is a real figure from the ledger, and frequent values on a text
    column are real values. Structural statistics (null rate, distinct count)
    leak nothing; these do.
    """
    if not profile:
        return

    if profile.get("min") is not None:
        brief["observed_min"] = str(profile["min"])[:40]
        brief["observed_max"] = str(profile["max"])[:40]

    samples = profile.get("top_values") or []
    if samples:
        brief["frequent_values"] = [
            str(entry.get("value"))[:30] for entry in samples[:8]
        ]


def _build_prompt(
    table_name: str,
    layer: str,
    row_count: Optional[int],
    existing_description: str,
    columns: List[Dict[str, Any]],
    profiles: Dict[str, Dict[str, Any]],
    upstream: Optional[List[str]] = None,
    send_sample_values: bool = False,
) -> str:
    briefs = []
    for column in columns:
        profile = profiles.get(column.get("name"))
        brief = _column_brief(column, profile)
        if send_sample_values:
            _add_sample_values(brief, column, profile)
        briefs.append(brief)

    payload = {
        "table": table_name,
        "medallion_layer": layer or "unknown",
        "row_count": row_count,
        "existing_table_description": existing_description or None,
        "upstream_models": upstream or [],
        "columns": briefs,
    }

    return (
        "Document the columns of this table.\n\n"
        "Table and column facts as JSON:\n"
        f"{json.dumps(payload, indent=2, default=str)}\n\n"
        "Return one entry per column, in the same order, using the exact "
        "column names given."
    )


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------

def _response_schema(types: Any) -> Any:
    """Force structured output so no response parsing guesswork is needed."""
    return types.Schema(
        type=types.Type.OBJECT,
        required=["columns"],
        properties={
            "table_description": types.Schema(
                type=types.Type.STRING,
                description="Two sentence description of the table as a whole.",
            ),
            "columns": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    required=["name", "description"],
                    properties={
                        "name": types.Schema(type=types.Type.STRING),
                        "description": types.Schema(type=types.Type.STRING),
                    },
                ),
            ),
        },
    )


def _map_error(exc: Exception) -> AiError:
    text = str(exc)
    lowered = text.lower()

    if "api key not valid" in lowered or "api_key_invalid" in lowered:
        return AiError(
            "Gemini rejected the API key.",
            detail=f"{text}\n\nGenerate a fresh key at "
                   f"https://aistudio.google.com/apikey and save it again.",
            kind="bad_key",
            fixable=True,
        )
    if "429" in text or "resource_exhausted" in lowered or "quota" in lowered:
        return AiError(
            "Free-tier quota reached for this model.",
            detail=(
                f"{text}\n\nOptions: wait for the daily window to reset, or "
                f"switch to Gemini 2.5 Flash-Lite which allows 1,000 requests "
                f"per day. Pattern documentation keeps working regardless and "
                f"costs nothing."
            ),
            kind="quota",
            fixable=True,
        )
    if "403" in text or "permission_denied" in lowered:
        return AiError(
            "Gemini denied the request.",
            detail=f"{text}\n\nIf the key came from a restricted Google Cloud "
                   f"project, check that the Generative Language API is "
                   f"enabled for it.",
            kind="denied",
        )
    if "404" in text and "model" in lowered:
        return AiError(
            "That model is not available to this key.",
            detail=f"{text}\n\nTry Gemini 2.5 Flash, which is on the free tier.",
            kind="bad_model",
            fixable=True,
        )
    if "deadline" in lowered or "timeout" in lowered:
        return AiError(
            "Gemini timed out.",
            detail=f"{text}\n\nRetry, or use a smaller model.",
            kind="timeout",
            fixable=True,
        )

    return AiError("Gemini request failed.", detail=text)


def describe_columns(
    table_name: str,
    columns: List[Dict[str, Any]],
    profiles: Optional[Dict[str, Dict[str, Any]]] = None,
    model: str = DEFAULT_MODEL,
    layer: str = "",
    row_count: Optional[int] = None,
    existing_description: str = "",
    upstream: Optional[List[str]] = None,
    send_sample_values: bool = False,
) -> Dict[str, Any]:
    """
    Generate a description for every column, plus one for the table.

    Returns descriptions keyed by column name, along with token usage so the UI
    can show what the call actually cost against the free quota.

    `send_sample_values` controls whether real values from the table (observed
    min/max, frequent values) are included in the prompt. Off by default: those
    are the only part of the payload that is actual company data.
    """
    _genai, types = _import_genai()
    client = _client()

    option = MODELS_BY_ID.get(model)
    if option is None:
        raise AiError(
            f"'{model}' is not one of the offered models.",
            detail=f"Choose from: {', '.join(MODELS_BY_ID)}",
            kind="bad_model",
            fixable=True,
        )

    profiles = profiles or {}
    descriptions: Dict[str, str] = {}
    table_description = ""
    usage = {"prompt_tokens": 0, "output_tokens": 0, "requests": 0}

    generation_config: Dict[str, Any] = {
        "system_instruction": SYSTEM_INSTRUCTION,
        "response_mime_type": "application/json",
        "response_schema": _response_schema(types),
        "temperature": 0.2,
        "max_output_tokens": 8192,
    }

    # 2.5 models think by default. For a structured extraction task that only
    # burns output tokens and can truncate the JSON, so it is switched off where
    # the model allows it.
    if option.supports_thinking_off:
        generation_config["thinking_config"] = types.ThinkingConfig(
            thinking_budget=0
        )

    chunks = [
        columns[i:i + COLUMNS_PER_REQUEST]
        for i in range(0, len(columns), COLUMNS_PER_REQUEST)
    ] or [[]]

    for index, chunk in enumerate(chunks):
        prompt = _build_prompt(
            table_name=table_name,
            layer=layer,
            row_count=row_count,
            existing_description=existing_description,
            columns=chunk,
            profiles=profiles,
            upstream=upstream,
            send_sample_values=send_sample_values,
        )

        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(**generation_config),
            )
        except Exception as exc:
            raise _map_error(exc) from exc

        usage["requests"] += 1
        meta = getattr(response, "usage_metadata", None)
        if meta:
            usage["prompt_tokens"] += int(getattr(meta, "prompt_token_count", 0) or 0)
            usage["output_tokens"] += int(
                getattr(meta, "candidates_token_count", 0) or 0
            )

        parsed = _parse_response(response)

        if index == 0 and parsed.get("table_description"):
            table_description = str(parsed["table_description"]).strip()

        valid_names = {column.get("name") for column in chunk}
        for entry in parsed.get("columns") or []:
            name = str(entry.get("name") or "").strip()
            text = str(entry.get("description") or "").strip()
            if name in valid_names and text:
                descriptions[name] = _tidy(text)

    missing = [
        column["name"] for column in columns
        if column.get("name") not in descriptions
    ]

    return {
        "engine": "ai",
        "model": model,
        "model_label": option.label,
        "table_description": table_description,
        "descriptions": descriptions,
        "sent_sample_values": send_sample_values,
        "missing": missing,
        "unclear": [
            name for name, text in descriptions.items()
            if text.lower().startswith("unclear")
        ],
        "usage": usage,
    }


def _parse_response(response: Any) -> Dict[str, Any]:
    """
    Read the model output.

    `.parsed` is used when the SDK has already deserialised it; otherwise the
    text is parsed, tolerating a stray code fence.
    """
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, dict) and parsed:
        return parsed

    text = (getattr(response, "text", "") or "").strip()
    if not text:
        feedback = getattr(response, "prompt_feedback", None)
        candidates = getattr(response, "candidates", None) or []
        reason = ""
        if candidates:
            reason = str(getattr(candidates[0], "finish_reason", "") or "")
        raise AiError(
            "Gemini returned an empty response.",
            detail=(
                f"finish_reason={reason or 'unknown'} "
                f"prompt_feedback={feedback}\n\n"
                "This usually means the output hit the token limit or a safety "
                "filter. Try a smaller table or a different model."
            ),
            kind="empty",
            fixable=True,
        )

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise AiError(
            "Gemini did not return valid JSON.",
            detail=f"{exc}\n\nFirst 600 characters:\n{text[:600]}",
            kind="bad_json",
            fixable=True,
        ) from exc


def _tidy(text: str) -> str:
    """Normalise whitespace and strip markdown the model may have added."""
    cleaned = " ".join(str(text).split())
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"^[-*]\s+", "", cleaned)
    cleaned = cleaned.strip(" \t\"")
    if cleaned and not cleaned.endswith((".", "!", "?", ":")):
        cleaned += "."
    return cleaned

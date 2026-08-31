"""
Surgical description editing inside a dbt schema YAML file.

Why not just load and re-dump with PyYAML: a round trip through
`yaml.safe_load` + `yaml.dump` silently destroys everything PyYAML does not
model. In this project that means every explanatory comment, every folded
block scalar, the key order, and the quoting style. The schema files here are
heavily commented on purpose, so a lossy rewrite is not acceptable.

ruamel.yaml would round-trip comments, but it is not installable here.

So this patches the file as *text*, using PyYAML only to find out where things
are. `yaml.compose` gives a node tree whose marks carry line and column
numbers; from those we compute the exact line span each description occupies
and replace only those lines. Anything the UI does not touch is preserved
byte for byte.

One trap worth recording, because it is not obvious and it corrupts files:
for a folded scalar, `end_mark.line` points at the line of the *next key*,
not the last line of the block. Trusting it would delete the following key.
The span is therefore derived from indentation instead.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml


class PatchError(RuntimeError):
    """The file could not be read, or an edit target was not found."""


class ConflictError(RuntimeError):
    """The file changed on disk since the client last read it."""

    def __init__(self, message: str, *, disk_mtime: float,
                 expected_mtime: float, current: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.disk_mtime = disk_mtime
        self.expected_mtime = expected_mtime
        self.current = current or {}


# Wrap folded descriptions at this width. Matches what codegen.py emits so
# generated and hand-edited files look the same.
WRAP_WIDTH = 74

# Beyond this, a description is written as a folded block rather than inline.
INLINE_MAX = 70


# --------------------------------------------------------------------------
# locating things
# --------------------------------------------------------------------------

@dataclass
class Span:
    """A half-open line range [start, end) that a value occupies."""
    start: int
    end: int


@dataclass
class ColumnDoc:
    name: str
    data_type: Optional[str] = None
    description: str = ""
    has_description: bool = False
    key_column: int = 8
    desc_span: Optional[Span] = None
    insert_after: int = 0          # line to insert a new description after


@dataclass
class ModelDoc:
    name: str
    description: str = ""
    has_description: bool = False
    key_column: int = 4
    desc_span: Optional[Span] = None
    insert_after: int = 0
    columns: List[ColumnDoc] = field(default_factory=list)


def _mapping_get(node: Any, key: str) -> Tuple[Any, Any]:
    """Find (key_node, value_node) in a MappingNode, or (None, None)."""
    if not isinstance(node, yaml.MappingNode):
        return None, None
    for key_node, value_node in node.value:
        if getattr(key_node, "value", None) == key:
            return key_node, value_node
    return None, None


def _value_span(lines: List[str], key_node: Any) -> Span:
    """
    The full line range a key and its value occupy.

    Derived from indentation rather than from end_mark, because end_mark on a
    folded scalar points at the following key's line. A continuation line is
    one that is blank, or indented deeper than the key itself.
    """
    start = key_node.start_mark.line
    key_column = key_node.start_mark.column
    end = start + 1

    while end < len(lines):
        line = lines[end]
        if not line.strip():
            end += 1
            continue
        indent = len(line) - len(line.lstrip())
        if indent > key_column:
            end += 1
            continue
        break

    # Do not swallow the blank lines that separate this key from the next; they
    # belong to the file's spacing, not to the value.
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1

    return Span(start, end)


def read_doc(path: pathlib.Path, model_name: Optional[str] = None) -> List[ModelDoc]:
    """
    Read every model in a schema file, with the line span of each description.

    `model_name` narrows the result to one model without changing the spans,
    which are always relative to the whole file.
    """
    if not path.exists():
        raise PatchError(f"{path} does not exist.")

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    try:
        with path.open(encoding="utf-8") as handle:
            root = yaml.compose(handle)
    except yaml.YAMLError as exc:
        raise PatchError(f"{path.name} is not valid YAML: {exc}") from exc

    if root is None:
        raise PatchError(f"{path.name} is empty.")

    out: List[ModelDoc] = []

    # dbt allows models:, seeds:, sources: and snapshots: in one file. Only the
    # first three carry per-column descriptions we can edit here.
    for section in ("models", "seeds", "snapshots"):
        _, sequence = _mapping_get(root, section)
        if not isinstance(sequence, yaml.SequenceNode):
            continue

        for entry in sequence.value:
            name_key, name_value = _mapping_get(entry, "name")
            if name_value is None:
                continue
            if model_name and name_value.value != model_name:
                continue

            doc = ModelDoc(name=str(name_value.value),
                           key_column=name_key.start_mark.column)

            desc_key, desc_value = _mapping_get(entry, "description")
            if desc_key is not None:
                doc.description = str(desc_value.value or "")
                doc.has_description = bool(doc.description.strip())
                doc.key_column = desc_key.start_mark.column
                doc.desc_span = _value_span(lines, desc_key)
                doc.insert_after = doc.desc_span.start
            else:
                doc.insert_after = name_key.start_mark.line

            _, columns = _mapping_get(entry, "columns")
            if isinstance(columns, yaml.SequenceNode):
                for column in columns.value:
                    col_name_key, col_name_value = _mapping_get(column, "name")
                    if col_name_value is None:
                        continue

                    col = ColumnDoc(name=str(col_name_value.value),
                                    key_column=col_name_key.start_mark.column)

                    _, type_value = _mapping_get(column, "data_type")
                    type_key, _ = _mapping_get(column, "data_type")
                    if type_value is not None:
                        col.data_type = str(type_value.value or "")

                    col_desc_key, col_desc_value = _mapping_get(column, "description")
                    if col_desc_key is not None:
                        col.description = str(col_desc_value.value or "")
                        col.has_description = bool(col.description.strip())
                        col.key_column = col_desc_key.start_mark.column
                        col.desc_span = _value_span(lines, col_desc_key)
                        col.insert_after = col.desc_span.start
                    else:
                        # Put a new description after data_type when present,
                        # otherwise directly after the name, matching the
                        # ordering the generator uses.
                        anchor = type_key or col_name_key
                        col.insert_after = anchor.start_mark.line
                        col.key_column = anchor.start_mark.column

                    doc.columns.append(col)

            out.append(doc)

    if model_name and not out:
        raise PatchError(f"No model named '{model_name}' in {path.name}.")

    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_description(text: str, indent: int) -> List[str]:
    """
    Render a description as YAML lines at the given indentation.

    Short, punctuation-free text goes inline. Anything longer, or containing a
    character that would need quoting, becomes a folded block, which avoids the
    quoting pitfalls around ':' and '#' entirely.
    """
    words = " ".join(str(text).split())
    pad = " " * indent

    if not words:
        return [f"{pad}description: ''"]

    simple = (
        len(words) <= INLINE_MAX
        and ":" not in words
        and "#" not in words
        and not words.startswith(("'", '"', "*", "&", "?", "-", "[", "{"))
    )
    if simple:
        return [f"{pad}description: {words}"]

    out = [f"{pad}description: >"]
    body_pad = " " * (indent + 2)
    current = ""
    for word in words.split(" "):
        if current and len(current) + len(word) + 1 > WRAP_WIDTH:
            out.append(f"{body_pad}{current}")
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(f"{body_pad}{current}")
    return out


# --------------------------------------------------------------------------
# patching
# --------------------------------------------------------------------------

def patch_descriptions(
    path: pathlib.Path,
    model_name: str,
    model_description: Optional[str] = None,
    column_descriptions: Optional[Dict[str, str]] = None,
    expected_mtime: Optional[float] = None,
    allow_clearing: bool = False,
) -> Dict[str, Any]:
    """
    Replace descriptions in place, preserving the rest of the file exactly.

    `expected_mtime` guards against clobbering an external change: pass the
    mtime the client last read and the write is refused if the file has moved
    on since.

    `allow_clearing` must be set to blank out a description that currently has
    content, so an accidental empty edit cannot silently delete prose.
    """
    column_descriptions = column_descriptions or {}

    if not path.exists():
        raise PatchError(f"{path} does not exist.")

    disk_mtime = path.stat().st_mtime
    if expected_mtime is not None and abs(disk_mtime - expected_mtime) > 0.001:
        docs = read_doc(path, model_name)
        current = docs[0] if docs else None
        raise ConflictError(
            f"{path.name} changed on disk since you loaded it.",
            disk_mtime=disk_mtime,
            expected_mtime=expected_mtime,
            current={
                "model_description": current.description if current else "",
                "columns": {c.name: c.description for c in current.columns}
                if current else {},
            },
        )

    original = path.read_text(encoding="utf-8")
    lines = original.splitlines()
    newline = "\r\n" if "\r\n" in original else "\n"

    docs = read_doc(path, model_name)
    doc = docs[0]

    # Collect edits as (start, end, replacement_lines), then apply from the
    # bottom of the file upward so earlier line numbers stay valid.
    edits: List[Tuple[int, int, List[str]]] = []
    applied: List[str] = []
    rejected: List[Dict[str, str]] = []

    def plan(target_name: str, existing_has: bool, existing_text: str,
             new_text: str, span: Optional[Span], insert_after: int,
             indent: int) -> None:
        cleaned = " ".join(str(new_text).split())

        if cleaned == " ".join(str(existing_text).split()):
            return  # unchanged, nothing to write

        if not cleaned and existing_has and not allow_clearing:
            rejected.append({
                "target": target_name,
                "reason": "Refusing to clear a description that already had "
                          "content. Send allow_clearing to override.",
            })
            return

        rendered = render_description(cleaned, indent)
        if span is not None:
            edits.append((span.start, span.end, rendered))
        else:
            edits.append((insert_after + 1, insert_after + 1, rendered))
        applied.append(target_name)

    if model_description is not None:
        plan(
            f"model:{doc.name}",
            doc.has_description,
            doc.description,
            model_description,
            doc.desc_span,
            doc.insert_after,
            doc.key_column,
        )

    by_name = {col.name: col for col in doc.columns}
    for column_name, new_text in column_descriptions.items():
        col = by_name.get(column_name)
        if col is None:
            rejected.append({
                "target": column_name,
                "reason": f"'{column_name}' is not a documented column of "
                          f"{doc.name}. Regenerate the schema first.",
            })
            continue
        plan(
            column_name,
            col.has_description,
            col.description,
            new_text,
            col.desc_span,
            col.insert_after,
            col.key_column,
        )

    if not edits:
        return {
            "written": False,
            "path": str(path),
            "mtime": disk_mtime,
            "applied": [],
            "rejected": rejected,
        }

    for start, end, replacement in sorted(edits, key=lambda e: -e[0]):
        lines[start:end] = replacement

    patched = newline.join(lines) + newline

    # Refuse to write anything that is no longer parseable, and refuse to write
    # a file whose meaning changed beyond the descriptions we intended.
    try:
        reparsed = yaml.safe_load(patched)
    except yaml.YAMLError as exc:
        raise PatchError(
            f"The edit would have made {path.name} invalid YAML, so nothing "
            f"was written. {exc}"
        ) from exc

    _verify_only_descriptions_changed(original, patched, path.name)

    backup = path.with_suffix(path.suffix + ".bak")
    backup.write_text(original, encoding="utf-8", newline="")
    path.write_text(patched, encoding="utf-8", newline="")

    return {
        "written": True,
        "path": str(path),
        "mtime": path.stat().st_mtime,
        "applied": applied,
        "rejected": rejected,
        "backup": str(backup),
        "bytes": path.stat().st_size,
    }


def _strip_descriptions(tree: Any) -> Any:
    """Recursively drop every `description` key, for comparison purposes."""
    if isinstance(tree, dict):
        return {
            key: _strip_descriptions(value)
            for key, value in tree.items()
            if key != "description"
        }
    if isinstance(tree, list):
        return [_strip_descriptions(item) for item in tree]
    return tree


def _verify_only_descriptions_changed(before: str, after: str, name: str) -> None:
    """
    Structural guard.

    Compares both versions with every description removed. If anything else
    differs, the line arithmetic went wrong and we must not write the file.
    Cheap insurance against silently corrupting tests or config.
    """
    try:
        old_tree = _strip_descriptions(yaml.safe_load(before))
        new_tree = _strip_descriptions(yaml.safe_load(after))
    except yaml.YAMLError as exc:
        raise PatchError(f"Could not verify the edit to {name}: {exc}") from exc

    if old_tree != new_tree:
        raise PatchError(
            f"The edit would have changed more than descriptions in {name}, "
            f"so nothing was written. This is a bug in the patcher; please "
            f"edit the file directly."
        )


# --------------------------------------------------------------------------
# export helpers
# --------------------------------------------------------------------------

def as_dict(path: pathlib.Path, model_name: Optional[str] = None) -> Dict[str, Any]:
    """Structured view of a schema file, for the JSON export."""
    docs = read_doc(path, model_name)
    return {
        "path": str(path),
        "mtime": path.stat().st_mtime,
        "models": [
            {
                "name": doc.name,
                "description": doc.description,
                "has_description": doc.has_description,
                "columns": [
                    {
                        "name": col.name,
                        "data_type": col.data_type,
                        "description": col.description,
                        "has_description": col.has_description,
                    }
                    for col in doc.columns
                ],
            }
            for doc in docs
        ],
    }

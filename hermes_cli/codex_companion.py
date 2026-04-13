"""Workspace review/store helpers.

MVP scope:
- Poll a workspace for text-file changes
- Batch nearby saves into a single change-set
- Persist diff events and command outputs under ~/.hermes/store/
- Optionally send each change-set to Hermes for natural-language analysis
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid

import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from hermes_cli.config import ensure_hermes_home, get_config_path, get_hermes_home, load_config
from hermes_cli.models import fetch_api_models
from hermes_cli.runtime_provider import (
    format_runtime_provider_error,
    resolve_runtime_provider,
)

FLOW_CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go", ".rs", ".java", ".kt", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".cs", ".rb", ".php", ".sh", ".bash", ".zsh"}
FLOW_LOW_SIGNAL_DIRS = {"test", "tests", "testing", "spec", "specs", "docs", "doc", "assets", "static", "public", "images", "img", "fixtures", "examples", "example", "samples", "vendor", "third_party", "dist", "build"}
FLOW_ENTRY_HINTS = ("main", "app", "server", "run", "cli", "index", "train", "api", "manage", "worker", "daemon", "bootstrap", "launch", "start", "serve", "web", "chat")
FLOW_SYMBOL_PREFERENCES = ("main", "run", "start", "serve", "train", "launch", "bootstrap", "execute", "cli", "app", "create_app")


DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".hermes",
}

DEFAULT_IGNORE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".so",
    ".dylib",
    ".dll",
    ".class",
    ".o",
    ".a",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
}

DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_MAX_CHANGES_PER_EVENT = 8
DEFAULT_MAX_DIFF_CHARS = 12_000
DEFAULT_MAX_RELATED_FILES = 6
DEFAULT_MAX_RELATED_CHARS = 16_000
DEFAULT_SYMBOL_MATCHES = 5
STORE_SCHEMA_VERSION = 3
SYNC_COMMANDS = ("flow", "explain", "review", "diff")
DEFAULT_STARTUP_FILE_TARGETS = 6
DEFAULT_STARTUP_DIR_TARGETS = 4
DEFAULT_STARTUP_FLOW_TARGETS = 4
DEFAULT_INCREMENTAL_FILE_TARGETS = 4
DEFAULT_INCREMENTAL_FLOW_TARGETS = 3

_LANGUAGE_SPECS = {
    "en": {
        "name": "English",
        "file_sections": [
            "## Overview",
            "## Key Functions and Responsibilities",
            "## Control Flow",
            "## Improvement Opportunities",
        ],
        "directory_sections": [
            "## Overview",
            "## Key Files and Responsibilities",
            "## Control Flow",
            "## Improvement Opportunities",
        ],
        "review_sections": [
            "## Change Summary",
            "## Control Flow",
            "## Risks",
            "## Improvement Suggestions",
        ],
        "review_guidance": {
            "summary": "In Change Summary, describe processing flow, major functions, and responsibility changes.",
            "flow": "In Control Flow, explain the call flow and data flow in the changed code.",
            "risks": "In Risks, call out likely bugs, regressions, and missing edge cases.",
            "improvements": "In Improvement Suggestions, suggest concrete follow-up improvements or tests.",
        },
    },
    "ja": {
        "name": "Japanese",
        "file_sections": [
            "## 概要",
            "## 主要な関数と責務",
            "## 処理フロー",
            "## 改善ポイント",
        ],
        "directory_sections": [
            "## 概要",
            "## 主要なファイルと責務",
            "## 処理フロー",
            "## 改善ポイント",
        ],
        "review_sections": [
            "## 変更説明",
            "## 処理フロー",
            "## リスク",
            "## 改善提案",
        ],
        "review_guidance": {
            "summary": "In 変更説明, describe processing flow, major functions, and responsibility changes.",
            "flow": "In 処理フロー, explain the call flow and data flow in the changed code.",
            "risks": "In リスク, call out likely bugs, regressions, and missing edge cases.",
            "improvements": "In 改善提案, suggest concrete follow-up improvements or tests.",
        },
    },
}


def _normalize_natural_language(language: Optional[str]) -> str:
    value = (language or "").strip().lower()
    aliases = {"english": "en", "jp": "ja", "japanese": "ja"}
    normalized = aliases.get(value, value)
    return normalized if normalized in _LANGUAGE_SPECS else "en"


def _language_spec(language: Optional[str]) -> dict[str, Any]:
    return _LANGUAGE_SPECS[_normalize_natural_language(language)]


def _normalize_sync_kind(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"startup", "incremental"} else "incremental"


def _safe_relpath(path: Path, root: Path) -> Optional[str]:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except Exception:
        return None


@dataclass(frozen=True)
class FileSnapshot:
    rel_path: str
    mtime_ns: int
    size: int
    digest: str
    content: str


@dataclass
class PendingChange:
    path: str
    change_type: str
    old_content: str
    new_content: str
    first_seen_at: float
    updated_at: float

    def refresh(self, *, change_type: str, new_content: str, updated_at: float) -> None:
        self.change_type = change_type
        self.new_content = new_content
        self.updated_at = updated_at


def _text_lines(text: str) -> list[str]:
    return text.splitlines()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "output"


def _is_likely_binary(data: bytes) -> bool:
    return b"\x00" in data


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_text_file(path: Path, max_file_bytes: int) -> Optional[str]:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > max_file_bytes or _is_likely_binary(raw):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None


def should_ignore_path(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in DEFAULT_IGNORE_DIRS for part in rel_parts[:-1]):
        return True
    return path.suffix.lower() in DEFAULT_IGNORE_SUFFIXES


def _normalize_ignore_globs(ignore_globs: Optional[Sequence[str]]) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in ignore_globs or ():
        text = str(item).strip()
        if not text:
            continue
        normalized.append(text.replace("\\", "/"))
    return tuple(normalized)


def _matches_ignore_glob(rel_path: str, ignore_globs: Sequence[str]) -> bool:
    if not ignore_globs:
        return False
    rel = rel_path.replace("\\", "/").lstrip("./")
    path_obj = Path(rel)
    candidates = {rel}
    for parent in path_obj.parents:
        parent_str = str(parent).replace("\\", "/")
        if parent_str and parent_str != ".":
            candidates.add(parent_str)
            candidates.add(f"{parent_str}/")
    for candidate in list(candidates):
        candidates.add(f"./{candidate}")
    for pattern in ignore_globs:
        normalized_pattern = pattern.replace("\\", "/").strip()
        if not normalized_pattern:
            continue
        for candidate in candidates:
            if fnmatch.fnmatchcase(candidate, normalized_pattern):
                return True
    return False


def should_ignore_review_path(path: Path, root: Path, ignore_globs: Optional[Sequence[str]] = None) -> bool:
    if should_ignore_path(path, root):
        return True
    try:
        rel_path = str(path.relative_to(root))
    except ValueError:
        return True
    return _matches_ignore_glob(rel_path, _normalize_ignore_globs(ignore_globs))


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[truncated]..."


def _split_markdown_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current = ""
    sections[current] = []
    for line in text.splitlines():
        if line.startswith("## "):
            current = line.strip()
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {
        key: "\n".join(lines).strip()
        for key, lines in sections.items()
    }


def _iter_candidate_lines(section_text: str) -> list[str]:
    candidates: list[str] = []
    for raw_line in section_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("### ", "## ")):
            continue
        if re.match(r"^[-*]\s+", line):
            line = re.sub(r"^[-*]\s+", "", line).strip()
        elif re.match(r"^\d+\.\s+", line):
            line = re.sub(r"^\d+\.\s+", "", line).strip()
        elif len(line) < 40:
            continue
        candidates.append(line)
    return candidates


def _candidate_target(line: str) -> tuple[str, str, float]:
    normalized = line.lower()
    skill_markers = (
        "workflow", "playbook", "checklist", "step", "steps", "procedure",
        "template", "pattern", "test", "verification", "guard", "automation",
        "runbook", "repeatable",
    )
    memory_markers = (
        "avoid", "be careful", "watch for", "pitfall", "risk", "remember",
        "convention", "assumption", "invariant", "regression",
    )
    if any(marker in normalized for marker in skill_markers):
        return "skill", "skill", 0.84
    if any(marker in normalized for marker in memory_markers):
        return "memory", "memory", 0.78
    return "memory", "memory", 0.68


def _candidate_title(command_name: str, target: str, summary: str) -> str:
    prefix = {
        "review": "Diff review",
        "explain": "Explain",
        "flow": "Flow",
    }.get(command_name, "Analysis")
    noun = "workflow" if target == "skill" else "note"
    words = re.sub(r"\s+", " ", summary).strip().split(" ")
    return f"{prefix} {noun}: {' '.join(words[:6]).strip()}"


def extract_promotion_candidates(
    analysis_text: str,
    *,
    command_name: str,
    metadata: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
) -> list[dict[str, Any]]:
    if not analysis_text.strip():
        return []

    spec = _language_spec(natural_language)
    sections = _split_markdown_sections(analysis_text)
    relevant_sections: list[tuple[str, str]] = []

    if command_name == "review":
        relevant_sections.extend(
            [
                (spec["review_sections"][2], "risk"),
                (spec["review_sections"][3], "improvement"),
            ]
        )
    else:
        relevant_sections.append((spec["file_sections"][3], "improvement"))
        relevant_sections.append((spec["directory_sections"][3], "improvement"))

    source_paths = []
    if metadata:
        if metadata.get("target_path"):
            source_paths.append(str(metadata["target_path"]))
        for item in metadata.get("related_files", []) or []:
            path = item.get("path")
            if path and path not in source_paths:
                source_paths.append(path)

    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []
    for section_name, section_kind in relevant_sections:
        section_text = sections.get(section_name, "")
        for line in _iter_candidate_lines(section_text):
            candidate_type, suggested_target, confidence = _candidate_target(line)
            normalized = re.sub(r"\W+", " ", line.lower()).strip()
            dedupe_key = (candidate_type, normalized)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            candidates.append(
                {
                    "type": candidate_type,
                    "title": _candidate_title(command_name, candidate_type, line),
                    "summary": line[:280],
                    "confidence": confidence,
                    "source_paths": source_paths[:6],
                    "suggested_target": suggested_target,
                    "source_command": command_name,
                    "section": section_kind,
                }
            )

    if candidates:
        return candidates[:5]

    fallback_lines = _iter_candidate_lines(analysis_text)
    for line in fallback_lines:
        if "should" not in line.lower() and "consider" not in line.lower():
            continue
        candidate_type, suggested_target, confidence = _candidate_target(line)
        return [{
            "type": candidate_type,
            "title": _candidate_title(command_name, candidate_type, line),
            "summary": line[:280],
            "confidence": max(0.65, confidence - 0.08),
            "source_paths": source_paths[:6],
            "suggested_target": suggested_target,
            "source_command": command_name,
            "section": "fallback",
        }]
    return []


def _read_workspace_file(root: Path, rel_path: str, *, max_file_bytes: int) -> Optional[str]:
    path = (root / rel_path).resolve()
    try:
        if not path.is_file() or should_ignore_path(path, root):
            return None
        return _load_text_file(path, max_file_bytes=max_file_bytes)
    except Exception:
        return None


def _candidate_import_paths(module: str, *, source_path: str, workspace_root: Path) -> list[str]:
    source_parts = Path(source_path).parts[:-1]
    candidates: list[str] = []
    stripped = module.strip()
    if not stripped:
        return candidates

    if stripped.startswith("."):
        dots = len(stripped) - len(stripped.lstrip("."))
        suffix = stripped[dots:]
        base_parts = list(source_parts[: max(0, len(source_parts) - max(0, dots - 1))])
        if suffix:
            base_parts.extend([part for part in suffix.split(".") if part])
        rel = Path(*base_parts) if base_parts else Path()
        for candidate in (rel.with_suffix(".py"), rel / "__init__.py"):
            if candidate.parts:
                candidates.append(str(candidate))
        return candidates

    rel = Path(*[part for part in stripped.split(".") if part])
    for candidate in (rel.with_suffix(".py"), rel / "__init__.py"):
        if candidate.parts:
            candidates.append(str(candidate))

    return [
        cand for cand in candidates
        if (workspace_root / cand).exists()
    ]


def _extract_related_paths_from_text(text: str, *, source_path: str, workspace_root: Path) -> list[str]:
    related: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"^\s*from\s+([.\w]+)\s+import\s+", text, re.MULTILINE):
        for candidate in _candidate_import_paths(match.group(1), source_path=source_path, workspace_root=workspace_root):
            if candidate != source_path and candidate not in seen:
                seen.add(candidate)
                related.append(candidate)
    for match in re.finditer(r"^\s*import\s+([a-zA-Z0-9_.,\s]+)", text, re.MULTILINE):
        modules = [part.strip().split(" as ", 1)[0].strip() for part in match.group(1).split(",")]
        for module in modules:
            for candidate in _candidate_import_paths(module, source_path=source_path, workspace_root=workspace_root):
                if candidate != source_path and candidate not in seen:
                    seen.add(candidate)
                    related.append(candidate)
    for match in re.finditer(r"""from\s+['"](\.{1,2}/[^'"]+)['"]|import\s+['"](\.{1,2}/[^'"]+)['"]""", text):
        raw = match.group(1) or match.group(2)
        base = (Path(source_path).parent / raw).resolve().relative_to(workspace_root)
        stem = str(base)
        for suffix in ("", ".ts", ".tsx", ".js", ".jsx", ".py"):
            candidate = stem if suffix == "" else f"{stem}{suffix}"
            if (workspace_root / candidate).exists() and candidate not in seen and candidate != source_path:
                seen.add(candidate)
                related.append(candidate)
                break
    return related


def collect_related_context(
    workspace_root: Path,
    *,
    changed_paths: Sequence[str],
    snapshots: Optional[Dict[str, FileSnapshot]] = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_related_files: int = DEFAULT_MAX_RELATED_FILES,
    max_total_chars: int = DEFAULT_MAX_RELATED_CHARS,
    ignore_globs: Optional[Sequence[str]] = None,
) -> list[dict[str, str]]:
    related: list[dict[str, str]] = []
    seen: set[str] = set(changed_paths)
    total_chars = 0
    normalized_ignore_globs = _normalize_ignore_globs(ignore_globs)

    for rel_path in changed_paths:
        text = None
        if snapshots and rel_path in snapshots:
            text = snapshots[rel_path].content
        if text is None:
            text = _read_workspace_file(workspace_root, rel_path, max_file_bytes=max_file_bytes) or ""
        for candidate in _extract_related_paths_from_text(text, source_path=rel_path, workspace_root=workspace_root):
            if candidate in seen:
                continue
            if _matches_ignore_glob(candidate, normalized_ignore_globs):
                continue
            candidate_text = _read_workspace_file(workspace_root, candidate, max_file_bytes=max_file_bytes)
            if not candidate_text:
                continue
            remaining = max_total_chars - total_chars
            if remaining <= 0:
                return related
            excerpt = _truncate_text(candidate_text, min(remaining, 4000))
            related.append({"path": candidate, "content": excerpt})
            total_chars += len(excerpt)
            seen.add(candidate)
            if len(related) >= max_related_files:
                return related
    return related


def _prune_event(event: dict) -> dict:
    pruned = dict(event)
    changes = list(event.get("changes", []))
    kept: list[dict[str, Any]] = []
    total_chars = 0
    omitted = 0
    for change in changes:
        if len(kept) >= DEFAULT_MAX_CHANGES_PER_EVENT:
            omitted += 1
            continue
        diff_text = str(change.get("diff_text") or "")
        remaining = DEFAULT_MAX_DIFF_CHARS - total_chars
        if remaining <= 0:
            omitted += 1
            continue
        change_copy = dict(change)
        change_copy["diff_text"] = _truncate_text(diff_text, remaining)
        total_chars += len(change_copy["diff_text"])
        kept.append(change_copy)
    pruned["changes"] = kept
    if omitted:
        pruned["omitted_changes"] = omitted
    return pruned


def collect_workspace_snapshot(
    root: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ignore_globs: Optional[Sequence[str]] = None,
) -> Dict[str, FileSnapshot]:
    snapshots: Dict[str, FileSnapshot] = {}
    normalized_ignore_globs = _normalize_ignore_globs(ignore_globs)
    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        filtered_dirnames: list[str] = []
        for dirname in dirnames:
            candidate_dir = current_dir / dirname
            if should_ignore_review_path(candidate_dir, root, normalized_ignore_globs):
                continue
            filtered_dirnames.append(dirname)
        dirnames[:] = filtered_dirnames
        for filename in filenames:
            path = current_dir / filename
            if should_ignore_review_path(path, root, normalized_ignore_globs):
                continue
            text = _load_text_file(path, max_file_bytes=max_file_bytes)
            if text is None:
                continue
            stat = path.stat()
            rel_path = str(path.relative_to(root))
            snapshots[rel_path] = FileSnapshot(
                rel_path=rel_path,
                mtime_ns=stat.st_mtime_ns,
                size=stat.st_size,
                digest=_sha256_text(text),
                content=text,
            )
    return snapshots


def build_diff_text(rel_path: str, old_content: str, new_content: str) -> str:
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    if old_content == "" and new_content:
        fromfile = "/dev/null"
        tofile = f"b/{rel_path}"
    elif new_content == "" and old_content:
        fromfile = f"a/{rel_path}"
        tofile = "/dev/null"
    else:
        fromfile = f"a/{rel_path}"
        tofile = f"b/{rel_path}"
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=fromfile,
            tofile=tofile,
            lineterm="\n",
        )
    )


def detect_changes(
    previous: Dict[str, FileSnapshot],
    current: Dict[str, FileSnapshot],
    *,
    now_ts: Optional[float] = None,
) -> Dict[str, PendingChange]:
    now_ts = now_ts or time.time()
    changes: Dict[str, PendingChange] = {}

    previous_paths = set(previous)
    current_paths = set(current)

    for rel_path in sorted(previous_paths - current_paths):
        prev = previous[rel_path]
        changes[rel_path] = PendingChange(
            path=rel_path,
            change_type="deleted",
            old_content=prev.content,
            new_content="",
            first_seen_at=now_ts,
            updated_at=now_ts,
        )

    for rel_path in sorted(current_paths - previous_paths):
        curr = current[rel_path]
        changes[rel_path] = PendingChange(
            path=rel_path,
            change_type="created",
            old_content="",
            new_content=curr.content,
            first_seen_at=now_ts,
            updated_at=now_ts,
        )

    for rel_path in sorted(previous_paths & current_paths):
        prev = previous[rel_path]
        curr = current[rel_path]
        if prev.digest == curr.digest:
            continue
        changes[rel_path] = PendingChange(
            path=rel_path,
            change_type="modified",
            old_content=prev.content,
            new_content=curr.content,
            first_seen_at=now_ts,
            updated_at=now_ts,
        )

    return changes


class HermesStore:
    def __init__(self, root: Optional[Path] = None):
        ensure_hermes_home()
        if root is not None:
            self.root = root
        else:
            hermes_home = get_hermes_home()
            self.root = hermes_home / "store"
            self._migrate_legacy_root(legacy_root=hermes_home / "codex_companion", store_root=self.root)
        self.events_dir = self.root / "events"
        self.outputs_dir = self.root / "outputs"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _migrate_legacy_root(*, legacy_root: Path, store_root: Path) -> None:
        if store_root.exists() or not legacy_root.exists():
            return
        try:
            shutil.move(str(legacy_root), str(store_root))
        except Exception:
            store_root.mkdir(parents=True, exist_ok=True)
            for child in legacy_root.iterdir():
                destination = store_root / child.name
                if destination.exists():
                    continue
                shutil.move(str(child), str(destination))
            try:
                legacy_root.rmdir()
            except OSError:
                pass

    def save_event(self, payload: dict) -> Path:
        event_id = payload["event_id"]
        path = self.events_dir / f"{event_id}.json"
        event_payload = dict(payload)
        event_payload["store_version"] = STORE_SCHEMA_VERSION
        event_payload["kind"] = "diff_event"
        event_payload["summary"] = {
            "change_count": len(payload.get("changes", [])),
            "paths": [str(change.get("path", "")) for change in payload.get("changes", [])],
        }
        for change in event_payload.get("changes", []):
            diff_text = str(change.get("diff_text") or "")
            change["diff_lines"] = _text_lines(diff_text)
        path.write_text(json.dumps(event_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_command_output(
        self,
        *,
        command: str,
        title: str,
        body: str = "",
        subtitle: str = "",
        workspace_root: str = "",
        session_id: str = "",
        status: str = "ok",
        metadata: Optional[dict[str, Any]] = None,
        output_id: Optional[str] = None,
    ) -> Path:
        created_at = time.time()
        output_id = output_id or uuid.uuid4().hex
        payload = {
            "store_version": STORE_SCHEMA_VERSION,
            "kind": "command_output",
            "output_id": output_id,
            "command": command,
            "title": title,
            "subtitle": subtitle,
            "status": status,
            "workspace_root": workspace_root,
            "session_id": session_id,
            "created_at": created_at,
            "content": {
                "text": body,
                "lines": _text_lines(body),
            },
            "metadata": metadata or {},
        }
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime(created_at))
        filename = f"{timestamp}_{_slugify(command)}_{_slugify(title)}_{output_id[:8]}.json"
        path = self.outputs_dir / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_workspace_bridge(payload)
        return path

    def _write_workspace_bridge(self, payload: dict[str, Any]) -> None:
        workspace_root = str(payload.get("workspace_root") or "").strip()
        if not workspace_root:
            return

        analysis_cfg = _analysis_config()
        if analysis_cfg.get("bridge_enabled", True) is False:
            return

        bridge_dir_value = str(analysis_cfg.get("bridge_dir") or ".hermes/companion").strip()
        workspace_path = Path(workspace_root).expanduser().resolve()
        bridge_dir = Path(bridge_dir_value)
        if not bridge_dir.is_absolute():
            bridge_dir = workspace_path / bridge_dir
        bridge_dir.mkdir(parents=True, exist_ok=True)

        bridge_payload = {
            "kind": "hermes_companion_output",
            "bridge_version": 1,
            "command": payload.get("command", ""),
            "title": payload.get("title", ""),
            "subtitle": payload.get("subtitle", ""),
            "status": payload.get("status", "ok"),
            "workspace_root": workspace_root,
            "session_id": payload.get("session_id", ""),
            "created_at": payload.get("created_at"),
            "content": payload.get("content", {}),
            "metadata": payload.get("metadata", {}),
        }

        latest_path = bridge_dir / "latest.json"
        command_path = bridge_dir / f"{_slugify(str(payload.get('command') or 'output'))}.json"
        serialized = json.dumps(bridge_payload, ensure_ascii=False, indent=2)
        latest_path.write_text(serialized, encoding="utf-8")
        command_path.write_text(serialized, encoding="utf-8")

    def save_analysis(self, event_id: str, payload: dict) -> Path:
        body = str(payload.get("analysis") or "")
        metadata = {k: v for k, v in payload.items() if k != "analysis"}
        return self.save_command_output(
            command="review",
            title="Diff Review",
            body=body,
            status="ok" if body else "error",
            metadata={"event_id": event_id, **metadata},
            output_id=event_id,
        )

    def load_latest_output(
        self,
        *,
        command: Optional[str] = None,
        title: Optional[str] = None,
    ) -> Optional[dict]:
        candidates = sorted(self.outputs_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("kind") != "command_output":
                continue
            if command and payload.get("command") != command:
                continue
            if title and payload.get("title") != title:
                continue
            return payload
        return None

    def load_sync_outputs(self) -> dict[str, Optional[dict]]:
        return {command: self.load_latest_output(command=command) for command in SYNC_COMMANDS}

    def load_latest_analysis(self) -> Optional[dict]:
        payload = self.load_latest_output(command="review", title="Diff Review")
        if not payload:
            payload = self.load_latest_output(command="review")
        if not payload:
            return None
        body = payload.get("content", {}).get("text", "")
        result = {
            "analysis": body,
            "event_id": payload.get("metadata", {}).get("event_id", ""),
            "timestamp": payload.get("created_at"),
            "status": payload.get("status", "ok"),
        }
        metadata = payload.get("metadata", {})
        if "error" in metadata:
            result["error"] = metadata["error"]
        if "provider" in metadata:
            result["provider"] = metadata["provider"]
        if "model" in metadata:
            result["model"] = metadata["model"]
        if "base_url" in metadata:
            result["base_url"] = metadata["base_url"]
        if "promotion_candidates" in metadata:
            result["promotion_candidates"] = metadata["promotion_candidates"]
        return result

    def load_latest_event(self) -> Optional[dict]:
        candidates = sorted(self.events_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for candidate in candidates:
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
        return None


CompanionStore = HermesStore


def _event_payload(
    *,
    session_id: str,
    workspace_root: Path,
    changes: Iterable[PendingChange],
) -> dict:
    change_items = []
    for change in changes:
        change_items.append(
            {
                "path": change.path,
                "change_type": change.change_type,
                "old_content": change.old_content,
                "new_content": change.new_content,
                "diff_text": build_diff_text(change.path, change.old_content, change.new_content),
                "first_seen_at": change.first_seen_at,
                "updated_at": change.updated_at,
            }
        )
    return {
        "event_id": uuid.uuid4().hex,
        "session_id": session_id,
        "workspace_root": str(workspace_root),
        "timestamp": time.time(),
        "changes": change_items,
    }


def _analysis_config() -> dict[str, Any]:
    config = load_config()
    analysis_cfg = config.get("analysis")
    return dict(analysis_cfg) if isinstance(analysis_cfg, dict) else {}



def _raw_user_config() -> dict[str, Any]:
    config_path = get_config_path()
    if not config_path.exists():
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
    except Exception:
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}



def _has_explicit_analysis_config() -> bool:
    return "analysis" in _raw_user_config()



def _default_analysis_model() -> str:
    analysis_cfg = _analysis_config()
    configured = str(analysis_cfg.get("model") or "").strip()
    if configured:
        return configured
    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, str) and model_cfg.strip():
        return model_cfg.strip()
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or "anthropic/claude-opus-4.6")
    return "anthropic/claude-opus-4.6"



def _detect_local_analysis_runtime() -> Optional[dict[str, Any]]:
    base_url = "http://127.0.0.1:8000/v1"
    api_key = os.getenv("VLLM_API_KEY", "").strip() or os.getenv("OPENAI_API_KEY", "").strip()
    models = fetch_api_models(api_key, base_url, timeout=0.4) or []
    selected_model = next((model.strip() for model in models if isinstance(model, str) and model.strip()), "")
    if not selected_model:
        return None

    try:
        runtime = resolve_runtime_provider(
            requested="custom",
            explicit_api_key=api_key or None,
            explicit_base_url=base_url,
        )
    except Exception:
        return None
    runtime["base_url"] = base_url
    runtime["api_key"] = api_key
    runtime["api_mode"] = "chat_completions"
    runtime["model"] = selected_model
    runtime["provider"] = "custom"
    runtime["source"] = "analysis-auto-local"
    return runtime



def resolve_analysis_runtime(
    *,
    fallback_runtime: Optional[dict[str, Any]] = None,
    requested_provider: Optional[str] = None,
) -> dict[str, Any]:
    analysis_cfg = _analysis_config()
    explicit_analysis = _has_explicit_analysis_config()

    if explicit_analysis and analysis_cfg.get("enabled", True) is False:
        if fallback_runtime:
            return dict(fallback_runtime)
        return resolve_runtime_provider(requested=requested_provider)

    if explicit_analysis:
        provider = str(analysis_cfg.get("provider") or "").strip().lower()
        base_url = str(analysis_cfg.get("base_url") or "").strip().rstrip("/")
        api_key_env = str(analysis_cfg.get("api_key_env") or "").strip()
        explicit_api_key = os.getenv(api_key_env, "").strip() if api_key_env else ""
        configured_model = str(analysis_cfg.get("model") or "").strip()

        if provider or base_url or api_key_env:
            runtime = resolve_runtime_provider(
                requested=provider or requested_provider or "openrouter",
                explicit_api_key=explicit_api_key or None,
                explicit_base_url=base_url or None,
            )
            if base_url:
                runtime["base_url"] = base_url
                runtime["api_mode"] = "chat_completions"
            if api_key_env or base_url:
                runtime["api_key"] = explicit_api_key
            if configured_model:
                runtime["model"] = configured_model
            if provider == "custom" or (base_url and "openrouter.ai" not in base_url):
                runtime["provider"] = "custom"
            runtime["source"] = "analysis-config"
            return runtime

    if fallback_runtime:
        return dict(fallback_runtime)

    auto_runtime = _detect_local_analysis_runtime()
    if auto_runtime is not None:
        return auto_runtime

    return resolve_runtime_provider(requested=requested_provider)


def _build_analysis_prompt(event: dict, *, natural_language: Optional[str] = None) -> str:
    event = _prune_event(event)
    spec = _language_spec(natural_language)
    guidance = spec["review_guidance"]
    lines = [
        f"You are reviewing a code change-set produced by a coding agent for a developer in {spec['name']}.",
        "Use the diff as the primary evidence. Only infer intent when strongly supported.",
        "If you need extra context, you may read the changed files and related files, but stay focused.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        *spec["review_sections"],
        "",
        guidance["summary"],
        guidance["flow"],
        guidance["risks"],
        guidance["improvements"],
        "",
        f"Workspace: {event['workspace_root']}",
        f"Change-set ID: {event['event_id']}",
        "",
        "Diffs:",
    ]
    for change in event["changes"]:
        lines.append("")
        lines.append(f"### {change['change_type']}: {change['path']}")
        lines.append(change["diff_text"] or "[no diff text]")
    omitted = int(event.get("omitted_changes") or 0)
    if omitted:
        lines.extend(["", f"Note: {omitted} additional change(s) were omitted to keep the review focused."])
    related_files = event.get("related_files") or []
    if related_files:
        lines.extend(["", "Related files (truncated excerpts):"])
        for item in related_files:
            lines.extend(["", f"### related: {item['path']}", item["content"]])
    return "\n".join(lines)


def build_file_explanation_prompt(
    workspace_root: Path,
    *,
    target_path: str,
    symbol: Optional[str] = None,
    related_files: Optional[Sequence[dict[str, str]]] = None,
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    lines = [
        f"You are explaining source code to a developer in {spec['name']}.",
        "Prefer direct evidence from the target file. Read additional files only if necessary.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        *spec["file_sections"],
        "",
        f"Workspace: {workspace_root}",
        f"Target file: {target_path}",
    ]
    if symbol:
        lines.append(f"Focus symbol: {symbol}")
    if related_files:
        lines.extend(["", "Related files (truncated excerpts):"])
        for item in related_files:
            lines.extend(["", f"### related: {item['path']}", item["content"]])
    return "\n".join(lines)


def collect_directory_context(
    workspace_root: Path,
    *,
    target_path: str,
    max_entries: int = 40,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_total_chars: int = 14_000,
) -> dict[str, Any]:
    target_dir = (workspace_root / target_path).resolve()
    entries: list[dict[str, Any]] = []
    total_chars = 0
    omitted_entries = 0

    try:
        relative_target = target_dir.relative_to(workspace_root)
    except ValueError:
        relative_target = Path(target_path)

    for dirpath, dirnames, filenames in os.walk(target_dir):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in DEFAULT_IGNORE_DIRS]

        for dirname in dirnames:
            rel_path = str((current_dir / dirname).relative_to(workspace_root))
            entries.append({"path": rel_path, "kind": "dir"})
            if len(entries) >= max_entries:
                omitted_entries += len(dirnames) - dirnames.index(dirname) - 1 + len(filenames)
                break
        if len(entries) >= max_entries:
            break

        for filename in sorted(filenames):
            path = current_dir / filename
            if should_ignore_path(path, workspace_root):
                omitted_entries += 1
                continue
            rel_path = str(path.relative_to(workspace_root))
            text = _load_text_file(path, max_file_bytes=max_file_bytes)
            if text is None:
                omitted_entries += 1
                continue
            remaining = max_total_chars - total_chars
            excerpt = ""
            if remaining > 0:
                excerpt = _truncate_text(text, min(remaining, 1200))
                total_chars += len(excerpt)
            entries.append({"path": rel_path, "kind": "file", "content": excerpt})
            if len(entries) >= max_entries:
                break
        if len(entries) >= max_entries:
            break

    all_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(target_dir):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]
        for dirname in sorted(dirnames):
            all_paths.append(str((current_dir / dirname).relative_to(workspace_root)))
        for filename in sorted(filenames):
            path = current_dir / filename
            if should_ignore_path(path, workspace_root):
                continue
            if _load_text_file(path, max_file_bytes=max_file_bytes) is None:
                continue
            all_paths.append(str(path.relative_to(workspace_root)))

    if len(all_paths) > len(entries):
        omitted_entries += len(all_paths) - len(entries)

    return {
        "target_path": str(relative_target),
        "entries": entries,
        "total_entries": len(all_paths),
        "omitted_entries": max(0, omitted_entries),
    }


def build_directory_explanation_prompt(
    workspace_root: Path,
    *,
    target_path: str,
    directory_context: dict[str, Any],
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    lines = [
        f"You are explaining a source directory to a developer in {spec['name']}.",
        "Prefer direct evidence from the listed files. Stay focused on architecture, call flow, and responsibilities.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        *spec["directory_sections"],
        "",
        f"Workspace: {workspace_root}",
        f"Target directory: {target_path}",
        f"Included entries: {directory_context.get('total_entries', 0)}",
    ]
    omitted = int(directory_context.get("omitted_entries") or 0)
    if omitted:
        lines.append(f"Omitted entries: {omitted}")
    lines.extend(["", "Directory entries (truncated excerpts):"])
    for item in directory_context.get("entries", []):
        label = item.get("path", "")
        kind = item.get("kind", "file")
        lines.extend(["", f"### {kind}: {label}"])
        content = item.get("content") or ""
        if content:
            lines.append(content)
    return "\n".join(lines)


def build_commit_message_prompt(
    workspace_root: Path,
    *,
    status_text: str,
    diff_text: str,
    changed_paths: Sequence[str],
    untracked_context: str = "",
    extra_instruction: str = "",
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    lines = [
        f"You are writing a git commit message explanation for a developer in {spec['name']}.",
        "Summarize the current uncommitted changes into a single cohesive commit message.",
        "Prefer concrete behavior and intent over low-level diff narration.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        "## Subject",
        "## Details",
        "",
        "Requirements:",
        "- Subject must be exactly one line, imperative mood, max 72 characters, and no trailing period.",
        "- Details must be 2-5 bullet points.",
        "- Mention the most important files or behaviors changed.",
        "- Do not wrap the subject in backticks or quotes.",
        "",
        f"Workspace: {workspace_root}",
        f"Changed paths ({len(changed_paths)}): {', '.join(changed_paths) if changed_paths else '(none)'}",
        "",
        "Git status:",
        status_text or "(empty)",
        "",
    ]
    if extra_instruction.strip():
        lines.extend([
            "Additional instruction:",
            extra_instruction.strip(),
            "",
        ])
    if diff_text.strip():
        lines.extend([
            "Diff:",
            diff_text.strip(),
            "",
        ])
    if untracked_context.strip():
        lines.extend([
            "Untracked file excerpts:",
            untracked_context.strip(),
            "",
        ])
    return "\n".join(lines).strip()


def build_diff_explanation_prompt(
    workspace_root: Path,
    *,
    event: Optional[dict[str, Any]] = None,
    changed_paths: Optional[Sequence[str]] = None,
    diff_text: str = "",
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    lines = [
        f"You are explaining code changes to a developer in {spec['name']}.",
        "Focus on what changed since the previous sync, why it matters, and where the control flow or behavior shifted.",
        "Be concrete and reference files and responsibilities directly.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        "## Overview",
        "## Changed Behavior",
        "## Control Flow Impact",
        "## Risks and Follow-ups",
        "",
        f"Workspace: {workspace_root}",
    ]
    if changed_paths:
        lines.append(f"Changed paths ({len(changed_paths)}): {', '.join(changed_paths)}")
    if event:
        lines.append(f"Change-set ID: {event.get('event_id', '')}")
    lines.extend(["", "Diffs:"])
    if event and event.get("changes"):
        for change in event.get("changes", []):
            lines.extend(["", f"### {change.get('change_type', 'modified')}: {change.get('path', '')}", str(change.get("diff_text") or "[no diff text]")])
    else:
        lines.append(diff_text.strip() or "[no diff available]")
    return "\n".join(lines)


def _collect_workspace_file_paths(workspace_root: Path, *, max_files: int = 4000) -> list[str]:
    rel_paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in DEFAULT_IGNORE_DIRS]
        for filename in sorted(filenames):
            path = current_dir / filename
            if should_ignore_path(path, workspace_root):
                continue
            rel_path = _safe_relpath(path, workspace_root)
            if rel_path:
                rel_paths.append(rel_path)
                if len(rel_paths) >= max_files:
                    return rel_paths
    return rel_paths


def _collect_readme_text(workspace_root: Path, *, max_files: int = 8, max_chars: int = 32_000) -> str:
    context_docs: list[tuple[int, str]] = []
    for rel_path in _collect_workspace_file_paths(workspace_root, max_files=400):
        p = Path(rel_path)
        lower_name = p.name.lower()
        if not (
            lower_name.startswith("readme")
            or lower_name.startswith("agents")
            or lower_name == "soul.md"
            or lower_name == ".cursorrules"
        ):
            continue
        depth = len(p.parts)
        context_docs.append((depth, rel_path))
    chunks: list[str] = []
    total = 0
    for _depth, rel_path in sorted(context_docs)[:max_files]:
        text = _load_text_file(workspace_root / rel_path, max_file_bytes=DEFAULT_MAX_FILE_BYTES)
        if not text:
            continue
        excerpt = _truncate_text(text, min(8_000, max_chars - total))
        chunks.append(f"## {rel_path}\n{excerpt}")
        total += len(excerpt)
        if total >= max_chars:
            break
    return "\n\n".join(chunks)


def _is_probably_flow_source(rel_path: str) -> bool:
    p = Path(rel_path.lower())
    if any(part in FLOW_LOW_SIGNAL_DIRS for part in p.parts[:-1]):
        return False
    if p.name in {"package.json", "makefile", "dockerfile"}:
        return False
    return p.suffix in FLOW_CODE_SUFFIXES or p.name in {"makefile", "dockerfile"}


def _readme_mention_score(readme_text: str, rel_path: str) -> int:
    if not readme_text:
        return 0
    lowered = readme_text.lower()
    target = rel_path.lower()
    basename = Path(target).name
    stem = Path(target).stem
    score = 0
    if target in lowered:
        score += 80
    if basename and re.search(rf"`[^`]*{re.escape(basename)}[^`]*`", lowered):
        score += 45
    elif basename and re.search(rf"(^|[^a-z0-9_]){re.escape(basename)}([^a-z0-9_]|$)", lowered):
        score += 18
    if stem and stem not in {"main", "app", "run", "index"} and re.search(rf"`[^`]*{re.escape(stem)}[^`]*`", lowered):
        score += 10
    if score and re.search(r"(run|start|serve|launch|train|entrypoint|pipeline|workflow|script|command)", lowered):
        score += 10
    return score


def _flow_candidate_score(rel_path: str, *, readme_text: str, changed: bool = False) -> int:
    p = Path(rel_path.lower())
    stem = p.stem
    filename = p.name
    score = 0
    score += _readme_mention_score(readme_text, rel_path)
    if changed:
        score += 35
    if any(part in {"cmd", "bin", "scripts", "runs"} for part in p.parts[:-1]):
        score += 35
    if any(part in {"src", "app", "server", "cli", "web"} for part in p.parts[:-1]):
        score += 14
    if any(token == stem for token in FLOW_ENTRY_HINTS):
        score += 40
    score += sum(10 for token in FLOW_ENTRY_HINTS if token in filename and token != stem)
    if p.name == "__main__.py":
        score += 60
    if p.suffix in {".py", ".go", ".rs", ".js", ".ts", ".tsx", ".jsx"}:
        score += 10
    if p.suffix in {".sh", ".bash", ".zsh"}:
        score += 8
    if any(part in FLOW_LOW_SIGNAL_DIRS for part in p.parts[:-1]):
        score -= 70
    return score


def _resolve_script_reference(rel_path: str, ref: str) -> str:
    value = str(ref or "").strip().strip("\"'")
    if not value:
        return ""
    value = value.split(":", 1)[0].strip()
    base = Path(rel_path).parent
    normalized = (base / value).resolve() if value.startswith(("./", "../")) else Path(value)
    return str(normalized).replace("\\", "/")


def _extract_script_dispatch_target(workspace_root: Path, rel_path: str, text: str, available_paths: set[str]) -> Optional[str]:
    patterns = [
        re.compile(r"(?:^|\s)(?:python|python3|uv\s+run\s+python)\s+-m\s+([A-Za-z_][A-Za-z0-9_\.]*)"),
        re.compile(r"(?:^|\s)(?:python|python3|uv\s+run\s+python)\s+([./A-Za-z0-9_-]+\.py)"),
        re.compile(r"(?:^|\s)(?:node|bun|deno\s+run)\s+([./A-Za-z0-9_-]+\.(?:js|mjs|cjs|ts|tsx))"),
        re.compile(r"(?:^|\s)(?:bash|sh|zsh)\s+([./A-Za-z0-9_-]+\.sh)"),
    ]
    workspace_prefix = str(workspace_root).replace("\\", "/") + "/"
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            if "." in raw and "/" not in raw and not raw.endswith((".py", ".js", ".ts", ".tsx", ".sh")):
                candidate = raw.replace(".", "/") + ".py"
            else:
                candidate = _resolve_script_reference(rel_path, raw)
            candidate = candidate.lstrip("./")
            if candidate in available_paths and _is_probably_flow_source(candidate):
                return candidate
            if candidate.startswith(workspace_prefix):
                trimmed = candidate[len(workspace_prefix):]
                if trimmed in available_paths and _is_probably_flow_source(trimmed):
                    return trimmed
    return None


def _infer_flow_symbol(rel_path: str, text: str) -> str:
    for symbol in FLOW_SYMBOL_PREFERENCES:
        patterns = [
            rf"^\s*(?:async\s+)?def\s+{re.escape(symbol)}\b",
            rf"^\s*class\s+{re.escape(symbol)}\b",
            rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{re.escape(symbol)}\b",
            rf"^\s*(?:export\s+)?(?:const|let|var)\s+{re.escape(symbol)}\s*=",
            rf"^\s*func\s+{re.escape(symbol)}\b",
            rf"^\s*fn\s+{re.escape(symbol)}\b",
            rf"^\s*{re.escape(symbol)}\s*\(\)\s*\{{",
        ]
        for pattern in patterns:
            if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
                return symbol
    generic_patterns = [
        re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE),
        re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE),
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE),
        re.compile(r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE),
        re.compile(r"^\s*fn\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.MULTILINE),
        re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.MULTILINE),
    ]
    for pattern in generic_patterns:
        match = pattern.search(text)
        if match:
            return match.group(1)
    return Path(rel_path).stem


def _make_flow_target(workspace_root: Path, rel_path: str, *, reason: str, available_paths: set[str]) -> dict[str, str]:
    chosen_path = rel_path
    text = _load_text_file(workspace_root / rel_path, max_file_bytes=DEFAULT_MAX_FILE_BYTES) or ""
    redirected = None
    if Path(rel_path).suffix.lower() in {".sh", ".bash", ".zsh"}:
        redirected = _extract_script_dispatch_target(workspace_root, rel_path, text, available_paths)
    if redirected:
        chosen_path = redirected
        text = _load_text_file(workspace_root / redirected, max_file_bytes=DEFAULT_MAX_FILE_BYTES) or ""
        reason = f"{reason}; dispatched from {rel_path}"
    symbol = _infer_flow_symbol(chosen_path, text)
    return {"path": chosen_path, "symbol": symbol, "reason": reason}


def _looks_like_entrypoint(rel_path: str) -> bool:
    lowered = rel_path.lower()
    filename = Path(lowered).name
    stem = Path(lowered).stem
    return any(token in lowered for token in ("main", "app", "server", "run", "cli", "index", "train", "api", "manage")) or stem in {
        "main", "app", "server", "run", "cli", "index", "train", "manage"
    } or filename in {"package.json", "pyproject.toml", "requirements.txt", "makefile"}


def _priority_for_path(rel_path: str) -> tuple[int, int, str]:
    p = Path(rel_path)
    depth = len(p.parts)
    score = 100
    if depth == 1:
        score -= 25
    if _looks_like_entrypoint(rel_path):
        score -= 35
    if p.name.lower().startswith("readme"):
        score -= 20
    if p.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".jsx", ".md", ".toml", ".yaml", ".yml", ".json"}:
        score -= 10
    return (score, depth, rel_path)


def collect_project_summary(workspace_root: Path, *, max_files: int = 18, max_chars: int = 18_000) -> dict[str, Any]:
    files: list[dict[str, str]] = []
    directories: list[str] = []
    total_chars = 0
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in DEFAULT_IGNORE_DIRS]
        if current_dir != workspace_root:
            rel_dir = _safe_relpath(current_dir, workspace_root)
            if rel_dir:
                directories.append(rel_dir)
        for filename in sorted(filenames):
            path = current_dir / filename
            if should_ignore_path(path, workspace_root):
                continue
            rel_path = _safe_relpath(path, workspace_root)
            if not rel_path:
                continue
            text = _load_text_file(path, max_file_bytes=DEFAULT_MAX_FILE_BYTES)
            if text is None:
                continue
            files.append({"path": rel_path, "content": _truncate_text(text, 1800)})
    files.sort(key=lambda item: _priority_for_path(item["path"]))
    selected_files: list[dict[str, str]] = []
    for item in files:
        excerpt = item["content"]
        if total_chars >= max_chars or len(selected_files) >= max_files:
            break
        excerpt = _truncate_text(excerpt, min(1800, max_chars - total_chars))
        selected_files.append({"path": item["path"], "content": excerpt})
        total_chars += len(excerpt)
    directories.sort()
    return {"directories": directories[:30], "files": selected_files}


def build_project_review_prompt(
    workspace_root: Path,
    *,
    project_context: dict[str, Any],
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    lines = [
        f"You are reviewing the current quality and architecture of a codebase for a developer in {spec['name']}.",
        "Focus on major responsibilities, control flow, current quality level, and the most important risks or improvement opportunities.",
        "",
        f"Return exactly these sections in {spec['name']}:",
        "## Overview",
        "## Architecture and Flow",
        "## Quality Assessment",
        "## Key Risks and Improvements",
        "",
        f"Workspace: {workspace_root}",
        "",
        "Directories:",
    ]
    for rel_dir in project_context.get("directories", []):
        lines.append(f"- {rel_dir}")
    lines.extend(["", "Representative files:"])
    for item in project_context.get("files", []):
        lines.extend(["", f"### {item['path']}", item.get("content", "")])
    return "\n".join(lines)


def build_sync_planner_prompt(
    workspace_root: Path,
    *,
    project_context: dict[str, Any],
    changed_paths: Sequence[str],
    natural_language: Optional[str] = None,
    sync_kind: str = "incremental",
) -> str:
    sync_kind = _normalize_sync_kind(sync_kind)
    lines = [
        "Select the most important codebase targets for four synchronized panes: flow, explain, review, and diff.",
        "Return JSON only. No markdown fences. The JSON object must have keys: flow_targets, explain_targets, review_scope, diff_scope.",
        "Each flow_targets item must be an object with path, symbol, and reason.",
        "Each explain_targets item must be an object with path, kind, and reason where kind is file or directory.",
        "review_scope must be a short string. diff_scope must be a short string.",
    ]
    if sync_kind == "startup":
        lines.extend([
            "Treat the workspace itself as the analysis scope.",
            "For flow_targets, prioritize the main entrypoint, orchestration layer, router, app factory, or central control-flow symbol for this workspace.",
            "Do not prioritize recently changed files because startup sync is workspace-based, not diff-based.",
            "Prefer files explicitly recommended in README, AGENTS.md, or getting-started docs, startup scripts, and the code they dispatch into.",
            "Choose symbols that best explain how this workspace starts, routes requests, or coordinates work.",
        ])
    else:
        lines.extend([
            "Use changed paths as a strong hint, but prefer the files and symbols that best explain the current control flow.",
        ])
    lines.extend([
        f"Workspace: {workspace_root}",
        f"Sync kind: {sync_kind}",
        f"Changed paths: {', '.join(changed_paths) if changed_paths else '(none)'}",
        "",
        "Directories:",
    ])
    for rel_dir in project_context.get("directories", []):
        lines.append(f"- {rel_dir}")
    lines.extend(["", "Representative files:"])
    for item in project_context.get("files", [])[:12]:
        lines.extend(["", f"### {item['path']}", _truncate_text(item.get("content", ""), 1000)])
    return "\n".join(lines)


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    candidates = [stripped]
    if "```" in stripped:
        for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL):
            candidates.append(match.group(1))
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _default_sync_plan(
    workspace_root: Path,
    *,
    project_context: dict[str, Any],
    changed_paths: Sequence[str],
) -> dict[str, Any]:
    file_candidates = [item["path"] for item in project_context.get("files", [])]
    top_files = [path for path in file_candidates if Path(path).parent == Path()]
    explain_targets = []
    seed_paths = list(changed_paths) if changed_paths else []
    for path in file_candidates:
        if path not in seed_paths and len(seed_paths) < DEFAULT_STARTUP_FILE_TARGETS:
            seed_paths.append(path)
    for path in seed_paths[:DEFAULT_STARTUP_FILE_TARGETS]:
        explain_targets.append({"path": path, "kind": "file", "reason": "high priority file"})
    seen_dirs = set()
    for rel_dir in project_context.get("directories", []):
        if rel_dir in seen_dirs:
            continue
        explain_targets.append({"path": rel_dir, "kind": "directory", "reason": "high priority directory"})
        seen_dirs.add(rel_dir)
        if len([item for item in explain_targets if item.get('kind') == 'directory']) >= DEFAULT_STARTUP_DIR_TARGETS:
            break

    available_paths = [path for path in _collect_workspace_file_paths(workspace_root) if _is_probably_flow_source(path)]
    if not available_paths:
        available_paths = [path for path in file_candidates if _is_probably_flow_source(path)]
    available_path_set = set(available_paths)
    readme_text = _collect_readme_text(workspace_root)
    ranked_paths = sorted(
        available_paths,
        key=lambda rel_path: (-_flow_candidate_score(rel_path, readme_text=readme_text, changed=rel_path in changed_paths), len(Path(rel_path).parts), rel_path),
    )
    flow_targets = []
    seen_flow_paths: set[str] = set()
    for rel_path in ranked_paths:
        score = _flow_candidate_score(rel_path, readme_text=readme_text, changed=rel_path in changed_paths)
        if score <= 0:
            continue
        reason = "README-guided entrypoint" if _readme_mention_score(readme_text, rel_path) else ("entrypoint-like file" if _looks_like_entrypoint(rel_path) else "representative executable file")
        target = _make_flow_target(workspace_root, rel_path, reason=reason, available_paths=available_path_set)
        if target["path"] in seen_flow_paths:
            continue
        seen_flow_paths.add(target["path"])
        flow_targets.append(target)
        if len(flow_targets) >= DEFAULT_STARTUP_FLOW_TARGETS:
            break
    if not flow_targets and top_files:
        fallback_paths = available_path_set or set(top_files)
        for path in top_files[:DEFAULT_STARTUP_FLOW_TARGETS]:
            target = _make_flow_target(workspace_root, path, reason="top-level file", available_paths=fallback_paths)
            if target["path"] in seen_flow_paths:
                continue
            seen_flow_paths.add(target["path"])
            flow_targets.append(target)
    return {
        "flow_targets": flow_targets,
        "explain_targets": explain_targets[: DEFAULT_STARTUP_FILE_TARGETS + DEFAULT_STARTUP_DIR_TARGETS],
        "review_scope": "project quality and architecture",
        "diff_scope": "recent changes",
    }


def plan_sync_targets(
    workspace_root: Path,
    *,
    project_context: dict[str, Any],
    changed_paths: Sequence[str],
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
    sync_kind: str = "incremental",
) -> dict[str, Any]:
    fallback = _default_sync_plan(workspace_root, project_context=project_context, changed_paths=changed_paths)
    prompt = build_sync_planner_prompt(
        workspace_root,
        project_context=project_context,
        changed_paths=changed_paths,
        natural_language=natural_language,
        sync_kind=sync_kind,
    )
    try:
        result = analyze_prompt(prompt, model=model, session_id=f"store-plan-{uuid.uuid4().hex}", runtime=runtime)
        parsed = _extract_json_object(result.get("analysis", ""))
    except Exception:
        parsed = {}
    if not parsed:
        return fallback

    def _normalize_explain(items: Any) -> list[dict[str, str]]:
        normalized = []
        seen = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            rel_path = str(item.get("path") or "").strip().lstrip("./")
            kind = str(item.get("kind") or "file").strip().lower()
            if not rel_path or kind not in {"file", "directory"} or rel_path in seen:
                continue
            target = (workspace_root / rel_path).resolve()
            if not target.exists():
                continue
            if kind == "directory" and not target.is_dir():
                continue
            if kind == "file" and not target.is_file():
                continue
            if should_ignore_review_path(target, workspace_root):
                continue
            seen.add(rel_path)
            normalized.append({"path": rel_path, "kind": kind, "reason": str(item.get("reason") or "planned target")})
        return normalized[: DEFAULT_STARTUP_FILE_TARGETS + DEFAULT_STARTUP_DIR_TARGETS]

    def _normalize_flow(items: Any) -> list[dict[str, str]]:
        normalized = []
        seen = set()
        for item in items or []:
            if not isinstance(item, dict):
                continue
            rel_path = str(item.get("path") or "").strip().lstrip("./")
            symbol = str(item.get("symbol") or "").strip()
            if not rel_path or rel_path in seen:
                continue
            target = (workspace_root / rel_path).resolve()
            if not target.is_file() or should_ignore_review_path(target, workspace_root):
                continue
            seen.add(rel_path)
            file_text = _load_text_file(target, max_file_bytes=DEFAULT_MAX_FILE_BYTES) or ""
            normalized.append({"path": rel_path, "symbol": symbol or _infer_flow_symbol(rel_path, file_text), "reason": str(item.get("reason") or "planned flow target")})
        return normalized[: DEFAULT_STARTUP_FLOW_TARGETS if _normalize_sync_kind(sync_kind) == "startup" else DEFAULT_INCREMENTAL_FLOW_TARGETS]

    return {
        "flow_targets": _normalize_flow(parsed.get("flow_targets")) or fallback["flow_targets"],
        "explain_targets": _normalize_explain(parsed.get("explain_targets")) or fallback["explain_targets"],
        "review_scope": str(parsed.get("review_scope") or fallback["review_scope"]),
        "diff_scope": str(parsed.get("diff_scope") or fallback["diff_scope"]),
    }


def _candidate_sync_files(
    workspace_root: Path,
    *,
    changed_paths: Sequence[str],
    ignore_globs: Optional[Sequence[str]] = None,
    limit: int,
) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def _consider(rel_path: str) -> None:
        normalized = str(rel_path or '').strip().lstrip('./')
        if not normalized or normalized in seen:
            return
        target = (workspace_root / normalized).resolve()
        if not target.is_file() or should_ignore_review_path(target, workspace_root):
            return
        if ignore_globs and _path_matches_any_glob(Path(normalized), ignore_globs):
            return
        if _load_text_file(target, max_file_bytes=DEFAULT_MAX_FILE_BYTES) is None:
            return
        seen.add(normalized)
        candidates.append(normalized)

    for rel_path in changed_paths:
        _consider(rel_path)
        if len(candidates) >= limit:
            return candidates

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in sorted(dirnames) if d not in DEFAULT_IGNORE_DIRS]
        for filename in sorted(filenames):
            rel_path = _safe_relpath(current_dir / filename, workspace_root)
            if rel_path:
                _consider(rel_path)
                if len(candidates) >= limit:
                    return candidates
    return candidates


def _select_flow_targets(
    workspace_root: Path,
    *,
    changed_paths: Sequence[str],
    ignore_globs: Optional[Sequence[str]] = None,
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
    sync_kind: str = "incremental",
) -> list[dict[str, str]]:
    sync_kind = _normalize_sync_kind(sync_kind)
    limit = DEFAULT_STARTUP_FLOW_TARGETS if sync_kind == "startup" else DEFAULT_INCREMENTAL_FLOW_TARGETS

    def _doc_references(doc_text: str, available_paths: set[str]) -> set[str]:
        if not doc_text or not available_paths:
            return set()

        refs: set[str] = set()

        def _try_add(raw: str) -> None:
            token = str(raw or "").strip().strip("`\"'")
            if not token:
                return
            token = token.split("#", 1)[0].split("?", 1)[0].strip()
            token = token.strip(".,:;()[]{}")
            if not token or "://" in token:
                return
            normalized = token.replace("\\", "/")
            if normalized.startswith("/"):
                try:
                    rel = str(Path(normalized).resolve().relative_to(workspace_root.resolve()))
                except Exception:
                    return
            else:
                rel = normalized.lstrip("./")
            if rel in available_paths:
                refs.add(rel)

        for match in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", doc_text):
            _try_add(match.group(1))
            if len(refs) >= 160:
                return refs

        for match in re.finditer(r"`([^`]+)`", doc_text):
            _try_add(match.group(1))
            if len(refs) >= 160:
                return refs

        token_pattern = re.compile(r"(?<![A-Za-z0-9_])(?:\./|\.\./)?[A-Za-z0-9_./-]+\.[A-Za-z0-9_+-]+(?![A-Za-z0-9_])")
        for match in token_pattern.finditer(doc_text):
            _try_add(match.group(0))
            if len(refs) >= 160:
                break

        return refs

    if sync_kind == "startup":
        project_context = collect_project_summary(workspace_root)
        planned = plan_sync_targets(
            workspace_root,
            project_context=project_context,
            changed_paths=[],
            model=model,
            runtime=runtime,
            natural_language=natural_language,
            sync_kind="startup",
        )
        planned_targets = list(planned.get("flow_targets") or [])

        guide_text = _collect_readme_text(workspace_root)
        all_candidates: list[str] = []
        for rel_path in _collect_workspace_file_paths(workspace_root, max_files=4000):
            target = (workspace_root / rel_path).resolve()
            if not target.is_file() or not _is_probably_flow_source(rel_path):
                continue
            if should_ignore_review_path(target, workspace_root):
                continue
            if ignore_globs and _path_matches_any_glob(Path(rel_path), ignore_globs):
                continue
            if _load_text_file(target, max_file_bytes=DEFAULT_MAX_FILE_BYTES) is None:
                continue
            all_candidates.append(rel_path)

        available_paths = set(all_candidates)
        doc_refs = _doc_references(guide_text, available_paths)

        main_content_scores: dict[str, int] = {}
        if not doc_refs:
            for rel_path in all_candidates:
                stem = Path(rel_path).stem.lower()
                if not stem.startswith("main"):
                    continue
                file_path = workspace_root / rel_path
                file_text = _load_text_file(file_path, max_file_bytes=DEFAULT_MAX_FILE_BYTES) or ""
                if not file_text:
                    continue
                score = 20
                inferred_symbol = _infer_flow_symbol(rel_path, file_text)
                if inferred_symbol and inferred_symbol.lower() not in {"main", stem}:
                    score += 45
                if re.search(r"if\s+__name__\s*==\s*['\"]__main__['\"]", file_text):
                    score += 90
                if re.search(r"\b(?:main|run|start|serve|bootstrap|launch)\s*\(", file_text):
                    score += 35
                if re.search(r"\b(?:app|router|server|cli)\b", file_text, re.IGNORECASE):
                    score += 20
                if Path(rel_path).suffix.lower() in {".sh", ".bash", ".zsh"}:
                    redirected = _extract_script_dispatch_target(workspace_root, rel_path, file_text, available_paths)
                    if redirected:
                        score += 60
                if score > 0:
                    main_content_scores[rel_path] = score

        ranked_candidates = sorted(
            all_candidates,
            key=lambda rel_path: (
                -(
                    _flow_candidate_score(rel_path, readme_text=guide_text, changed=False)
                    + (120 if rel_path in doc_refs else 0)
                    + (main_content_scores.get(rel_path, 0) if not doc_refs else 0)
                    + (25 if _looks_like_entrypoint(rel_path) else 0)
                ),
                len(Path(rel_path).parts),
                rel_path,
            ),
        )

        targets: list[dict[str, str]] = []
        seen_paths: set[str] = set()

        for item in planned_targets:
            rel_path = str(item.get("path") or "").strip()
            symbol = str(item.get("symbol") or "").strip()
            reason = str(item.get("reason") or "planned flow target")
            if not rel_path or rel_path in seen_paths:
                continue
            if rel_path not in available_paths:
                continue
            target = _make_flow_target(workspace_root, rel_path, reason=reason, available_paths=available_paths)
            if symbol:
                target["symbol"] = symbol
            seen_paths.add(target["path"])
            targets.append(target)
            if len(targets) >= limit:
                return targets

        for rel_path in ranked_candidates:
            if rel_path in seen_paths:
                continue
            if rel_path in doc_refs:
                reason = "doc-guided entrypoint"
            elif main_content_scores.get(rel_path, 0) > 0 and not doc_refs:
                reason = "main-file content indicates entrypoint"
            else:
                reason = "workspace representative file"
            target = _make_flow_target(workspace_root, rel_path, reason=reason, available_paths=available_paths)
            if target["path"] in seen_paths:
                continue
            seen_paths.add(target["path"])
            targets.append(target)
            if len(targets) >= limit:
                break

        if targets:
            return targets

    file_limit = max(limit * 3, DEFAULT_STARTUP_FILE_TARGETS)
    candidates = _candidate_sync_files(
        workspace_root,
        changed_paths=changed_paths,
        ignore_globs=ignore_globs,
        limit=file_limit,
    )
    readme_text = _collect_readme_text(workspace_root) if sync_kind == "startup" else ""
    available_paths = set(candidates)
    ranked_candidates = sorted(
        candidates,
        key=lambda rel_path: (-_flow_candidate_score(rel_path, readme_text=readme_text, changed=rel_path in changed_paths), len(Path(rel_path).parts), rel_path),
    )
    targets: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for rel_path in ranked_candidates:
        reason = "changed file" if rel_path in changed_paths else "representative file"
        if sync_kind == "startup":
            if _readme_mention_score(readme_text, rel_path):
                reason = "README-guided entrypoint"
            else:
                reason = "workspace representative file"
        elif _looks_like_entrypoint(rel_path):
            reason = "entrypoint-like file"
        target = _make_flow_target(workspace_root, rel_path, reason=reason, available_paths=available_paths)
        if target["path"] in seen_paths:
            continue
        seen_paths.add(target["path"])
        targets.append(target)
        if len(targets) >= limit:
            break
    return targets


def _select_explain_targets(
    flow_targets: Sequence[dict[str, str]],
    *,
    sync_kind: str = 'incremental',
) -> list[dict[str, str]]:
    sync_kind = _normalize_sync_kind(sync_kind)
    file_limit = DEFAULT_STARTUP_FILE_TARGETS if sync_kind == 'startup' else DEFAULT_INCREMENTAL_FILE_TARGETS
    dir_limit = DEFAULT_STARTUP_DIR_TARGETS if sync_kind == 'startup' else 2
    targets: list[dict[str, str]] = []
    seen_files: set[str] = set()
    seen_dirs: set[str] = set()

    for item in flow_targets:
        rel_path = str(item.get('path') or '').strip()
        if not rel_path or rel_path in seen_files:
            continue
        seen_files.add(rel_path)
        targets.append({'path': rel_path, 'kind': 'file', 'reason': 'selected from flow target'})
        if len([t for t in targets if t.get('kind') == 'file']) >= file_limit:
            break

    for item in flow_targets:
        parent = str(Path(str(item.get('path') or '')).parent)
        if not parent or parent == '.' or parent in seen_dirs:
            continue
        seen_dirs.add(parent)
        targets.append({'path': parent, 'kind': 'directory', 'reason': 'parent directory of flow target'})
        if len([t for t in targets if t.get('kind') == 'directory']) >= dir_limit:
            break

    return targets


def _explain_directory(
    workspace_root: Path,
    *,
    target_path: str,
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
) -> dict[str, Any]:
    directory_context = collect_directory_context(workspace_root, target_path=target_path)
    prompt = build_directory_explanation_prompt(
        workspace_root,
        target_path=target_path,
        directory_context=directory_context,
        natural_language=natural_language,
    )
    result = analyze_prompt(
        prompt,
        model=model,
        session_id=f'store-explain-{uuid.uuid4().hex}',
        runtime=runtime,
    )
    result.update({'target_path': target_path, 'kind': 'directory'})
    return result


def _runtime_metadata_from_result(result: Optional[dict[str, Any]]) -> dict[str, str]:
    if not isinstance(result, dict):
        return {}
    metadata: dict[str, str] = {}
    for key in ("provider", "model", "base_url"):
        value = str(result.get(key) or "").strip()
        if value:
            metadata[key] = value
    return metadata

def generate_sync_bundle(
    workspace_root: Path,
    *,
    event: Optional[dict[str, Any]] = None,
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
    ignore_globs: Optional[Sequence[str]] = None,
    sync_kind: str = 'incremental',
) -> dict[str, dict[str, Any]]:
    workspace_root = workspace_root.resolve()
    sync_kind = _normalize_sync_kind(sync_kind)
    changed_paths = [str(change.get('path')) for change in (event or {}).get('changes', []) if change.get('path')]
    bundle: dict[str, dict[str, Any]] = {}

    review_event = event or {
        'event_id': f'startup-{uuid.uuid4().hex}',
        'workspace_root': str(workspace_root),
        'changes': [],
    }
    review_result = analyze_change_set(
        review_event,
        model=model,
        runtime=runtime,
        natural_language=natural_language,
        ignore_globs=ignore_globs,
    )
    bundle['review'] = {
        'title': 'Diff Review',
        'subtitle': ', '.join(changed_paths[:3]) if changed_paths else 'startup snapshot',
        'body': review_result.get('analysis', ''),
        'metadata': {
            'sync_kind': sync_kind,
            'targets': [{'path': path} for path in changed_paths],
            'related_files': review_result.get('related_files', []),
            'promotion_candidates': review_result.get('promotion_candidates', []),
            'selection_reason': 'changed files' if changed_paths else 'workspace snapshot',
            **_runtime_metadata_from_result(review_result),
            'source_event_id': review_event.get('event_id', ''),
        },
    }

    flow_targets = _select_flow_targets(
        workspace_root,
        changed_paths=changed_paths,
        ignore_globs=ignore_globs,
        model=model,
        runtime=runtime,
        natural_language=natural_language,
        sync_kind=sync_kind,
    )
    flow_parts: list[str] = []
    flow_runtime_meta: dict[str, str] = {}
    for item in flow_targets:
        result = explain_file(
            workspace_root,
            target_path=item['path'],
            symbol=item['symbol'],
            model=model,
            runtime=runtime,
            natural_language=natural_language,
        )
        if not flow_runtime_meta:
            flow_runtime_meta = _runtime_metadata_from_result(result)
        flow_parts.append(f"# {item['symbol']} @ {item['path']}\n{result.get('analysis', '')}".strip())
    bundle['flow'] = {
        'title': 'Flow',
        'subtitle': ', '.join(item['symbol'] for item in flow_targets[:3]) or 'No targets',
        'body': '\n\n'.join(part for part in flow_parts if part).strip(),
        'metadata': {
            'sync_kind': sync_kind,
            'targets': flow_targets,
            'selection_reason': 'workspace-based llm planner' if sync_kind == 'startup' else 'changed files first, then representative files',
            **flow_runtime_meta,
        },
    }

    explain_targets = _select_explain_targets(flow_targets, sync_kind=sync_kind)
    explain_parts: list[str] = []
    explain_runtime_meta: dict[str, str] = {}
    for item in explain_targets:
        rel_path = str(item.get('path') or '')
        kind = str(item.get('kind') or 'file')
        if not rel_path:
            continue
        if kind == 'directory':
            result = _explain_directory(
                workspace_root,
                target_path=rel_path,
                model=model,
                runtime=runtime,
                natural_language=natural_language,
            )
        else:
            result = explain_file(
                workspace_root,
                target_path=rel_path,
                model=model,
                runtime=runtime,
                natural_language=natural_language,
            )
        if not explain_runtime_meta:
            explain_runtime_meta = _runtime_metadata_from_result(result)
        explain_parts.append(f"# {rel_path}\n{result.get('analysis', '')}".strip())
    bundle['explain'] = {
        'title': 'Explain',
        'subtitle': ', '.join(item['path'] for item in explain_targets[:3]) or 'No targets',
        'body': '\n\n'.join(part for part in explain_parts if part).strip(),
        'metadata': {
            'sync_kind': sync_kind,
            'targets': explain_targets,
            'selection_reason': 'derived from flow targets',
            **explain_runtime_meta,
        },
    }

    diff_prompt = build_diff_explanation_prompt(
        workspace_root,
        event=event,
        changed_paths=changed_paths,
        natural_language=natural_language,
    )
    diff_result = analyze_prompt(
        diff_prompt,
        model=model,
        session_id=f'store-diff-{uuid.uuid4().hex}',
        runtime=runtime,
    )
    bundle['diff'] = {
        'title': 'Diff',
        'subtitle': ', '.join(changed_paths[:3]) if changed_paths else 'No tracked changes',
        'body': diff_result.get('analysis', ''),
        'metadata': {
            'sync_kind': sync_kind,
            'targets': [{'path': path} for path in changed_paths],
            'selection_reason': 'current git/workspace diff',
            **_runtime_metadata_from_result(diff_result),
            'source_event_id': (event or {}).get('event_id', ''),
        },
    }

    return bundle

def find_symbol_candidates(
    workspace_root: Path,
    *,
    symbol: str,
    max_matches: int = DEFAULT_SYMBOL_MATCHES,
) -> list[str]:
    if not symbol.strip():
        return []
    patterns = [
        re.compile(rf"^\s*def\s+{re.escape(symbol)}\b", re.MULTILINE),
        re.compile(rf"^\s*class\s+{re.escape(symbol)}\b", re.MULTILINE),
        re.compile(rf"\b{re.escape(symbol)}\b"),
    ]
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(workspace_root):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]
        for filename in filenames:
            path = current_dir / filename
            if should_ignore_path(path, workspace_root):
                continue
            text = _load_text_file(path, max_file_bytes=DEFAULT_MAX_FILE_BYTES)
            if text is None:
                continue
            if any(pattern.search(text) for pattern in patterns):
                matches.append(str(path.relative_to(workspace_root)))
                if len(matches) >= max_matches:
                    return matches
    return matches


def _runtime_display_provider(runtime: Optional[dict[str, Any]]) -> str:
    runtime = dict(runtime or {})
    provider = str(runtime.get("provider") or "").strip().lower()
    base_url = str(runtime.get("base_url") or "").strip().lower()
    if provider == "openrouter" and base_url and "openrouter.ai" not in base_url:
        return "custom"
    if provider:
        return provider
    if base_url:
        return "custom"
    return "openrouter"


def analyze_prompt(
    prompt: str,
    *,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    thinking_callback: Optional[Callable[[str], None]] = None,
    tool_progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    from run_agent import AIAgent

    runtime = dict(runtime) if runtime else resolve_analysis_runtime()
    effective_model = str(runtime.get("model") or model or _default_analysis_model()).strip() or _default_analysis_model()
    if thinking_callback is None:
        def _noop_thinking_callback(_text: str) -> None:
            return
        thinking_callback = _noop_thinking_callback
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=effective_model,
        enabled_toolsets=["file", "session_search"],
        quiet_mode=True,
        platform="cli",
        session_id=session_id or f"store-{uuid.uuid4().hex}",
        skip_memory=True,
        thinking_callback=thinking_callback,
        tool_progress_callback=tool_progress_callback,
    )
    result = agent.run_conversation(prompt)
    response_text = result.get("final_response") if isinstance(result, dict) else str(result)
    return {
        "model": effective_model,
        "provider": _runtime_display_provider(runtime),
        "base_url": runtime.get("base_url"),
        "analysis": response_text or "",
        "timestamp": time.time(),
    }


def analyze_change_set(
    event: dict,
    *,
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
    ignore_globs: Optional[Sequence[str]] = None,
    thinking_callback: Optional[Callable[[str], None]] = None,
    tool_progress_callback: Optional[Callable[..., None]] = None,
) -> dict:
    event = dict(event)
    root = Path(event["workspace_root"])
    snapshots = collect_workspace_snapshot(
        root,
        max_file_bytes=DEFAULT_MAX_FILE_BYTES,
        ignore_globs=ignore_globs,
    )
    event["related_files"] = collect_related_context(
        root,
        changed_paths=[str(change["path"]) for change in event.get("changes", [])],
        snapshots=snapshots,
        ignore_globs=ignore_globs,
    )
    prompt = _build_analysis_prompt(event, natural_language=natural_language)
    result = analyze_prompt(
        prompt,
        model=model,
        session_id=f"store-{event['event_id']}",
        runtime=runtime,
        thinking_callback=thinking_callback,
        tool_progress_callback=tool_progress_callback,
    )
    promotion_candidates = extract_promotion_candidates(
        result.get("analysis", ""),
        command_name="review",
        metadata={"related_files": event["related_files"]},
        natural_language=natural_language,
    )
    return {
        "event_id": event["event_id"],
        "model": result.get("model") or model or _default_analysis_model(),
        "provider": result.get("provider"),
        "base_url": result.get("base_url"),
        "analysis": result.get("analysis", ""),
        "timestamp": result.get("timestamp", time.time()),
        "related_files": event["related_files"],
        "promotion_candidates": promotion_candidates,
    }


def explain_file(
    workspace_root: Path,
    *,
    target_path: str,
    symbol: Optional[str] = None,
    model: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    natural_language: Optional[str] = None,
) -> dict:
    related = collect_related_context(
        workspace_root,
        changed_paths=[target_path],
        max_related_files=4,
        max_total_chars=10_000,
    )
    prompt = build_file_explanation_prompt(
        workspace_root,
        target_path=target_path,
        symbol=symbol,
        related_files=related,
        natural_language=natural_language,
    )
    result = analyze_prompt(
        prompt,
        model=model,
        session_id=f"store-explain-{uuid.uuid4().hex}",
        runtime=runtime,
    )
    result.update({"target_path": target_path, "symbol": symbol or "", "related_files": related})
    return result


class CodexCompanionWatcher:
    def __init__(
        self,
        workspace_root: Path,
        *,
        poll_interval: float = 1.0,
        debounce_seconds: float = 2.0,
        analyze: bool = True,
        once: bool = False,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        ignore_globs: Optional[Sequence[str]] = None,
        runtime: Optional[dict[str, Any]] = None,
        model: Optional[str] = None,
        natural_language: Optional[str] = None,
        on_event: Optional[Callable[[dict, Path], None]] = None,
        on_analysis: Optional[Callable[[dict, Path], None]] = None,
        stop_event: Optional[threading.Event] = None,
        emit_logs: bool = True,
    ):
        self.workspace_root = workspace_root.resolve()
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds
        self.analyze = analyze
        self.once = once
        self.max_file_bytes = max_file_bytes
        self.ignore_globs = _normalize_ignore_globs(ignore_globs)
        self.runtime = runtime
        self.model = model
        self.natural_language = _normalize_natural_language(natural_language)
        self.on_event = on_event
        self.on_analysis = on_analysis
        self.stop_event = stop_event or threading.Event()
        self.emit_logs = bool(emit_logs)
        self.session_id = uuid.uuid4().hex
        self.store = HermesStore()
        self._previous_snapshot = collect_workspace_snapshot(
            self.workspace_root,
            max_file_bytes=self.max_file_bytes,
            ignore_globs=self.ignore_globs,
        )
        self._pending: Dict[str, PendingChange] = {}

    def _log(self, message: str) -> None:
        if self.emit_logs:
            print(message)

    def _merge_changes(self, changes: Dict[str, PendingChange]) -> None:
        for rel_path, change in changes.items():
            existing = self._pending.get(rel_path)
            if existing is None:
                self._pending[rel_path] = change
                continue
            if existing.change_type == "created" and change.change_type == "deleted":
                # A file that is created and deleted within the debounce window
                # has no net effect and should not be emitted.
                self._pending.pop(rel_path, None)
                continue
            if existing.change_type == "created" and change.change_type == "modified":
                existing.refresh(
                    change_type="created",
                    new_content=change.new_content,
                    updated_at=change.updated_at,
                )
                continue
            if existing.change_type == "deleted" and change.change_type == "created":
                if existing.old_content == change.new_content:
                    # Delete/recreate with identical content is also a no-op.
                    self._pending.pop(rel_path, None)
                    continue
                existing.refresh(
                    change_type="modified",
                    new_content=change.new_content,
                    updated_at=change.updated_at,
                )
                continue
            existing.refresh(
                change_type=change.change_type,
                new_content=change.new_content,
                updated_at=change.updated_at,
            )

    def _flush_ready(self, *, force: bool = False) -> Optional[dict]:
        if not self._pending:
            return None
        latest_update = max(change.updated_at for change in self._pending.values())
        if not force and (time.time() - latest_update) < self.debounce_seconds:
            return None
        changes = [self._pending[k] for k in sorted(self._pending)]
        self._pending = {}
        return _event_payload(
            session_id=self.session_id,
            workspace_root=self.workspace_root,
            changes=changes,
        )

    def _save_sync_bundle(
        self,
        bundle: dict[str, dict[str, Any]],
        *,
        session_id: str,
        output_id_prefix: str = "",
    ) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        for command, payload in bundle.items():
            output_id = None
            if output_id_prefix:
                output_id = f"{output_id_prefix}-{command}"
            saved = self.store.save_command_output(
                command=command,
                title=str(payload.get("title") or command.title()),
                subtitle=str(payload.get("subtitle") or ""),
                body=str(payload.get("body") or ""),
                workspace_root=str(self.workspace_root),
                session_id=session_id,
                status="ok" if payload.get("body") else "error",
                metadata=dict(payload.get("metadata") or {}),
                output_id=output_id,
            )
            self._log(f"[store] output saved: {saved}")
            paths[command] = saved
        return paths

    def run_startup_sync(self) -> dict[str, dict[str, Any]]:
        bundle = generate_sync_bundle(
            self.workspace_root,
            model=self.model,
            runtime=self.runtime,
            natural_language=self.natural_language,
            ignore_globs=self.ignore_globs,
            sync_kind="startup",
        )
        self._save_sync_bundle(bundle, session_id=self.session_id, output_id_prefix=f"startup-{self.session_id}")
        if self.on_analysis is not None:
            self.on_analysis({"bundle": bundle, "sync_kind": "startup"}, Path())
        return bundle

    def _process_event(self, event: dict) -> None:
        event_path = self.store.save_event(event)
        self._log(f"[store] event saved: {event_path}")
        if self.on_event is not None:
            self.on_event(event, event_path)
        if not self.analyze:
            return
        try:
            bundle = generate_sync_bundle(
                self.workspace_root,
                event=event,
                model=self.model,
                runtime=self.runtime,
                natural_language=self.natural_language,
                ignore_globs=self.ignore_globs,
                sync_kind="incremental",
            )
        except Exception as exc:
            error_payload = {
                "event_id": event["event_id"],
                "timestamp": time.time(),
                "error": format_runtime_provider_error(exc),
            }
            analysis_path = self.store.save_command_output(
                command="diff",
                title="Diff",
                subtitle=", ".join(change["path"] for change in event.get("changes", [])[:3]),
                workspace_root=str(self.workspace_root),
                session_id=event.get("session_id", ""),
                status="error",
                metadata={"event_id": event["event_id"], **error_payload},
            )
            self._log(f"[store] output saved: {analysis_path}")
            if self.on_analysis is not None:
                self.on_analysis(error_payload, analysis_path)
            return
        paths = self._save_sync_bundle(bundle, session_id=event.get("session_id", ""), output_id_prefix=event["event_id"])
        if self.on_analysis is not None:
            self.on_analysis({"bundle": bundle, "event_id": event["event_id"]}, paths.get("diff") or next(iter(paths.values()), Path()))

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> int:
        self._log(f"[store] watching {self.workspace_root}")
        try:
            while not self.stop_event.is_set():
                current_snapshot = collect_workspace_snapshot(
                    self.workspace_root,
                    max_file_bytes=self.max_file_bytes,
                    ignore_globs=self.ignore_globs,
                )
                changes = detect_changes(self._previous_snapshot, current_snapshot)
                self._previous_snapshot = current_snapshot
                if changes:
                    self._merge_changes(changes)
                event = self._flush_ready(force=self.once)
                if event is not None:
                    self._process_event(event)
                    if self.once:
                        return 0
                if self.once and not changes:
                    return 0
                self.stop_event.wait(self.poll_interval)
        except KeyboardInterrupt:
            event = self._flush_ready(force=True)
            if event is not None:
                self._process_event(event)
            self._log("\n[store] stopped")
            return 0


def run_codex_watch(args: argparse.Namespace) -> int:
    workspace_root = Path(args.path or ".").expanduser().resolve()
    if not workspace_root.exists():
        print(f"[store] workspace not found: {workspace_root}")
        return 1
    if not workspace_root.is_dir():
        print(f"[store] workspace is not a directory: {workspace_root}")
        return 1
    watcher = CodexCompanionWatcher(
        workspace_root,
        poll_interval=args.poll_interval,
        debounce_seconds=args.debounce_seconds,
        analyze=not args.no_analyze,
        once=args.once,
        max_file_bytes=args.max_file_bytes,
    )
    return watcher.run()


def build_arg_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "store",
        aliases=["codex-watch"],
        help="Watch workspace diffs and store readable review outputs",
        description="Poll the current workspace, batch nearby file saves into a change-set, and optionally ask Hermes to analyze them while saving readable JSON records under ~/.hermes/store.",
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=".",
        help="Workspace root to watch (default: current directory)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between workspace scans (default: 1.0)",
    )
    parser.add_argument(
        "--debounce-seconds",
        type=float,
        default=2.0,
        help="Quiet period before emitting a batched change-set (default: 2.0)",
    )
    parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help="Skip files larger than this many bytes (default: 200000)",
    )
    parser.add_argument(
        "--no-analyze",
        action="store_true",
        help="Only persist change-sets; skip Hermes analysis",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan/flush cycle and exit",
    )
    parser.set_defaults(func=run_codex_watch)


__all__ = [
    "CompanionStore",
    "CodexCompanionWatcher",
    "HermesStore",
    "PendingChange",
    "analyze_prompt",
    "build_arg_parser",
    "build_directory_explanation_prompt",
    "build_diff_explanation_prompt",
    "build_diff_text",
    "build_file_explanation_prompt",
    "build_project_review_prompt",
    "build_sync_planner_prompt",
    "collect_directory_context",
    "collect_project_summary",
    "collect_workspace_snapshot",
    "collect_related_context",
    "detect_changes",
    "explain_file",
    "extract_promotion_candidates",
    "find_symbol_candidates",
    "generate_sync_bundle",
    "plan_sync_targets",
    "resolve_analysis_runtime",
    "run_codex_watch",
]

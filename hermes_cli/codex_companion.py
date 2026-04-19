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
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from hermes_cli.config import ensure_hermes_home, get_hermes_home, load_config
from hermes_cli.runtime_provider import (
    format_runtime_provider_error,
    resolve_runtime_provider,
)


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


@dataclass(frozen=True)
class TargetSnapshot:
    target_path: str
    kind: str
    exists: bool
    digest: str
    entries: tuple[str, ...] = ()


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
    """Promotion candidate generation is disabled.

    The previous heuristic produced low-quality one-line candidates that were
    noisier than the analyses themselves. Keep the public helper as a no-op so
    older callers and tests do not break.
    """
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


def collect_target_file_snapshots(
    workspace_root: Path,
    *,
    target_path: str,
    kind: str,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Dict[str, FileSnapshot]:
    """Collect readable file snapshots under a watched explain/flow target.

    Unlike collect_workspace_snapshot(target_dir), returned keys are always
    workspace-relative paths so diffs and metadata stay stable when watching a
    subdirectory.
    """
    target = (workspace_root / target_path).resolve()
    snapshots: Dict[str, FileSnapshot] = {}

    if kind == "file":
        if not target.exists() or not target.is_file():
            return snapshots
        text = _load_text_file(target, max_file_bytes=max_file_bytes)
        if text is None:
            return snapshots
        stat = target.stat()
        try:
            rel_path = str(target.relative_to(workspace_root))
        except ValueError:
            rel_path = str(target_path)
        snapshots[rel_path] = FileSnapshot(
            rel_path=rel_path,
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            digest=_sha256_text(text),
            content=text,
        )
        return snapshots

    if kind == "directory":
        if not target.exists() or not target.is_dir():
            return snapshots
        nested = collect_workspace_snapshot(target, max_file_bytes=max_file_bytes)
        for rel_path, snap in nested.items():
            full_path = target / rel_path
            try:
                workspace_rel = str(full_path.relative_to(workspace_root))
            except ValueError:
                workspace_rel = str(Path(target_path) / rel_path)
            snapshots[workspace_rel] = FileSnapshot(
                rel_path=workspace_rel,
                mtime_ns=snap.mtime_ns,
                size=snap.size,
                digest=snap.digest,
                content=snap.content,
            )
        return snapshots

    raise ValueError(f"Unsupported target snapshot kind: {kind}")


def collect_target_snapshot(
    workspace_root: Path,
    *,
    target_path: str,
    kind: str,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> TargetSnapshot:
    target = (workspace_root / target_path).resolve()
    normalized_path = str(target_path)

    if kind == "file":
        if not target.exists() or not target.is_file():
            return TargetSnapshot(
                target_path=normalized_path,
                kind="file",
                exists=False,
                digest="missing",
            )
        text = _load_text_file(target, max_file_bytes=max_file_bytes)
        if text is None:
            return TargetSnapshot(
                target_path=normalized_path,
                kind="file",
                exists=True,
                digest="unreadable",
            )
        digest = _sha256_text(text)
        return TargetSnapshot(
            target_path=normalized_path,
            kind="file",
            exists=True,
            digest=digest,
        )

    if kind == "directory":
        if not target.exists() or not target.is_dir():
            return TargetSnapshot(
                target_path=normalized_path,
                kind="directory",
                exists=False,
                digest="missing",
            )
        snapshots = collect_workspace_snapshot(target, max_file_bytes=max_file_bytes)
        entries = tuple(
            f"{rel_path}:{snap.digest}:{snap.size}"
            for rel_path, snap in sorted(snapshots.items())
        )
        digest = _sha256_text("\n".join(entries))
        return TargetSnapshot(
            target_path=normalized_path,
            kind="directory",
            exists=True,
            digest=digest,
            entries=entries,
        )

    raise ValueError(f"Unsupported target snapshot kind: {kind}")


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


_SUPPRESSED_GIT_REFLOG_PREFIXES = (
    "checkout:",
    "reset:",
    "restore:",
    "rebase",
    "merge",
    "pull",
)


def _resolve_git_dir(root: Path) -> Optional[Path]:
    """Return the git metadata directory for root or one of its parents."""
    current = root.resolve()
    candidates = (current, *current.parents)
    for candidate in candidates:
        dotgit = candidate / ".git"
        if dotgit.is_dir():
            return dotgit
        if dotgit.is_file():
            try:
                content = dotgit.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            prefix = "gitdir:"
            if content.lower().startswith(prefix):
                raw = content[len(prefix):].strip()
                git_dir = Path(raw)
                if not git_dir.is_absolute():
                    git_dir = (candidate / git_dir).resolve()
                if git_dir.exists():
                    return git_dir
    return None


def _read_git_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_git_log_tail(path: Path) -> tuple[int, int, str]:
    try:
        stat = path.stat()
        with path.open("rb") as fh:
            if stat.st_size > 4096:
                fh.seek(stat.st_size - 4096)
            raw = fh.read()
    except OSError:
        return (0, 0, "")
    text = raw.decode("utf-8", errors="replace")
    lines = [line for line in text.splitlines() if line.strip()]
    return (stat.st_size, stat.st_mtime_ns, lines[-1] if lines else "")


def _git_operation_marker(root: Path) -> dict[str, Any]:
    git_dir = _resolve_git_dir(root)
    if git_dir is None:
        return {}
    return {
        "git_dir": str(git_dir),
        "head": _read_git_text(git_dir / "HEAD"),
        "head_log": _read_git_log_tail(git_dir / "logs" / "HEAD"),
        "stash_log": _read_git_log_tail(git_dir / "logs" / "refs" / "stash"),
    }


def _is_suppressed_git_reflog_line(line: str) -> bool:
    lowered = line.lower()
    return any(token in lowered for token in _SUPPRESSED_GIT_REFLOG_PREFIXES)


def _git_operation_marker_changed(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    if not previous or not current:
        return False
    if previous.get("git_dir") != current.get("git_dir"):
        return False
    if previous.get("head") != current.get("head"):
        return True
    previous_head_log = previous.get("head_log")
    current_head_log = current.get("head_log")
    if previous_head_log != current_head_log:
        line = str(current_head_log[-1] if current_head_log else "")
        if _is_suppressed_git_reflog_line(line):
            return True
    if previous.get("stash_log") != current.get("stash_log"):
        return True
    return False


def _changes_have_git_status_delta(root: Path, changes: Iterable[PendingChange]) -> bool:
    """Return False when changed paths are clean relative to git status.

    This suppresses automatic reviews for branch checkout/stash operations that
    rewrite files but leave no current worktree delta to review.
    """
    paths = [change.path for change in changes]
    if not paths:
        return False
    if _resolve_git_dir(root) is None:
        return True
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--", *paths],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


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
        return path

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

    def load_latest_analysis(self) -> Optional[dict]:
        payload = self.load_latest_output(command="review", title="Diff Review")
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


def _default_analysis_model() -> str:
    config = load_config()
    model_cfg = config.get("model")
    if isinstance(model_cfg, str) and model_cfg.strip():
        return model_cfg.strip()
    if isinstance(model_cfg, dict):
        return str(model_cfg.get("default") or "anthropic/claude-opus-4.6")
    return "anthropic/claude-opus-4.6"


def _code_evidence_instruction(language: Optional[str]) -> str:
    if _normalize_natural_language(language) == "ja":
        return (
            "各セクションでは、説明対象の直前に該当コードを短い fenced code block として添付してください。"
            "コード引用には file path や symbol 名の見出しを付け、必要最小限の行だけを引用してください。"
            "diff 由来の review では、変更された行や周辺の短い hunk を優先してください。"
        )
    return (
        "In each section, attach the relevant code before the explanation as a short fenced code block. "
        "Label each excerpt with its file path or symbol, and quote only the minimum lines needed. "
        "For diff reviews, prefer changed lines or a short surrounding hunk."
    )


def _build_analysis_prompt(event: dict, *, natural_language: Optional[str] = None) -> str:
    event = _prune_event(event)
    spec = _language_spec(natural_language)
    guidance = spec["review_guidance"]
    lines = [
        f"You are reviewing a code change-set produced by a coding agent for a developer in {spec['name']}.",
        "Use the diff as the primary evidence. Only infer intent when strongly supported.",
        "If you need extra context, you may read the changed files and related files, but stay focused.",
        _code_evidence_instruction(natural_language),
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
        _code_evidence_instruction(natural_language),
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
        _code_evidence_instruction(natural_language),
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


def build_incremental_explanation_prompt(
    workspace_root: Path,
    *,
    command_name: str,
    title: str,
    subtitle: str,
    target_path: str,
    kind: str,
    changes: Sequence[dict[str, Any]],
    previous_output: Optional[dict[str, Any]] = None,
    symbol: str = "",
    related_files: Optional[Sequence[dict[str, str]]] = None,
    natural_language: Optional[str] = None,
) -> str:
    spec = _language_spec(natural_language)
    previous_text = ""
    if previous_output:
        previous_text = str(previous_output.get("content", {}).get("text") or "").strip()
    previous_text = _truncate_text(previous_text, 10_000) if previous_text else "(no previous explanation available)"

    lines = [
        f"You are incrementally updating a prior {command_name} explanation in {spec['name']}.",
        "Use only the previous explanation and the changed hunks below unless a listed related excerpt is essential.",
        "Do not re-read or re-explain unchanged code. Update conclusions only where the diff changes behavior, responsibility, control flow, risk, or improvement suggestions.",
        "Return a complete updated explanation, not a patch note, using the same section structure as the prior explanation.",
        "If the diff does not affect a section, keep that section concise and preserve the prior conclusion.",
        _code_evidence_instruction(natural_language),
        "",
        f"Return exactly these sections in {spec['name']}:",
        *(spec["directory_sections"] if kind == "directory" else spec["file_sections"]),
        "",
        f"Workspace: {workspace_root}",
        f"Command: {command_name}",
        f"Title: {title}",
        f"Subtitle: {subtitle}",
        f"Target {kind}: {target_path}",
    ]
    if symbol:
        lines.append(f"Focus symbol: {symbol}")

    lines.extend([
        "",
        "Previous explanation to update:",
        previous_text,
        "",
        f"Changed files ({len(changes)}):",
    ])
    for change in changes:
        lines.extend([
            "",
            f"### {change.get('change_type', 'modified')}: {change.get('path', '')}",
            str(change.get("diff_text") or "[no diff text]"),
        ])

    if related_files:
        lines.extend(["", "Related files (truncated excerpts; use only if needed):"])
        for item in related_files:
            lines.extend(["", f"### related: {item['path']}", item["content"]])
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


def analyze_prompt(
    prompt: str,
    *,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    runtime: Optional[dict[str, Any]] = None,
    enabled_toolsets: Optional[list[str]] = None,
) -> dict:
    from run_agent import AIAgent

    runtime = runtime or resolve_runtime_provider()
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=model or _default_analysis_model(),
        enabled_toolsets=["file", "session_search"] if enabled_toolsets is None else enabled_toolsets,
        quiet_mode=True,
        platform="cli",
        session_id=session_id or f"store-{uuid.uuid4().hex}",
        skip_memory=True,
    )
    result = agent.run_conversation(prompt)
    response_text = result.get("final_response") if isinstance(result, dict) else str(result)
    return {
        "model": model or _default_analysis_model(),
        "provider": runtime.get("provider"),
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
    )
    return {
        "event_id": event["event_id"],
        "model": model or _default_analysis_model(),
        "provider": result.get("provider"),
        "analysis": result.get("analysis", ""),
        "timestamp": result.get("timestamp", time.time()),
        "related_files": event["related_files"],
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
        natural_language: Optional[str] = None,
        on_event: Optional[Callable[[dict, Path], None]] = None,
        on_analysis: Optional[Callable[[dict, Path], None]] = None,
        stop_event: Optional[threading.Event] = None,
        suppress_git_operation_events: bool = True,
    ):
        self.workspace_root = workspace_root.resolve()
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds
        self.analyze = analyze
        self.once = once
        self.max_file_bytes = max_file_bytes
        self.ignore_globs = _normalize_ignore_globs(ignore_globs)
        self.runtime = runtime
        self.natural_language = _normalize_natural_language(natural_language)
        self.on_event = on_event
        self.on_analysis = on_analysis
        self.stop_event = stop_event or threading.Event()
        self.suppress_git_operation_events = suppress_git_operation_events
        self.session_id = uuid.uuid4().hex
        self.store = HermesStore()
        self._previous_snapshot = collect_workspace_snapshot(
            self.workspace_root,
            max_file_bytes=self.max_file_bytes,
            ignore_globs=self.ignore_globs,
        )
        self._pending: Dict[str, PendingChange] = {}
        self._git_operation_marker = _git_operation_marker(self.workspace_root)

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

    def _process_event(self, event: dict) -> None:
        event_path = self.store.save_event(event)
        print(f"[store] event saved: {event_path}")
        if self.on_event is not None:
            self.on_event(event, event_path)
        if not self.analyze:
            return
        try:
            analysis = analyze_change_set(
                event,
                runtime=self.runtime,
                natural_language=self.natural_language,
                ignore_globs=self.ignore_globs,
            )
        except Exception as exc:
            error_payload = {
                "event_id": event["event_id"],
                "timestamp": time.time(),
                "error": format_runtime_provider_error(exc),
            }
            analysis_path = self.store.save_command_output(
                command="review",
                title="Diff Review",
                subtitle=", ".join(change["path"] for change in event.get("changes", [])[:3]),
                workspace_root=str(self.workspace_root),
                session_id=event.get("session_id", ""),
                status="error",
                metadata={"event_id": event["event_id"], **error_payload},
            )
            print(f"[store] output saved: {analysis_path}")
            if self.on_analysis is not None:
                self.on_analysis(error_payload, analysis_path)
            return
        changed = ", ".join(change["path"] for change in event.get("changes", [])[:3])
        if len(event.get("changes", [])) > 3:
            changed += ", ..."
        analysis_path = self.store.save_command_output(
            command="review",
            title="Diff Review",
            subtitle=changed,
            body=analysis.get("analysis", ""),
            workspace_root=str(self.workspace_root),
            session_id=event.get("session_id", ""),
            status="ok",
            metadata={
                "event_id": event["event_id"],
                "provider": analysis.get("provider"),
                "model": analysis.get("model"),
                "related_files": analysis.get("related_files", []),
            },
            output_id=event["event_id"],
        )
        print(f"[store] output saved: {analysis_path}")
        if self.on_analysis is not None:
            self.on_analysis(analysis, analysis_path)

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> int:
        print(f"[store] watching {self.workspace_root}")
        try:
            while not self.stop_event.is_set():
                current_snapshot = collect_workspace_snapshot(
                    self.workspace_root,
                    max_file_bytes=self.max_file_bytes,
                    ignore_globs=self.ignore_globs,
                )
                changes = detect_changes(self._previous_snapshot, current_snapshot)
                current_git_marker = _git_operation_marker(self.workspace_root)
                suppress_for_git_operation = False
                if changes and self.suppress_git_operation_events:
                    if _git_operation_marker_changed(self._git_operation_marker, current_git_marker):
                        suppress_for_git_operation = True
                    elif not _changes_have_git_status_delta(self.workspace_root, changes.values()):
                        suppress_for_git_operation = True
                self._previous_snapshot = current_snapshot
                self._git_operation_marker = current_git_marker
                if suppress_for_git_operation:
                    self._pending = {}
                    changes = {}
                elif changes:
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
            print("\n[store] stopped")
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
    "build_diff_text",
    "build_file_explanation_prompt",
    "collect_directory_context",
    "collect_workspace_snapshot",
    "collect_related_context",
    "collect_target_snapshot",
    "detect_changes",
    "_changes_have_git_status_delta",
    "_git_operation_marker_changed",
    "explain_file",
    "extract_promotion_candidates",
    "find_symbol_candidates",
    "run_codex_watch",
]

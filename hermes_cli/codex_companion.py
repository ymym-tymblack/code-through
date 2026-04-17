"""Workspace diff watcher for Codex companion workflows.

MVP scope:
- Poll a workspace for text-file changes
- Batch nearby saves into a single change-set
- Persist raw diff events under ~/.hermes/codex_companion/
- Optionally send each change-set to Hermes for natural-language analysis
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional

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


def collect_workspace_snapshot(
    root: Path,
    *,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> Dict[str, FileSnapshot]:
    snapshots: Dict[str, FileSnapshot] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        current_dir = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_IGNORE_DIRS]
        for filename in filenames:
            path = current_dir / filename
            if should_ignore_path(path, root):
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


class CompanionStore:
    def __init__(self, root: Optional[Path] = None):
        ensure_hermes_home()
        self.root = root or (get_hermes_home() / "codex_companion")
        self.events_dir = self.root / "events"
        self.analysis_dir = self.root / "analysis"
        self.events_dir.mkdir(parents=True, exist_ok=True)
        self.analysis_dir.mkdir(parents=True, exist_ok=True)

    def save_event(self, payload: dict) -> Path:
        event_id = payload["event_id"]
        path = self.events_dir / f"{event_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def save_analysis(self, event_id: str, payload: dict) -> Path:
        path = self.analysis_dir / f"{event_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


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


def _build_analysis_prompt(event: dict) -> str:
    lines = [
        "You are reviewing a code change-set produced by a coding agent.",
        "Use the diff as the primary evidence. Only infer intent when strongly supported.",
        "If you need extra context, you may read the changed files, but stay focused.",
        "",
        "Return exactly these sections in Japanese:",
        "## 変更説明",
        "## リスク",
        "## 改善提案",
        "",
        "In 変更説明, describe processing flow, major functions, and responsibility changes.",
        "In リスク, call out likely bugs, regressions, and missing edge cases.",
        "In 改善提案, suggest concrete follow-up improvements or tests.",
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
    return "\n".join(lines)


def analyze_change_set(
    event: dict,
    *,
    model: Optional[str] = None,
) -> dict:
    from run_agent import AIAgent

    runtime = resolve_runtime_provider()
    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        model=model or _default_analysis_model(),
        enabled_toolsets=["file", "session_search"],
        quiet_mode=True,
        platform="cli",
        session_id=f"codex-companion-{event['event_id']}",
        skip_memory=True,
    )
    prompt = _build_analysis_prompt(event)
    result = agent.run_conversation(prompt)
    response_text = result.get("final_response") if isinstance(result, dict) else str(result)
    return {
        "event_id": event["event_id"],
        "model": model or _default_analysis_model(),
        "provider": runtime.get("provider"),
        "analysis": response_text or "",
        "timestamp": time.time(),
    }


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
    ):
        self.workspace_root = workspace_root.resolve()
        self.poll_interval = poll_interval
        self.debounce_seconds = debounce_seconds
        self.analyze = analyze
        self.once = once
        self.max_file_bytes = max_file_bytes
        self.session_id = uuid.uuid4().hex
        self.store = CompanionStore()
        self._previous_snapshot = collect_workspace_snapshot(
            self.workspace_root,
            max_file_bytes=self.max_file_bytes,
        )
        self._pending: Dict[str, PendingChange] = {}

    def _merge_changes(self, changes: Dict[str, PendingChange]) -> None:
        for rel_path, change in changes.items():
            existing = self._pending.get(rel_path)
            if existing is None:
                self._pending[rel_path] = change
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
        print(f"[codex-watch] change-set saved: {event_path}")
        if not self.analyze:
            return
        try:
            analysis = analyze_change_set(event)
        except Exception as exc:
            error_payload = {
                "event_id": event["event_id"],
                "timestamp": time.time(),
                "error": format_runtime_provider_error(exc),
            }
            analysis_path = self.store.save_analysis(event["event_id"], error_payload)
            print(f"[codex-watch] analysis failed: {analysis_path}")
            print(error_payload["error"])
            return
        analysis_path = self.store.save_analysis(event["event_id"], analysis)
        print(f"[codex-watch] analysis saved: {analysis_path}")
        print(analysis["analysis"])

    def run(self) -> int:
        print(f"[codex-watch] watching {self.workspace_root}")
        try:
            while True:
                current_snapshot = collect_workspace_snapshot(
                    self.workspace_root,
                    max_file_bytes=self.max_file_bytes,
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
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            event = self._flush_ready(force=True)
            if event is not None:
                self._process_event(event)
            print("\n[codex-watch] stopped")
            return 0


def run_codex_watch(args: argparse.Namespace) -> int:
    workspace_root = Path(args.path or ".").expanduser().resolve()
    if not workspace_root.exists():
        print(f"[codex-watch] workspace not found: {workspace_root}")
        return 1
    if not workspace_root.is_dir():
        print(f"[codex-watch] workspace is not a directory: {workspace_root}")
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
        "codex-watch",
        help="Watch the workspace for text-file diffs and analyze them with Hermes",
        description="Poll the current workspace, batch nearby file saves into a change-set, and optionally ask Hermes to explain risks and improvements.",
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
    "PendingChange",
    "build_arg_parser",
    "build_diff_text",
    "collect_workspace_snapshot",
    "detect_changes",
    "run_codex_watch",
]

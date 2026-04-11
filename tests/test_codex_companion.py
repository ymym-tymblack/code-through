import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

from hermes_cli.codex_companion import (
    CodexCompanionWatcher,
    HermesStore,
    PendingChange,
    build_diff_text,
    build_directory_explanation_prompt,
    build_file_explanation_prompt,
    collect_related_context,
    collect_workspace_snapshot,
    detect_changes,
    extract_promotion_candidates,
    resolve_analysis_runtime,
    run_codex_watch,
)


def test_collect_workspace_snapshot_skips_ignored_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(tmp_path)

    assert "src/main.py" in snapshot
    assert ".git/ignored.txt" not in snapshot


def test_collect_workspace_snapshot_skips_workspace_bridge_dir(tmp_path):
    (tmp_path / ".hermes" / "companion").mkdir(parents=True)
    (tmp_path / ".hermes" / "companion" / "latest.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(tmp_path)

    assert "src/main.py" in snapshot
    assert ".hermes/companion/latest.json" not in snapshot


def test_collect_workspace_snapshot_skips_review_exclude_globs(tmp_path):
    (tmp_path / "memo").mkdir()
    (tmp_path / "memo" / "note.md").write_text("todo\n", encoding="utf-8")
    (tmp_path / "memo" / "keep.py").write_text("print('skip dir')\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(tmp_path, ignore_globs=["memo/**"])

    assert "src/main.py" in snapshot
    assert "memo/note.md" not in snapshot
    assert "memo/keep.py" not in snapshot


def test_detect_changes_handles_created_modified_deleted(tmp_path):
    (tmp_path / "modified.py").write_text("v1\n", encoding="utf-8")
    (tmp_path / "deleted.py").write_text("gone\n", encoding="utf-8")
    previous = collect_workspace_snapshot(tmp_path)

    (tmp_path / "modified.py").write_text("v2\n", encoding="utf-8")
    (tmp_path / "deleted.py").unlink()
    (tmp_path / "created.py").write_text("fresh\n", encoding="utf-8")
    current = collect_workspace_snapshot(tmp_path)

    changes = detect_changes(previous, current, now_ts=123.0)

    assert set(changes) == {"modified.py", "deleted.py", "created.py"}
    assert changes["modified.py"].change_type == "modified"
    assert changes["deleted.py"].change_type == "deleted"
    assert changes["created.py"].change_type == "created"
    assert changes["created.py"].old_content == ""
    assert changes["deleted.py"].new_content == ""


def test_build_diff_text_uses_dev_null_for_created_and_deleted():
    created = build_diff_text("src/new.py", "", "print('x')\n")
    deleted = build_diff_text("src/old.py", "print('x')\n", "")

    assert "/dev/null" in created
    assert "b/src/new.py" in created
    assert "a/src/old.py" in deleted
    assert "/dev/null" in deleted


def test_companion_store_persists_event_and_analysis(tmp_path):
    store = HermesStore(root=tmp_path / "store")
    event = {"event_id": "evt1", "changes": []}
    analysis = {"event_id": "evt1", "analysis": "ok"}

    event_path = store.save_event(event)
    analysis_path = store.save_analysis("evt1", analysis)

    saved_event = json.loads(event_path.read_text(encoding="utf-8"))
    saved_output = json.loads(analysis_path.read_text(encoding="utf-8"))
    assert saved_event["event_id"] == "evt1"
    assert saved_event["kind"] == "diff_event"
    assert saved_output["command"] == "review"
    assert saved_output["content"]["text"] == "ok"
    assert saved_output["content"]["lines"] == ["ok"]


def test_companion_store_writes_workspace_bridge_files(tmp_path):
    store = HermesStore(root=tmp_path / "store")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with patch("hermes_cli.codex_companion.load_config", return_value={"analysis": {"bridge_enabled": True, "bridge_dir": ".hermes/companion"}}):
        store.save_command_output(
            command="explain",
            title="File Explain",
            body="body",
            workspace_root=str(workspace),
            metadata={"provider": "openrouter", "model": "gemma4:26b"},
        )

    latest_payload = json.loads((workspace / ".hermes" / "companion" / "latest.json").read_text(encoding="utf-8"))
    command_payload = json.loads((workspace / ".hermes" / "companion" / "explain.json").read_text(encoding="utf-8"))

    assert latest_payload["kind"] == "hermes_companion_output"
    assert latest_payload["command"] == "explain"
    assert latest_payload["content"]["text"] == "body"
    assert command_payload["metadata"]["model"] == "gemma4:26b"


def test_run_codex_watch_rejects_missing_workspace(tmp_path):
    args = Namespace(
        path=str(tmp_path / "missing"),
        poll_interval=1.0,
        debounce_seconds=2.0,
        no_analyze=True,
        once=True,
        max_file_bytes=200_000,
    )

    assert run_codex_watch(args) == 1


def test_merge_changes_drops_create_then_delete(tmp_path):
    watcher = CodexCompanionWatcher(tmp_path, analyze=False, once=True)
    watcher._pending["demo.py"] = PendingChange(
        path="demo.py",
        change_type="created",
        old_content="",
        new_content="v1\n",
        first_seen_at=1.0,
        updated_at=1.0,
    )
    watcher._merge_changes(
        {
            "demo.py": PendingChange(
                path="demo.py",
                change_type="deleted",
                old_content="v1\n",
                new_content="",
                first_seen_at=2.0,
                updated_at=2.0,
            )
        }
    )

    assert watcher._pending == {}


def test_merge_changes_turns_delete_then_recreate_into_modify(tmp_path):
    watcher = CodexCompanionWatcher(tmp_path, analyze=False, once=True)
    watcher._pending["demo.py"] = PendingChange(
        path="demo.py",
        change_type="deleted",
        old_content="before\n",
        new_content="",
        first_seen_at=1.0,
        updated_at=1.0,
    )
    watcher._merge_changes(
        {
            "demo.py": PendingChange(
                path="demo.py",
                change_type="created",
                old_content="",
                new_content="after\n",
                first_seen_at=2.0,
                updated_at=2.0,
            )
        }
    )

    change = watcher._pending["demo.py"]
    assert change.change_type == "modified"
    assert change.old_content == "before\n"
    assert change.new_content == "after\n"


def test_merge_changes_drops_create_modify_delete_sequence(tmp_path):
    watcher = CodexCompanionWatcher(tmp_path, analyze=False, once=True)
    watcher._pending["demo.py"] = PendingChange(
        path="demo.py",
        change_type="created",
        old_content="",
        new_content="v1\n",
        first_seen_at=1.0,
        updated_at=1.0,
    )
    watcher._merge_changes(
        {
            "demo.py": PendingChange(
                path="demo.py",
                change_type="modified",
                old_content="v1\n",
                new_content="v2\n",
                first_seen_at=2.0,
                updated_at=2.0,
            )
        }
    )
    watcher._merge_changes(
        {
            "demo.py": PendingChange(
                path="demo.py",
                change_type="deleted",
                old_content="v2\n",
                new_content="",
                first_seen_at=3.0,
                updated_at=3.0,
            )
        }
    )

    assert watcher._pending == {}


def test_collect_related_context_follows_python_imports(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("from pkg.helper import helper\n\nprint(helper())\n", encoding="utf-8")

    related = collect_related_context(tmp_path, changed_paths=["main.py"])

    assert related
    assert related[0]["path"] == "pkg/helper.py"
    assert "def helper" in related[0]["content"]


def test_build_file_explanation_prompt_defaults_to_english(tmp_path):
    prompt = build_file_explanation_prompt(tmp_path, target_path="src/app.py")

    assert "You are explaining source code to a developer in English." in prompt
    assert "Return exactly these sections in English:" in prompt
    assert "## Overview" in prompt
    assert "## Key Functions and Responsibilities" in prompt
    assert "## Control Flow" in prompt
    assert "## Improvement Opportunities" in prompt
    assert "## 概要" not in prompt


def test_build_directory_explanation_prompt_supports_japanese(tmp_path):
    prompt = build_directory_explanation_prompt(
        tmp_path,
        target_path="src",
        directory_context={"entries": [], "total_entries": 0},
        natural_language="ja",
    )

    assert "You are explaining a source directory to a developer in Japanese." in prompt
    assert "Return exactly these sections in Japanese:" in prompt
    assert "## 概要" in prompt
    assert "## 主要なファイルと責務" in prompt
    assert "## 処理フロー" in prompt
    assert "## 改善ポイント" in prompt
    assert "## Overview" not in prompt


def test_extract_promotion_candidates_prefers_skill_for_test_workflows():
    analysis = """## Change Summary
Updated the retry path.

## Control Flow
The retry path now preserves the previous user message.

## Risks
- Watch for duplicate retries if the queue is already populated.

## Improvement Suggestions
- Add a regression test covering queued retry behavior after a slash command.
"""

    candidates = extract_promotion_candidates(
        analysis,
        command_name="review",
        metadata={"target_path": "cli.py"},
    )

    assert len(candidates) >= 2
    assert candidates[0]["source_paths"] == ["cli.py"]
    assert any(candidate["suggested_target"] == "skill" for candidate in candidates)
    assert any("regression test" in candidate["summary"].lower() for candidate in candidates)


def test_companion_store_loads_latest_saved_artifacts(tmp_path):
    store = HermesStore(root=tmp_path / "store")
    event = {"event_id": "evt2", "changes": [{"path": "demo.py"}]}
    analysis = {"event_id": "evt2", "analysis": "ok"}

    store.save_event(event)
    store.save_analysis("evt2", analysis)

    assert store.load_latest_event()["event_id"] == "evt2"
    assert store.load_latest_analysis()["analysis"] == "ok"


def test_process_event_saves_sync_bundle_without_printing_full_body(tmp_path, capsys):
    watcher = CodexCompanionWatcher(tmp_path, analyze=True, once=True)
    watcher.store = HermesStore(root=tmp_path / "store")
    event = {"event_id": "evt3", "workspace_root": str(tmp_path), "changes": []}

    bundle = {
        "flow": {"title": "Flow", "subtitle": "main", "body": "flow body", "metadata": {}},
        "explain": {"title": "Explain", "subtitle": "app.py", "body": "explain body", "metadata": {}},
        "review": {"title": "Code Review", "subtitle": "quality", "body": "full review body", "metadata": {}},
        "diff": {"title": "Diff", "subtitle": "app.py", "body": "diff body", "metadata": {"promotion_candidates": [{"summary": "save me"}]}} ,
    }

    with patch("hermes_cli.codex_companion.generate_sync_bundle", return_value=bundle):
        watcher._process_event(event)

    output = capsys.readouterr().out
    assert "[store] output saved:" in output
    assert "full review body" not in output
    payload = watcher.store.load_latest_output(command="diff", title="Diff")
    assert payload["metadata"]["promotion_candidates"] == [{"summary": "save me"}]


def test_store_command_output_writes_readable_multiline_json(tmp_path):
    store = HermesStore(root=tmp_path / "store")

    output_path = store.save_command_output(
        command="flow",
        title="Flow Explain",
        subtitle="forward @ nanochat/gpt.py",
        body="## 概要\nline one\nline two",
        workspace_root="/workspace/demo",
        session_id="sess1",
        metadata={"symbol": "forward"},
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["kind"] == "command_output"
    assert payload["command"] == "flow"
    assert payload["content"]["lines"] == ["## 概要", "line one", "line two"]
    assert payload["metadata"]["symbol"] == "forward"


def test_store_migrates_legacy_codex_companion_directory(tmp_path):
    hermes_home = tmp_path / ".hermes"
    legacy_root = hermes_home / "codex_companion"
    (legacy_root / "events").mkdir(parents=True)
    (legacy_root / "outputs").mkdir(parents=True)
    (legacy_root / "events" / "evt.json").write_text('{"event_id":"evt"}', encoding="utf-8")
    (legacy_root / "outputs" / "out.json").write_text('{"output_id":"out"}', encoding="utf-8")

    with patch("hermes_cli.codex_companion.get_hermes_home", return_value=hermes_home):
        store = HermesStore()

    assert store.root == hermes_home / "store"
    assert (hermes_home / "store" / "events" / "evt.json").exists()
    assert (hermes_home / "store" / "outputs" / "out.json").exists()
    assert not legacy_root.exists()

def test_resolve_analysis_runtime_prefers_analysis_config(monkeypatch):
    monkeypatch.setenv("LOCAL_GEMMA_KEY", "gemma-key")

    with patch(
        "hermes_cli.codex_companion.load_config",
        return_value={
            "analysis": {
                "enabled": True,
                "provider": "openrouter",
                "model": "gemma4:26b",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key_env": "LOCAL_GEMMA_KEY",
            }
        },
    ):
        runtime = resolve_analysis_runtime(
            fallback_runtime={"provider": "openai-codex", "base_url": "https://example.com", "api_key": "fallback", "api_mode": "codex_responses"},
            requested_provider="openai-codex",
        )

    assert runtime["provider"] == "openrouter"
    assert runtime["base_url"] == "http://127.0.0.1:11434/v1"
    assert runtime["api_key"] == "gemma-key"
    assert runtime["source"] == "analysis-config"


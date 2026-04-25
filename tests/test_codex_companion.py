import json
import subprocess
from argparse import Namespace
from unittest.mock import MagicMock, patch

from hermes_cli.codex_companion import (
    CodexCompanionWatcher,
    HermesStore,
    PendingChange,
    build_diff_text,
    build_directory_explanation_prompt,
    build_file_diff_prompt,
    build_file_explanation_prompt,
    build_incremental_explanation_prompt,
    collect_related_context,
    collect_target_file_snapshots,
    collect_target_snapshot,
    collect_workspace_snapshot,
    detect_changes,
    _changes_have_git_status_delta,
    _git_operation_marker_changed,
    extract_promotion_candidates,
    run_codex_watch,
)




def test_collect_target_file_snapshots_uses_workspace_relative_paths(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (tmp_path / "pkg" / "b.py").write_text("print('b')\n", encoding="utf-8")

    snapshots = collect_target_file_snapshots(tmp_path, target_path="pkg", kind="directory")

    assert sorted(snapshots) == ["pkg/a.py", "pkg/b.py"]
    assert snapshots["pkg/a.py"].content == "print('a')\n"


def test_build_incremental_explanation_prompt_uses_previous_output_and_diff_only(tmp_path):
    prompt = build_incremental_explanation_prompt(
        tmp_path,
        command_name="explain",
        title="File Explain",
        subtitle="app.py",
        target_path="app.py",
        kind="file",
        previous_output={"content": {"text": "## Overview\nOld explanation"}},
        changes=[
            {
                "path": "app.py",
                "change_type": "modified",
                "diff_text": "@@ -1 +1 @@\n-old\n+new\n",
            }
        ],
        natural_language="en",
    )

    assert "incrementally updating" in prompt
    assert "Previous explanation to update:" in prompt
    assert "Old explanation" in prompt
    assert "@@ -1 +1 @@" in prompt
    assert "Do not re-read or re-explain unchanged code" in prompt
    assert "attach the relevant code" in prompt
    assert "## Overview" in prompt

def test_collect_workspace_snapshot_skips_ignored_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "ignored.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    snapshot = collect_workspace_snapshot(tmp_path)

    assert "src/main.py" in snapshot
    assert ".git/ignored.txt" not in snapshot


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


def test_collect_target_snapshot_detects_file_changes(tmp_path):
    target = tmp_path / "demo.py"
    target.write_text("print(1)\n", encoding="utf-8")

    before = collect_target_snapshot(tmp_path, target_path="demo.py", kind="file")
    target.write_text("print(2)\n", encoding="utf-8")
    after = collect_target_snapshot(tmp_path, target_path="demo.py", kind="file")

    assert before.exists is True
    assert after.exists is True
    assert before != after


def test_collect_target_snapshot_detects_directory_changes(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("print(1)\n", encoding="utf-8")

    before = collect_target_snapshot(tmp_path, target_path="src", kind="directory")
    (src / "helper.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    after = collect_target_snapshot(tmp_path, target_path="src", kind="directory")

    assert before.exists is True
    assert after.exists is True
    assert before != after


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


def test_changes_have_git_status_delta_false_after_checkout_revert(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    target = tmp_path / "demo.py"
    target.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "demo.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    target.write_text("v2\n", encoding="utf-8")
    before = collect_workspace_snapshot(tmp_path)
    subprocess.run(["git", "checkout", "--", "demo.py"], cwd=tmp_path, check=True)
    after = collect_workspace_snapshot(tmp_path)
    changes = detect_changes(before, after, now_ts=123.0)

    assert changes
    assert _changes_have_git_status_delta(tmp_path, changes.values()) is False


def test_changes_have_git_status_delta_true_for_manual_edit(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
    target = tmp_path / "demo.py"
    target.write_text("v1\n", encoding="utf-8")
    subprocess.run(["git", "add", "demo.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    before = collect_workspace_snapshot(tmp_path)
    target.write_text("v2\n", encoding="utf-8")
    after = collect_workspace_snapshot(tmp_path)
    changes = detect_changes(before, after, now_ts=123.0)

    assert changes
    assert _changes_have_git_status_delta(tmp_path, changes.values()) is True


def test_git_operation_marker_changed_detects_checkout_and_stash():
    previous = {
        "git_dir": "/repo/.git",
        "head": "ref: refs/heads/main\n",
        "head_log": (10, 1, "commit: init"),
        "stash_log": (0, 0, ""),
    }
    checkout = {
        **previous,
        "head_log": (30, 2, "abc def User <u@example.com> 1 +0000\tcheckout: moving from main to feature"),
    }
    stash = {
        **previous,
        "stash_log": (20, 3, "abc def User <u@example.com> 1 +0000\tWIP on main: init"),
    }

    assert _git_operation_marker_changed(previous, checkout) is True
    assert _git_operation_marker_changed(previous, stash) is True


def test_build_analysis_prompt_requests_code_excerpts():
    from hermes_cli.codex_companion import _build_analysis_prompt

    prompt = _build_analysis_prompt(
        {
            "event_id": "evt",
            "workspace_root": "/workspace",
            "changes": [
                {
                    "path": "app.py",
                    "change_type": "modified",
                    "diff_text": "@@ -1 +1 @@\n-old\n+new\n",
                }
            ],
        },
        natural_language="en",
    )

    assert "attach the relevant code" in prompt
    assert "For diff reviews, prefer changed lines" in prompt


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
    saved_output = store.load_latest_output(command="review", title="Diff Review")
    assert analysis_path.suffix == ".md"
    assert saved_event["event_id"] == "evt1"
    assert saved_event["kind"] == "diff_event"
    assert saved_output["command"] == "review"
    assert saved_output["content"]["text"] == "ok"
    assert saved_output["content"]["lines"] == ["ok"]


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
    assert "attach the relevant code" in prompt
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
    assert "該当コードを短い fenced code block" in prompt
    assert "## 概要" in prompt
    assert "## 主要なファイルと責務" in prompt
    assert "## 処理フロー" in prompt
    assert "## 改善ポイント" in prompt
    assert "## Overview" not in prompt


def test_build_file_diff_prompt_requests_semantic_equivalence_judgment(tmp_path):
    prompt = build_file_diff_prompt(
        tmp_path,
        left_path="left.py",
        right_path="right.py",
        left_content="def run(x):\n    return x + 1\n",
        right_content="def run(value):\n    return value + 1\n",
        natural_language="en",
    )

    assert "semantic diff" in prompt
    assert "no meaningful behavioral difference" in prompt
    assert "## Behavioral Equivalence" in prompt
    assert "### left: left.py" in prompt
    assert "### right: right.py" in prompt


def test_extract_promotion_candidates_is_disabled():
    analysis = """## Risks
- Watch for duplicate retries.

## Improvement Suggestions
- Add a regression test covering queued retry behavior.
"""

    candidates = extract_promotion_candidates(
        analysis,
        command_name="review",
        metadata={"target_path": "cli.py"},
    )

    assert candidates == []


def test_companion_store_loads_latest_saved_artifacts(tmp_path):
    store = HermesStore(root=tmp_path / "store")
    event = {"event_id": "evt2", "changes": [{"path": "demo.py"}]}
    analysis = {"event_id": "evt2", "analysis": "ok"}

    store.save_event(event)
    store.save_analysis("evt2", analysis)

    assert store.load_latest_event()["event_id"] == "evt2"
    assert store.load_latest_analysis()["analysis"] == "ok"


def test_process_event_saves_analysis_without_printing_full_body(tmp_path, capsys):
    watcher = CodexCompanionWatcher(tmp_path, analyze=True, once=True)
    watcher.store = HermesStore(root=tmp_path / "store")
    event = {"event_id": "evt3", "workspace_root": str(tmp_path), "changes": []}

    with patch(
        "hermes_cli.codex_companion.analyze_change_set",
        return_value={"analysis": "full review body"},
    ):
        watcher._process_event(event)

    output = capsys.readouterr().out
    assert "[store] output saved:" in output
    assert "full review body" not in output
    payload = watcher.store.load_latest_output(command="review", title="Diff Review")
    assert "promotion_candidates" not in payload["metadata"]


def test_store_command_output_writes_readable_multiline_markdown(tmp_path):
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

    assert output_path.suffix == ".md"
    text = output_path.read_text(encoding="utf-8")
    assert text.startswith("<!-- codet-output")
    assert "# Flow Explain" in text
    assert "## 概要" in text
    payload = store.load_latest_output(command="flow", title="Flow Explain")
    assert payload["kind"] == "command_output"
    assert payload["command"] == "flow"
    assert payload["content"]["lines"] == ["## 概要", "line one", "line two"]
    assert payload["metadata"]["symbol"] == "forward"


def test_store_command_output_writes_to_workspace_codet_output_when_configured(tmp_path, monkeypatch):
    store = HermesStore(root=tmp_path / "store")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODET_OUTPUT_ROOT", str(workspace))

    output_path = store.save_command_output(
        command="review",
        title="Diff Review",
        body="review body",
        workspace_root=str(workspace),
        session_id="sess1",
    )

    assert output_path.parent == workspace / "codet-output" / "review"
    assert output_path.suffix == ".md"
    assert output_path.exists()
    payload = store.load_latest_output(command="review", title="Diff Review")
    assert payload["content"]["text"] == "review body"


def test_store_command_output_writes_diff_to_workspace_codet_output_when_configured(tmp_path, monkeypatch):
    store = HermesStore(root=tmp_path / "store")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODET_OUTPUT_ROOT", str(workspace))

    output_path = store.save_command_output(
        command="diff",
        title="Semantic Diff",
        body="## Comparison Summary\nEquivalent",
        workspace_root=str(workspace),
        session_id="sess1",
    )

    assert output_path.parent == workspace / "codet-output" / "diff"
    payload = store.load_latest_output(command="diff", title="Semantic Diff")
    assert payload["content"]["text"] == "## Comparison Summary\nEquivalent"


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

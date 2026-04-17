import json
from argparse import Namespace

from hermes_cli.codex_companion import (
    CompanionStore,
    build_diff_text,
    collect_workspace_snapshot,
    detect_changes,
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
    store = CompanionStore(root=tmp_path / "store")
    event = {"event_id": "evt1", "changes": []}
    analysis = {"event_id": "evt1", "analysis": "ok"}

    event_path = store.save_event(event)
    analysis_path = store.save_analysis("evt1", analysis)

    assert json.loads(event_path.read_text(encoding="utf-8"))["event_id"] == "evt1"
    assert json.loads(analysis_path.read_text(encoding="utf-8"))["analysis"] == "ok"


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

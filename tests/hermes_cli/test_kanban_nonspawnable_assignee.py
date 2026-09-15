"""Nonspawnable-assignee invariants (#101015 point 3, #99284, #74548 follow-up).

- Historical ghost assignees (emma, orchestrateur) never were Hermes profiles:
  cards created with them must canonicalise to default with a migration trace.
- Any other non-profile assignee stays creatable (control-plane lanes pull via
  claim_task) but is loudly warned at creation and flagged by diagnostics —
  unless declared in kanban.nonspawnable_assignee_allowlist (the escape hatch
  PR #74548 was missing).
- E2E throughout: real imports, real DB in a temp HERMES_HOME.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _created_event_payload(conn, task_id):
    row = conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' "
        "ORDER BY id ASC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert row is not None
    return json.loads(row["payload"])


def test_ghost_alias_canonicalizes_to_default():
    assert kb._canonical_assignee("emma") == "default"
    assert kb._canonical_assignee("Orchestrateur") == "default"
    assert kb._canonical_assignee("developer") == "developer"
    assert kb._canonical_assignee(None) is None


def test_create_task_migrates_ghost_alias_with_trace(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ghost alias", assignee="emma")
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["assignee"] == "default"
        payload = _created_event_payload(conn, tid)
        assert payload["assignee"] == "default"
        assert payload["assignee_migrated_from"] == "emma"


def test_unknown_lane_warns_but_persists_and_allowlist_mutes(kanban_home, monkeypatch, caplog):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "default")
    with kbc.connect_closing() as conn:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            tid = kb.create_task(conn, title="lane task", assignee="orion-cc")
        row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (tid,)).fetchone()
        assert row["assignee"] == "orion-cc"
        assert _created_event_payload(conn, tid)["assignee_not_a_profile"] is True
    assert any("orion-cc" in r.message for r in caplog.records)
    caplog.clear()
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  nonspawnable_assignee_allowlist:\n    - orion-cc\n", encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
            kb.create_task(conn, title="known lane", assignee="orion-cc")
    assert [r for r in caplog.records if "orion-cc" in r.message] == []


def test_diagnostics_flag_unknown_assignee_and_allowlist_mutes(monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: name == "default")
    task = {"id": "t_demo00", "title": "demo", "assignee": "ghost-lane",
            "status": "ready", "consecutive_failures": 0, "last_failure_error": None}
    flagged = [d for d in kd.compute_task_diagnostics(task, [], []) if d.kind == "nonspawnable_assignee"]
    assert len(flagged) == 1
    assert flagged[0].severity == "warning"
    assert "claim" in flagged[0].detail
    healthy = dict(task, assignee="default")
    assert [d for d in kd.compute_task_diagnostics(healthy, [], [])
            if d.kind == "nonspawnable_assignee"] == []
    muted = kd.compute_task_diagnostics(
        task, [], [], config={"kanban": {"nonspawnable_assignee_allowlist": ["ghost-lane"]}})
    assert [d for d in muted if d.kind == "nonspawnable_assignee"] == []


def test_dispatch_routes_unknown_lane_to_nonspawnable_bucket(kanban_home, monkeypatch):
    from hermes_cli import profiles

    monkeypatch.setattr(profiles, "profile_exists", lambda name: False)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="lane card", assignee="orion-cc")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert res.spawned == []
    assert res.skipped_nonspawnable == [tid]

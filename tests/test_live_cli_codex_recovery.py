"""Independent restart and pre-dispatch accounting regressions; fully offline."""
import json

import pytest
import yaml

from tests.test_live_cli_pipeline import LiveProject, happy_steps, run_one_live_slot
from xhs_discovery import app
from tests.test_source_cli import SCENARIOS, ClockStub, build_source, invoke, make_ctx
from xhs_discovery.config import load_settings
from xhs_discovery.models.enums import OperationState, RunStatus
from xhs_discovery.models.run import RunTermination
from xhs_discovery.storage import db
from xhs_discovery.storage.artifacts import recover_run
from xhs_discovery.storage.ledger import Ledger
from xhs_discovery.storage.repository import Repository
from xhs_discovery.storage.worker import OsGuard


@pytest.mark.parametrize('persisted', [5000.0, float('inf'), float('nan')])
def test_rebooted_clock_never_waits_old_uptime(tmp_path, persisted):
    scenario = next(s for s in SCENARIOS['scenarios'] if s['name'] == 'suggest_ok')
    clock = ClockStub(2.0)
    source = build_source(tmp_path, scenario, clock=clock)
    source._interval_dir.mkdir(parents=True)
    source._interval_file().write_text(str(persisted), encoding='utf-8')
    result = invoke(source, 'suggest', make_ctx('suggest'))
    assert result.state.value == 'completed'
    assert clock.slept == [source.config.min_interval_seconds]


def busy_project(tmp_path, monkeypatch):
    project = LiveProject(tmp_path, monkeypatch, happy_steps())
    guard = OsGuard(project.lock_dir/'xhs-cli-profile-default.guard')
    assert guard.try_acquire()
    try:
        result = run_one_live_slot(project)
        assert result['engine']['pause_reason'] == 'profile_busy'
        assert project.call_count() == 0
    finally:
        guard.release()
    return project


def test_local_guard_refusal_does_not_charge_a_platform_attempt(tmp_path, monkeypatch):
    project = busy_project(tmp_path, monkeypatch)
    conn = db.connect(project.paths.db_path)
    try:
        ledger = Ledger.from_settings(conn, project.run_id, project.settings)
        assert ledger.used('suggest_attempts') == 0
        recover_run(conn, project.paths, project.run_id, ledger=ledger)
        assert ledger.used('suggest_attempts') == 0
        assert conn.execute('SELECT COUNT(*) FROM budget_reservations WHERE consumed_at IS NULL').fetchone()[0] == 0
    finally:
        conn.close()


def test_recovery_charges_unknown_attempt_but_keeps_prior_local_refund(tmp_path, monkeypatch):
    project = busy_project(tmp_path, monkeypatch)
    conn = db.connect(project.paths.db_path)
    try:
        repo = Repository(conn)
        ledger = Ledger.from_settings(conn, project.run_id, project.settings)
        op = conn.execute('SELECT id FROM operations WHERE run_id=?', (project.run_id,)).fetchone()[0]
        assert ledger.used('suggest_attempts') == 0
        ledger.reserve('suggest_attempts', operation_id=op)
        repo.start_attempt(op, 2)
        repo.set_operation_status(op, OperationState.uncertain)
        recover_run(conn, project.paths, project.run_id, ledger=ledger)
        recover_run(conn, project.paths, project.run_id, ledger=ledger)
        assert ledger.used('suggest_attempts') == 1
        assert [r[0] for r in conn.execute('SELECT attempt_no FROM budget_ledger WHERE external_access=1')] == [2]
    finally:
        conn.close()


@pytest.mark.parametrize('seconds,valid', [(120, True), (121, False)])
def test_live_operation_timeout_matches_the_installed_cli_spec(tmp_path, monkeypatch, seconds, valid):
    project = LiveProject(tmp_path, monkeypatch, [])
    data = yaml.safe_load(project.config_path.read_text(encoding='utf-8'))
    data['sources']['xhs_search'].update(timeout_seconds=seconds, total_process_timeout_seconds=180)
    project.config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding='utf-8')
    if valid:
        assert load_settings(project.config_path).sources.xhs_search.timeout_seconds == 120
    else:
        with pytest.raises(ValueError, match='120'):
            load_settings(project.config_path)


def test_resume_rejects_imported_cli_code_change_with_unchanged_entry(tmp_path, monkeypatch):
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=1)
    (project.cwd/'package.json').write_text(json.dumps({'name':'xhs-search-cli','version':'test-1'}), encoding='utf-8')
    module = project.cwd/'src/imported.mjs'
    module.parent.mkdir()
    module.write_text('export const selector = "old";', encoding='utf-8')
    project.execute()
    calls = project.call_count()
    frozen = project.snapshot_meta()['tool']['package_identity']
    assert frozen['version'] == 'test-1'
    assert 'src/imported.mjs' in frozen['files']
    module.write_text('export const selector = "new";', encoding='utf-8')
    with pytest.raises(app.SourceIntegrityError, match='package'):
        project.resume(max_slots=1)
    assert project.call_count() == calls


def test_resume_abandoned_running_marker_consumes_request_without_self_transition(tmp_path, monkeypatch):
    project = LiveProject(tmp_path, monkeypatch, happy_steps(), suggest_attempts=1, search_attempts=0, card_limit=0, max_posts_stored=0)
    project.execute()
    conn = db.connect(project.paths.db_path)
    try:
        Repository(conn).set_run_status(project.run_id, RunTermination(status=RunStatus.running))
    finally:
        conn.close()
    calls = project.call_count()
    result = project.resume(max_slots=1)
    assert result['engine']['status'] == 'paused'
    assert project.call_count() == calls
    assert project.query("SELECT COUNT(*) AS n FROM control_requests WHERE run_id=? AND action='resume' AND applied_at IS NOT NULL", (project.run_id,))[0]['n'] == 1

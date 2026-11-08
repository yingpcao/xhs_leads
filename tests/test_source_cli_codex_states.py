"""State combinations that must not publish false successful observations."""
import copy
import json

import pytest

from tests.test_source_cli import SCENARIOS, build_source, invoke, make_ctx


@pytest.mark.parametrize('kind', ['suggest', 'search'])
@pytest.mark.parametrize('mutation', ['entry-error', 'skipped', 'auth-partial', 'action-partial', 'nonzero-ok', 'bool-exit', 'nonstring-version'])
def test_inconsistent_or_unexecuted_result_is_not_success(tmp_path, kind, mutation):
    scenario = copy.deepcopy(next(s for s in SCENARIOS['scenarios'] if s['name'] == f'{kind}_ok'))
    doc = json.loads(scenario['stdout'])
    row = doc['data']['results'][0]
    if mutation == 'entry-error':
        row['error'] = {'code': 'PAGE_TIMEOUT', 'message': 'operation failed'}
    elif mutation == 'skipped':
        row.update(status='skipped', suggestions=[], notes=[], warnings=['Not executed'])
    elif mutation in ('auth-partial', 'action-partial'):
        scenario['returncode'] = 7
        doc.update(exitCode=7, ok=False, error={'code': 'AUTH_REQUIRED' if mutation == 'auth-partial' else 'ACTION_REQUIRED', 'message': 'human required'})
    elif mutation == 'nonzero-ok':
        scenario['returncode'] = 7
        doc.update(exitCode=7, ok=True)
    elif mutation == 'bool-exit':
        doc['exitCode'] = False
    else:
        doc['cliVersion'] = ['2.0.1']
    scenario['stdout'] = json.dumps(doc, ensure_ascii=False)
    result = invoke(build_source(tmp_path, scenario), kind, make_ctx(kind))
    assert not result.items
    if mutation == 'skipped':
        assert result.state.value == 'skipped'
        assert result.warnings
    else:
        assert result.state.value == 'failed'
        assert result.error is not None
    if mutation in ('auth-partial', 'action-partial'):
        assert result.error.category.value == ('auth' if mutation == 'auth-partial' else 'action_required')
        assert result.error.human_action_required


@pytest.mark.parametrize('kind', ['suggest', 'search'])
@pytest.mark.parametrize('signal', ['timed_out', 'cancelled'])
@pytest.mark.parametrize('where', ['entry', 'top'])
def test_rescue_does_not_publish_contradictory_success(tmp_path, kind, signal, where):
    scenario = copy.deepcopy(next(s for s in SCENARIOS['scenarios'] if s['name'] == f'{kind}_ok'))
    doc = json.loads(scenario['stdout'])
    target = doc if where == 'top' else doc['data']['results'][0]
    target['error'] = {'code': 'PAGE_TIMEOUT', 'message': 'operation failed'}
    scenario[signal] = True
    scenario['returncode'] = -15 if signal == 'timed_out' else 130
    scenario['stdout'] = json.dumps(doc, ensure_ascii=False)
    result = invoke(build_source(tmp_path, scenario), kind, make_ctx(kind))
    assert result.state.value == 'failed'
    assert not result.items
    assert result.error is not None

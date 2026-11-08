"""Fixed A06 contracts; Fake engineering consistency is not model quality."""
from copy import deepcopy
import hashlib
import json
import socket

import pytest

from scripts.benchmarks import a06_memory as bench


CASES=('missing','current_hint','code_fact','project_context','stale_snapshot','foreign_project',
       'missing_provenance','duplicate','near_duplicate','conflict','deleted_replay','invalidated_replay',
       'expired','damaged_record','damaged_index','oversized_proposal','sensitive_tool','sensitive_proposal',
       'provider_failure','storage_failure_recovery','capacity','source_priority')


@pytest.fixture(scope='module')
def measured(tmp_path_factory):
    out=tmp_path_factory.mktemp('a06_measurement')/'run'
    return out,bench.run_benchmark(out,repeats=1,warmups=0)


def test_fixed_scenarios_and_independent_contract_identity():
    data,checksum=bench.read_fixture()
    assert tuple(c['id'] for c in data['cases'])==CASES
    assert checksum=='aa25ebd515f6a059358834e881976c61d0cc1455889d4948340c3ed8ee657da8'
    assert data['contract_commit']=='4f1127f1bd58b944af219deea6d94256ea65f190'
    assert data['binding']['commit_sha']=='a'*40
    assert data['source']=="def entry():\n    return 'CURRENT_SOURCE'\n"
    assert data['cases'][-1]['expected']['memory_context_omitted'] is True


@pytest.mark.parametrize('mode',bench.MODES)
@pytest.mark.parametrize('case_id',CASES)
def test_fixed_runtime_memory_contracts(measured,case_id,mode):
    data,_=bench.read_fixture();case=next(c for c in data['cases'] if c['id']==case_id)
    rows=[r for r in measured[1]['rows'] if r['case_id']==case_id and r['mode']==mode]
    assert len(rows)==1
    row=rows[0]
    assert row['contract_errors']==[] and row['contract_passed'] and row['runtime_ok']
    assert row['binding']==data['binding'] and row['synthetic_binding'] is True
    assert row['current_source_delivered'] and row['context_source_references']>0
    assert row['peak_estimated_input_tokens']<=row['context_budget']
    assert row['model_calls']==row['execution_model_calls']
    assert row['preserved_existing_records']
    assert row['real_model_calls']==0 and row['billed_tokens'] is row['real_cost'] is row['natural_language_quality'] is None
    if mode=='disabled':
        assert not row['calls'].get('selection') and not row['calls'].get('extraction')
        assert row['selected_ids']==[] and row['created_count']==0 and row['disabled_store_unchanged']
    else:
        assert row['selected_ids']==case['expected']['selected']
        assert row['available_ids']==case['expected']['available']
        assert row['created_count']==case['expected']['created']
        assert set(case['expected'].get('reasons',[]))<=row['reason_counts'].keys()


def test_summary_counts_use_measured_rows_without_fabricating_cost_or_quality(measured):
    out,report=measured
    assert len(report['rows'])==len(CASES)*2 and report['warmups']==0
    for mode in bench.MODES:
        rows=[r for r in report['rows'] if r['mode']==mode]
        summary=report['summaries'][mode]
        assert summary['samples']==len(CASES)
        assert summary['model_calls']==sum(r['model_calls'] for r in rows)
        assert summary['extraction_calls']==sum(r['calls'].get('extraction',0) for r in rows)
        assert summary['latency_ms']['p50']>=0
    assert report['billed_tokens'] is report['real_cost'] is report['natural_language_quality'] is None
    assert report['implementation_unchanged'] is True
    assert report['implementation_hashes']['pycode/agent/memory.py']==hashlib.sha256(bench.ROOT.joinpath('pycode/agent/memory.py').read_bytes()).hexdigest()
    assert (out/'metrics.csv').read_text().count('\n')==len(report['rows'])+1
    encoded=(out/'results.json').read_text(encoding='utf-8')
    assert 'FAKE_A06_SECRET' not in encoded and 'Memory hint says' not in encoded and 'prompt' not in json.loads(encoded)['rows'][0]


def test_fixture_changes_and_previous_output_are_rejected(tmp_path,measured):
    data,_=bench.read_fixture();changed=deepcopy(data);changed['cases'][0]['expected']['created']=99
    path=tmp_path/'changed.json';path.write_text(json.dumps(changed),encoding='utf-8')
    with pytest.raises(ValueError,match='fixture changed'):bench.read_fixture(path)
    previous=(measured[0]/'results.json').read_bytes()
    with pytest.raises(FileExistsError):bench.run_benchmark(measured[0])
    assert (measured[0]/'results.json').read_bytes()==previous


@pytest.mark.parametrize('repeats,warmups',[(0,0),(True,0),(1,-1),(1,4)])
def test_invalid_measurement_limits_do_not_create_output(tmp_path,repeats,warmups):
    out=tmp_path/'run'
    with pytest.raises(ValueError):bench.run_benchmark(out,repeats=repeats,warmups=warmups)
    assert not out.exists()


def test_same_fixture_repeats_stable_policy_results_and_blocks_network(tmp_path,monkeypatch):
    data,_=bench.read_fixture();case=next(c for c in data['cases'] if c['id']=='current_hint')
    original=bench.FakeModel.generate
    def guarded(self,prompt):
        with pytest.raises(AssertionError,match='forbids network'):
            socket.create_connection(('127.0.0.1',9),timeout=.001)
        return original(self,prompt)
    monkeypatch.setattr(bench.FakeModel,'generate',guarded)
    rows=[bench.run_case(data,case,tmp_path/str(i),'enabled') for i in range(2)]
    stable=('selected_ids','created_count','available_ids','reason_counts','source_counts','calls',
            'storage_bytes_after','binding','contract_errors')
    assert {k:rows[0][k] for k in stable}=={k:rows[1][k] for k in stable}
    assert rows[0]['contract_passed'] and rows[1]['contract_passed']

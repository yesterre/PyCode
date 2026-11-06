"""A05d: independent engineering expectations, real historical/current runtimes."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import socket
import tarfile

import pytest

from scripts.benchmarks import a05_harness as harness
from scripts.benchmarks import a05_harness_case as worker


CASES = ('normal','model_budget','tool_budget','deadline','repeated_failure','safe_retry',
         'invalid_plan','provider_timeout','tool_timeout','partial_success','large_context','output_limit','upgrade_reuse')


@pytest.fixture(scope='module')
def comparison(tmp_path_factory):
    output = tmp_path_factory.mktemp('a05_comparison') / 'run'
    report = harness.run_benchmark(output, repeats=2, warmups=1)
    raw = {name:json.loads((output / (name + '.json')).read_text(encoding='utf-8'))
           for name in report['variants']}
    return output, report, raw


def rows(comparison, variant, case):
    return [r for r in comparison[2][variant]['rows'] if r['case_id'] == case and not r['warmup']]


def test_fixed_fixture_identity_and_versioned_ground_rules():
    value, raw_hash = harness.read_fixture()
    assert value['baseline_commit'] == value['corpus_commit'] == '09bc1a83829064d57e6bf27b151ca3b69e341a93'
    assert [c['id'] for c in value['cases']] == list(CASES)
    assert value['invariants'] == {'prompt_within_budget': True}
    assert raw_hash == hashlib.sha256(harness.FIXTURE.read_bytes()).hexdigest()
    assert harness.FIXTURE_CANONICAL_SHA256 == 'bf1f85c15b1cab131774cbff4453a1a4ef8548d1f2fd4ec7d0ff59a364cc38f9'


@pytest.mark.parametrize('case', CASES)
def test_each_current_case_meets_fixed_contract_and_provider_budget(comparison, case):
    samples = rows(comparison, 'a05_current', case)
    assert len(samples) == 2
    for row in samples:
        assert row['contract_passed'] and row['contract_errors'] == []
        assert row['prompt_within_budget']
        assert row['execution_budget']['usage']['model_calls'] == row['model_calls']
        assert row['execution_budget']['usage']['tool_calls'] == row['handler_attempts']
        assert row['binding']['commit_sha'] == comparison[1]['corpus_commit']
        assert row['real_model_calls'] == 0
        assert row['real_cost'] is row['billed_tokens'] is row['natural_language_quality'] is None
    stable = ('stop_reason','outcome','runtime_ok','model_calls','handler_attempts',
              'query_embedding_calls','input_sha256','config_sha256','corpus_sha256','binding')
    assert {k:samples[0][k] for k in stable} == {k:samples[1][k] for k in stable}


def test_historical_failure_mask_and_bounded_stop_remain_visible(comparison):
    old = rows(comparison, 'pre_a05', 'tool_timeout')[0]
    new = rows(comparison, 'a05_current', 'tool_timeout')[0]
    assert old['runtime_ok'] and old['answer_present'] and old['outcome'] == 'failed'
    assert not new['runtime_ok'] and not new['answer_present'] and new['stop_reason'] == 'required_tool_failed'
    old = rows(comparison, 'pre_a05', 'repeated_failure')[0]
    new = rows(comparison, 'a05_current', 'repeated_failure')[0]
    assert (old['handler_attempts'], old['model_calls'], old['repeated_failed_observations']) == (4,6,3)
    assert (new['handler_attempts'], new['model_calls'], new['repeated_failed_observations']) == (2,3,1)
    assert new['no_progress_events'] == 1


def test_safe_retry_recovers_real_read_without_replaying_an_extra_decision(comparison):
    old = rows(comparison, 'pre_a05', 'safe_retry')[0]
    new = rows(comparison, 'a05_current', 'safe_retry')[0]
    assert old['handler_attempts'] == 1 and old['outcome'] == 'failed'
    assert new['handler_attempts'] == 2 and new['tool_observations'] == 1
    assert new['observations'][0]['execution']['attempts'] == 2
    assert new['outcome'] == 'complete' and new['runtime_ok']


def test_large_source_is_preserved_raw_and_wholly_omitted_with_provenance(comparison):
    output, _, _ = comparison
    content = '\n'.join((output / 'corpus/pycode/agent/runtime.py').read_text(encoding='utf-8').splitlines())
    old = rows(comparison, 'pre_a05', 'large_context')[0]
    new = rows(comparison, 'a05_current', 'large_context')[0]
    assert len(content) > 32768
    for sample in (old, new):
        assert sample['observations'][0]['returned_content_chars'] == len(content)
        assert sample['observations'][0]['content_sha256'] == hashlib.sha256(content.encode()).hexdigest()
    assert old['peak_estimated_input_tokens'] > old['requested_limits']['context_tokens']
    assert new['peak_estimated_input_tokens'] <= new['requested_limits']['context_tokens']
    assert new['discard_reasons']['context_budget'] > 0
    assert any(d['reference'].get('path') == 'pycode/agent/runtime.py' and d['reason'] == 'context_budget'
               for d in new['actual_context']['decisions'])


def test_upgrade_reuses_original_pack_once_with_same_commit_and_no_second_query(comparison):
    for variant in ('pre_a05', 'a05_current'):
        sample = rows(comparison, variant, 'upgrade_reuse')[0]
        assert sample['preflight_status'] == 'insufficient' and sample['pack_reused']
        assert sample['query_embedding_calls'] == 1
        assert sample['pack_digest'] == sample['preflight_summary']['pack_digest']
    sample = rows(comparison, 'a05_current', 'upgrade_reuse')[0]
    assert sample['execution_budget']['usage']['query_embeddings'] == 1


def test_versions_are_imported_from_immutable_exports_not_worktree(comparison):
    output, report, raw = comparison
    assert report['variants']['pre_a05']['source']['commit'] != report['entry_head']
    assert report['variants']['a05_current']['source']['representation'] == 'working_tree_snapshot'
    for variant, data in raw.items():
        source = output / (variant + '_source')
        assert Path(data['imported_package']).resolve() == source / 'pycode/__init__.py'
        manifest = report['variants'][variant]['source']['files']
        assert {p.relative_to(source).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in source.rglob('*') if p.is_file()} == manifest
    legacy = rows(comparison, 'pre_a05', 'normal')[0]
    assert legacy['actual_context'] is legacy['execution_budget'] is legacy['discard_reasons'] is None
    assert 'context_tokens' in legacy['unsupported_limits']


def test_aggregation_excludes_warmups_and_keeps_lower_completion_rate_honest(comparison):
    _, report, raw = comparison
    for variant, value in report['variants'].items():
        assert len(raw[variant]['rows']) == 39
        summary = value['summary']
        assert summary['samples'] == 26 and summary['latency_ms']['samples'] == 26
        assert sum(summary['outcomes'].values()) == 26
        assert summary['completion_rate'] + summary['partial_rate'] + summary['failure_rate'] == pytest.approx(1)
    new, old = (report['variants'][key]['summary'] for key in ('a05_current','pre_a05'))
    assert new['contract_pass_rate'] == 1 and old['contract_pass_rate'] < 1
    assert new['completion_rate'] < old['completion_rate']  # Controlled stops are not "completed tasks".
    assert harness.distribution([1,10,20,30]) == dict(samples=4,p50=15,p95=30,mean=15.25)
    assert harness.distribution([]) is None


def test_output_directory_is_never_overwritten(tmp_path):
    target = tmp_path / 'existing'
    target.mkdir()
    sentinel = target / 'keep.txt'
    sentinel.write_text('Preserve', encoding='utf-8')
    with pytest.raises(FileExistsError):
        harness.run_benchmark(target, repeats=1, warmups=0)
    assert sentinel.read_text() == 'Preserve' and list(target.iterdir()) == [sentinel]


def test_current_copy_includes_uncommitted_changes_and_new_files_without_touching_source(tmp_path,monkeypatch):
    source = tmp_path / 'pycode'
    source.mkdir()
    (source/'__init__.py').write_bytes(b'# Uncommitted current source\n')
    (source/'new.py').write_bytes(b'# New current module\n')
    monkeypatch.setattr(harness,'ROOT',tmp_path)
    def fake_git(*args):
        if args[0]=='status':return b' M pycode/__init__.py\n D pycode/deleted.py\n?? pycode/new.py\n'
        return b'pycode/new.py\0' if '--others' in args else b'pycode/__init__.py\0pycode/deleted.py\0'
    monkeypatch.setattr(harness,'git',fake_git)
    manifest=harness.copy_current_implementation('a'*40,tmp_path/'copy')
    assert manifest['representation']=='working_tree_snapshot' and len(manifest['dirty_paths'])==3
    assert set(manifest['files'])=={'pycode/__init__.py','pycode/new.py'}
    assert (tmp_path/'copy/pycode/__init__.py').read_bytes()==(source/'__init__.py').read_bytes()
    assert (tmp_path/'copy/pycode/new.py').read_bytes()==(source/'new.py').read_bytes()
    assert not (tmp_path/'copy/pycode/deleted.py').exists()


@pytest.mark.parametrize('name,link', [('../outside.py',False),('other/file.py',False),('pycode/link',True)])
def test_git_export_rejects_escaping_paths_and_links(tmp_path,monkeypatch,name,link):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w') as archive:
        member = tarfile.TarInfo(name)
        if link:
            member.type=tarfile.SYMTYPE
            member.linkname='../outside'
        archive.addfile(member)
    monkeypatch.setattr(harness,'git',lambda *args:stream.getvalue())
    with pytest.raises(ValueError):
        harness.export_implementation('unused',tmp_path/'export')
    assert not (tmp_path/'outside.py').exists()


def test_fixture_rejects_versions_duplicate_cases_and_path_escape(tmp_path):
    value,_ = harness.read_fixture()
    for update in ({'schema_version':2},{'corpus_paths':['../private.py']},{'cases':[value['cases'][0]]*2}):
        altered=deepcopy(value)
        altered.update(update)
        path=tmp_path/'bad.json'
        path.write_text(json.dumps(altered),encoding='utf-8')
        with pytest.raises(ValueError):harness.read_fixture(path)


def test_child_environment_and_network_guard_exclude_real_services(monkeypatch):
    for name in ('OPENAI_API_KEY','AZURE_OPENAI_API_KEY','LLM_API_KEY','PYCODE_EMBEDDING_API_KEY',
                 'DATABASE_URL','REDIS_URL','TEST_DATABASE_URL','TEST_REDIS_URL','PYTHONPATH'):
        monkeypatch.setenv(name,'synthetic-private-sentinel')
    env=harness.child_environment()
    assert 'synthetic-private-sentinel' not in env.values()
    assert env['PYTHON_DOTENV_DISABLED']=='1' and env['PYCODE_EMBEDDING_ENABLED']=='false'
    monkeypatch.setattr(socket.socket,'connect',socket.socket.connect)
    monkeypatch.setattr(socket,'create_connection',socket.create_connection)
    worker.block_network()
    with pytest.raises(AssertionError,match='Network is forbidden'):socket.create_connection(('127.0.0.1',1))
    with pytest.raises(AssertionError,match='Network is forbidden'):socket.socket.connect(None,('example.com',443))

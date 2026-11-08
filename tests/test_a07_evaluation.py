"""A07a provenance/classification/aggregation seams; not a model quality score."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.benchmarks import a07_evaluation as bench


def test_fixed_protocol_keeps_original_questions_and_explicit_model_boundaries():
    protocol,data=bench.read_protocol()
    assert bench.PROTOCOL_SHA256=='9b22043b73009b84536908025ae113e18e446d4ed78d2a0079340489c3d61755'
    assert protocol['question_sha256']=='8083244fe8a5b61b600835ca15a378ec5413e4feb931a5ce49d5d0512dc8bb74'
    assert data['commit']=='5925bb30fb674451b1c9d08360ca83d00f64780a'
    assert len(data['cases'])==16 and sum(c['truth_status']=='unknown' for c in data['cases'])==1
    assert protocol['graph_schema_version']==4 and protocol['embedding']['provider']=='fake'
    assert protocol['llm']['model'] is None and protocol['policy']['real_model_quality']=='not_run'


def test_protocol_mutation_is_rejected_without_output_or_model_work(tmp_path):
    protocol,_=bench.read_protocol();protocol['top_k']=1
    path=tmp_path/'changed.json';path.write_text(json.dumps(protocol),encoding='utf-8')
    with pytest.raises(ValueError,match='protocol changed'):
        bench.run_evaluation(tmp_path/'out',protocol_path=path)
    assert not (tmp_path/'out').exists()


def historical_sample():
    dataset=dict(cases=[dict(id='positive',category='lookup',truth_status='source_verified',
        evidence=[dict(path='a.py',start_line=1,end_line=2)]),
        dict(id='unknown',category='unknown',truth_status='unknown',evidence=[])])
    archive=dict(summary={'1':dict(recall=1.0,reciprocal_rank=1.0,snippet_line_coverage=.5)},rows=[
        dict(id='positive',retrieved_paths=['a.py'],metrics={'1':dict(snippet_line_coverage=.5)}),
        dict(id='unknown',retrieved_paths=['unlabelled.py'],metrics={'1':dict(snippet_line_coverage=None)})])
    return dataset,archive


def test_history_path_rank_recomputed_but_missing_lines_and_unknown_never_fabricated():
    dataset,archive=historical_sample();before=deepcopy(archive)
    result=bench.a00_archive_metrics(archive,dataset)
    assert result['summary']['1']==dict(recall=1,reciprocal_rank=1,archived_line_coverage=.5)
    assert result['categories']['unknown']['1']['recall'] is None
    assert result['categories']['unknown']['1']['archived_line_coverage'] is None
    assert result['line_coverage_status'].startswith('cached_case_scores_reaggregated_not_independently')
    assert result['ndcg_status']=='not_recorded_in_original_A00' and archive==before
    archive['rows'][0]['retrieved_paths']=['wrong.py']
    with pytest.raises(ValueError,match='aggregate'):bench.a00_archive_metrics(archive,dataset)


def test_history_receipt_and_retriever_gap_remain_explicit(tmp_path):
    protocol,_=bench.read_protocol();dataset,archive=historical_sample()
    dataset['commit']=protocol['corpus_commit']
    archive.update(commit=dataset['commit'],dataset_sha256=protocol['question_sha256'],corpus_sha256='a'*64,
        implementation_files={'pycode/retriever.py':protocol['legacy_retriever_sha256']})
    path=tmp_path/'archive.json';path.write_text(json.dumps(archive),encoding='utf-8')
    protocol['historical_receipts']['a00']=bench.sha(path.read_bytes())
    result=bench.inspect_archive(path,'a00',protocol,dataset)
    assert result['classification']=='historical_A00_stub_archive'
    assert result['implementation_reproduced'] is result['exact_retriever_source_available'] is False
    assert result['reexecuted'] is result['current_latency_comparable'] is False
    path.write_text('{}',encoding='utf-8')
    with pytest.raises(ValueError,match='receipt mismatch'):bench.inspect_archive(path,'a00',protocol,dataset)


def test_implementation_drift_rejects_a_measurement_without_rewriting_source(monkeypatch):
    monkeypatch.setattr(bench,'implementation',lambda:dict(head='new',files={}))
    with pytest.raises(ValueError,match='changed during'):bench.verify_implementation(dict(head='old',files={}))


@pytest.mark.parametrize('structured,expected',[(False,1),(True,4)])
def test_a02_opt_in_uses_current_graph_and_retains_old_default(tmp_path,monkeypatch,structured,expected):
    source=b'def run():\n    return 1\n'
    dataset=dict(schema_version=1,commit='a'*40,corpus_prefixes=['pycode/','backend/'],cases=[dict(
        id='Q',category='lookup',question='run',mode='ask',target=None,truth_status='source_verified',
        evidence=[dict(path='pycode/a.py',symbol='run',start_line=1,end_line=2,source_sha256=bench.sha(source))])])
    path=tmp_path/'questions.json';path.write_text(json.dumps(dataset),encoding='utf-8')
    monkeypatch.setattr(bench.a02,'read_corpus',lambda *a:{'pycode/a.py':source})
    result=bench.a02.run_benchmark(bench.ROOT,path,tmp_path/'out',repeats=1,warmup=0,structured_graph=structured)
    assert result['configuration']['graph_schema_version']==expected
    assert result['configuration']['structured_graph'] is structured
    assert result['binding']['commit_sha']=='a'*40
    assert set(result['summary'])=={'keyword','vector','graph','hybrid','production_hybrid'}


@pytest.fixture(scope='module')
def measured(tmp_path_factory):
    out=tmp_path_factory.mktemp('a07_evaluation')/'run'
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*a,**kw):pytest.fail('Evaluation must not load developer credentials or create an LLM')
        patch.setattr('pycode.llm_client._parse_env_file',forbidden)
        patch.setattr('pycode.engine.OpenAIResponsesClient',forbidden)
        result=bench.run_evaluation(out,profile='test')
    return out,result


def test_current_fixed_corpus_is_clearly_fake_and_not_an_old_baseline_reproduction(measured):
    out,result=measured
    assert result['profile']=='test' and result['historical']=={}
    assert result['legacy_A00_reproduction']['status']=='not_reproduced'
    assert result['real_model_results'] is result['answer_quality'] is result['real_model_cost'] is None
    assert result['llm_calls']==result['real_model_calls']==result['paid_requests']==0
    assert result['implementation_unchanged']
    assert result['truth_status']=={'source_verified':15,'unknown':1}
    retrieval=result['retrievals']['12000']
    assert retrieval['classification']=='current_implementation_fake_engineering'
    assert retrieval['configuration']['graph_schema_version']==4
    raw=json.loads((out/retrieval['result_path']).read_text(encoding='utf-8'))
    for row in raw['rows']:
        for strategy in row['strategies'].values():
            for item in strategy['selected']:
                assert {k:item[k] for k in ('project_id','snapshot_id','commit_sha')}==raw['binding']
    assert result['repository_scale']['classification']=='not_measured_test_profile'


def test_category_aggregation_is_joined_by_original_question_identity(measured):
    out,result=measured;_,dataset=bench.read_protocol()
    retrieval=result['retrievals']['12000']
    raw=json.loads((out/retrieval['result_path']).read_text(encoding='utf-8'))
    unknown=next(c for c in dataset['cases'] if c['truth_status']=='unknown')
    group=retrieval['categories'][unknown['category']]
    assert all(g['selected']['6']['file_recall'] is None for g in group.values())
    assert retrieval['summary']==bench.a02.aggregate(raw['rows'],raw['configuration']['ks'])
    assert set(retrieval['categories'])=={c['category'] for c in dataset['cases']}
    assert 'production_intent_replay' in (out/'retrieval-metrics.csv').read_text()


def test_builds_are_deterministic_and_fake_artifact_reuse_makes_no_document_request(measured):
    _,result=measured;build=result['fixed_corpus_build']
    assert len(build['rounds'])==2 and build['repeated_full_builds_identical']
    assert build['rounds'][0]['artifact_sha256']==build['rounds'][1]['artifact_sha256']
    assert build['fake_embedding']['document_calls']>0 and build['fake_embedding']['repeat_document_calls']==0
    assert build['fake_embedding']['artifact_bytes']>0 and build['fake_embedding']['segments']>=build['chunks']
    assert build['incremental_indexing']=='not_implemented'
    assert all(s['python_peak_bytes']>0 and s['build_ms']>0 for s in build['rounds'])


def test_existing_output_and_unknown_profile_are_rejected_without_reexecution(measured,tmp_path):
    out,_=measured;before=(out/'results.json').read_bytes()
    with pytest.raises(FileExistsError):bench.run_evaluation(out,profile='test')
    assert (out/'results.json').read_bytes()==before
    with pytest.raises(ValueError,match='profile'):bench.run_evaluation(tmp_path/'unused',profile='unknown')
    assert not (tmp_path/'unused').exists()

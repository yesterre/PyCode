"""New A07b evaluation seams only; not a rerun of A01–A06 suites."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from scripts.benchmarks import a07_answers as bench, a07_answer_case as worker
from pycode.chunks import SnapshotBinding,build_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.retrieval_evidence import RetrievalContext


@pytest.fixture
def tiny(tmp_path):
    root=tmp_path/'repo';root.mkdir()
    (root/'entry.py').write_text('from helper import work\ndef entry():\n    return work()\n',encoding='utf-8')
    (root/'helper.py').write_text('def work():\n    return 42\n',encoding='utf-8')
    binding=SnapshotBinding('10000000-0000-4000-8000-000000000001','20000000-0000-4000-8000-000000000001','a'*40)
    index=PyCodeEngine().build_index(root,include_chunk_metadata=True,include_graph_metadata=True)
    chunks=build_chunk_index(index,binding)
    context=RetrievalContext.create(root,index,build_code_graph(index,binding=binding),binding=binding,chunks=chunks)
    protocol,_=bench.read_protocol()
    public=dict(id='Q01',question='Explain entry',target='entry',mode='ask')
    return protocol,public,context,root


def test_fixed_protocol_original_questions_and_quality_are_not_replaced():
    protocol,data=bench.read_protocol()
    assert len(data['cases'])==16 and sum(c['truth_status']=='unknown' for c in data['cases'])==1
    assert protocol['corpus_commit']==data['commit']=='5925bb30fb674451b1c9d08360ca83d00f64780a'
    assert protocol['legacy']['original_A00_reproduced'] is False
    assert protocol['legacy']['retriever_sha256']!='498b9fab9b878a94de08b54210c0176fa30d9c3d3ebec2def5afc00df484eee2'
    assert protocol['quality']['eligible_source']=='real_model_answer_with_human_review_only'
    assert set(protocol['quality']['dimensions'])=={'factual_correctness','evidence_coverage','unsupported_claim_ratio','answer_completeness'}


def test_changed_protocol_and_missing_artifacts_fail_before_any_run(tmp_path):
    protocol,_=bench.read_protocol();protocol['repeats']=99
    changed=tmp_path/'changed.json';changed.write_text(json.dumps(protocol),encoding='utf-8')
    with pytest.raises(ValueError,match='protocol changed'):
        bench.run_benchmark(tmp_path/'out',tmp_path/'absent',protocol_path=changed)
    with pytest.raises(ValueError,match='receipt mismatch'):
        bench.run_benchmark(tmp_path/'out',tmp_path/'absent')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('variant,key,value',[
    ('memory_off','enable_memory',False),('one_turn','limits.max_turns',1),
    ('graph_off','graph_depth',0),('vector_off','vector_candidates',False),('keyword_off','keyword_candidates',False)])
def test_ablation_has_one_direct_control(variant,key,value):
    protocol,_=bench.read_protocol();base=worker.effective_controls(protocol,'agent')
    altered=worker.effective_controls(protocol,variant)
    def flatten(v):return {**{k:val for k,val in v.items() if k!='limits'},**{'limits.'+k:val for k,val in v['limits'].items()}}
    left,right=flatten(base),flatten(altered)
    assert {k for k in left if left[k]!=right[k]}=={key} and right[key]==value


def test_real_adapter_fast_and_agent_share_initial_pack_and_measure_real_calls(tiny,monkeypatch):
    protocol,public,context,root=tiny
    def forbidden(*args,**kw):pytest.fail('Developer credentials or external clients must not be loaded')
    monkeypatch.setattr('pycode.llm_client._parse_env_file',forbidden)
    monkeypatch.setattr('pycode.engine.OpenAIResponsesClient',forbidden)
    # Poison GT fields are never passed into the Fake policy or prompts.
    public.update(evidence=[{'hidden':'A07_GT_MUST_NOT_BE_SEEN'}],explanation_points=['A07_GT_MUST_NOT_BE_SEEN'])
    fast=worker.run_current(protocol,public,context,root,'fast')
    agent=worker.run_current(protocol,public,context,root,'agent')
    assert fast['runtime_ok'] and agent['runtime_ok']
    assert fast['preflight']['pack_digest']==agent['preflight']['pack_digest']
    assert agent['initial_pack_reused'] and fast['tool_calls']==0
    assert [v['tool'] for v in agent['tools']]==['retrieve_context','query_graph']
    assert agent['tool_failures']==0 and agent['model_calls']>fast['model_calls']==1
    assert fast['binding']==agent['binding']==vars(context.binding)
    for row in (fast,agent):
        assert row['real_model_calls']==0 and row['real_cost'] is row['billed_tokens'] is None
        assert all(v is None for v in row['quality'].values())
        assert row['model_calls']==row['execution_budget']['usage']['model_calls']
        assert row['peak_estimated_input_tokens']<=protocol['limits']['context_tokens']
        assert all('A07_GT_MUST_NOT_BE_SEEN' not in c['prompt'] for c in row['calls'])
        assert all(c['estimated_input_tokens']==len(c['prompt'].encode())+512 for c in row['calls'])
        assert all(r['estimated_tokens']<=r['budget'] for r in row['context_reports'] if r['delivered_to_model'])
    assert agent['answer'].startswith('FAKE engineering placeholder')


def test_memory_off_skips_selection_and_preserves_seeded_record(tiny):
    from pycode.agent.memory import MemoryStore
    from pycode.agent.memory_record import MemoryScope
    from scripts.benchmarks.a06_memory import add,storage
    protocol,public,context,root=tiny
    add(MemoryStore(root,scope=MemoryScope(root,context.binding,protocol['seed_run_id']),origin='user_explicit'),protocol['memory_note'])
    before=storage(root)
    off=worker.run_current(protocol,public,context,root,'memory_off')
    assert storage(root)==before
    on=worker.run_current(protocol,public,context,root,'agent')
    assert off['runtime_ok'] and on['runtime_ok']
    assert not any(c['phase'] in {'selection','extraction'} for c in off['calls'])
    assert any(c['phase']=='selection' for c in on['calls'])
    assert 'A07_SYNTHETIC_PREFERENCE' in '\n'.join(c['prompt'] for c in on['calls'])
    assert 'A07_SYNTHETIC_PREFERENCE' not in '\n'.join(c['prompt'] for c in off['calls'])


def test_one_turn_records_budget_stop_not_success_from_placeholder(tiny):
    protocol,public,context,root=tiny
    row=worker.run_current(protocol,public,context,root,'one_turn')
    assert not row['runtime_ok'] and row['outcome']=='failed'
    assert row['stop_reason'] in {'max_turns','budget_turns'}
    assert row['answer'] is None and row['tool_calls']==1
    assert row['execution_budget']['usage']['turns']==1


def test_ownership_loss_and_model_failure_do_not_become_success(tiny,monkeypatch):
    from backend.app.core.errors import BackendError
    protocol,public,context,root=tiny
    def lost():raise BackendError('worker_execution_lost','Synthetic execution lost')
    row=worker.run_current(protocol,public,context,root,'agent',checkpoint=lost)
    assert not row['runtime_ok'] and row['model_calls']==row['tool_calls']==0
    assert row['error']['code']=='worker_execution_lost'
    def fail(self,*a):raise TimeoutError('Synthetic model failure')
    monkeypatch.setattr(worker.ScriptedModel,'_respond',fail)
    row=worker.run_current(protocol,public,context,root,'fast')
    assert not row['runtime_ok'] and row['outcome']=='failed' and row['model_failures']==1
    assert row['calls'][0]['failed']


def test_network_guards_and_actual_prompt_line_observer(tiny):
    protocol,public,context,root=tiny
    with worker.block_network()[1]:
        with pytest.raises(AssertionError,match='forbids network'):socket.create_connection(('127.0.0.1',9))
    row=worker.run_current(protocol,public,context,root,'agent')
    sources=dict(context.sources)
    truth=[dict(path='entry.py',start_line=1,end_line=3)]
    score=bench.engineering_evidence(row,truth,sources)
    assert score['citation_count']>0 and score['address_support_rate']==1
    assert score['delivered_line_coverage']>0
    assert bench.engineering_evidence(row,[],sources)['delivered_line_coverage'] is None
    broken=deepcopy(row)
    source_call=next(c for c in reversed(broken['calls']) if c['visible_code'])
    source_call['visible_code'][0]['lines'][0][1]='invented source'
    with pytest.raises(ValueError,match='differs from pinned'):bench.engineering_evidence(broken,truth,sources)


def test_summary_counts_unknown_separately_and_does_not_impute_human_quality(tiny):
    protocol,public,context,root=tiny
    row=worker.run_current(protocol,public,context,root,'fast')
    row['engineering_evidence']=bench.engineering_evidence(row,[],dict(context.sources))
    summary=bench.summarize([row])
    assert summary['unknown_samples']==1 and summary['delivered_line_coverage'] is None
    assert summary['natural_language_quality'] is summary['real_cost'] is summary['billed_tokens'] is None
    assert summary['model_calls']==1 and summary['latency_ms']['samples']==1


def test_exact_substitute_legacy_export_runs_isolated_without_claiming_A00(tmp_path):
    protocol,_=bench.read_protocol();protocol['repeats']=1
    source=tmp_path/'source';manifest=bench.export_legacy(protocol['legacy']['commit'],source)
    assert manifest['files']['pycode/retriever.py']==protocol['legacy']['retriever_sha256']
    inputs=tmp_path/'inputs';(inputs/'corpus').mkdir(parents=True)
    (inputs/'corpus'/'entry.py').write_text('def entry():\n    return 42\n',encoding='utf-8')
    (inputs/'protocol.json').write_text(json.dumps(protocol),encoding='utf-8')
    (inputs/'public-questions.json').write_text(json.dumps([dict(id='Q01',question='entry',mode='ask',target=None)]),encoding='utf-8')
    output=tmp_path/'output'
    child=subprocess.run([sys.executable,str(bench.WORKER),str(source),str(inputs),str(output),'legacy'],
        env=bench.harness.child_environment(),capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert child.returncode==0,child.stdout+child.stderr
    row=json.loads((output/'case-0000.json').read_text(encoding='utf-8'))
    assert row['runtime_ok'] and row['classification']=='substitute_legacy_sync_not_original_A00'
    assert row['original_A00_reproduced'] is row['trusted_runtime_binding'] is False
    assert row['model_calls']==1 and row['execution_budget'] is None


def test_current_isolated_worker_loads_published_inputs_and_records_both_paths(tiny,tmp_path):
    import shutil
    from dataclasses import asdict
    from pycode.chunks import save_chunk_index
    from pycode.storage import save_index,save_graph
    from pycode.embedding import prepare_inputs
    from pycode.semantic_index import SemanticIdentity,encode_vectors,seal_artifact,file_hash
    from scripts.benchmarks.a02_retrieval import HashFakeProvider
    protocol,public,context,root=tiny
    public=dict(public,target='entry.py')  # Supported file-target contract in Hybrid.
    protocol['repeats']=1  # Tiny CLI contract fixture, not the formal experiment.
    inputs=tmp_path/'inputs';inputs.mkdir()
    shutil.copytree(root,inputs/'corpus')
    save_index(context.index,inputs/'index.json');save_graph(context.graph,inputs/'graph.json')
    save_chunk_index(context.chunks,inputs/'chunks.json')
    fake=HashFakeProvider();segments=prepare_inputs(context.chunks)
    vectors=fake.embed_documents([s.text for s in segments])
    artifact=inputs/'fake-artifact';artifact.mkdir()
    (artifact/'vectors.f32').write_bytes(encode_vectors(vectors.vectors,fake.dimension))
    seal_artifact(artifact,SemanticIdentity(context.binding,file_hash(inputs/'chunks.json'),fake.provider,fake.model,fake.dimension),segments)
    (inputs/'a07a-results.json').write_text(json.dumps({'retrievals':{'131072':{'binding':asdict(context.binding)}}}),encoding='utf-8')
    (inputs/'protocol.json').write_text(json.dumps(protocol),encoding='utf-8')
    (inputs/'public-questions.json').write_text(json.dumps([public]),encoding='utf-8')
    source=tmp_path/'current-source'
    bench.harness.copy_current_implementation(bench.harness.git('rev-parse','HEAD').decode().strip(),source)
    out=tmp_path/'current-output'
    child=subprocess.run([sys.executable,str(bench.WORKER),str(source),str(inputs),str(out),'current'],
        env=bench.harness.child_environment(),capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert child.returncode==0,child.stdout+child.stderr
    rows=[json.loads((out/p).read_text(encoding='utf-8')) for p in json.loads((out/'rows.json').read_text(encoding='utf-8'))]
    assert {r['variant'] for r in rows}=={'fast','agent'} and all(r['runtime_ok'] for r in rows)
    assert all(r['binding']==asdict(context.binding) for r in rows)
    assert rows[0]['memory_before']==rows[1]['memory_before']
    assert all(c['phase'] not in {'selection','extraction'} for r in rows if r['variant']=='fast' for c in r['calls'])


def test_hybrid_symbol_target_limitation_is_reported_without_rewriting_input(tiny):
    protocol,public,context,root=tiny
    assert public['target']=='entry'
    row=worker.run_current(protocol,public,replace(context,retrieval_mode='hybrid'),root,'agent')
    assert not row['runtime_ok'] and row['outcome']=='failed'
    assert row['stop_reason']=='required_tool_failed' and row['preflight']['status']=='insufficient'
    assert 'target_not_indexed' in row['tools'][0]['data']['retrieval']['diagnostics']
    assert public['target']=='entry' and row['model_failures']==0


def test_numbered_source_cannot_be_mistaken_for_a_prompt_evidence_header():
    source="label = '## 证据 99: embedded marker'\nreturn label\n"
    prompt="## 证据 1: Synthetic\n路径: entry.py\n代码片段:\n"+'\n'.join(
        f'{n}: {line}' for n,line in enumerate(source.splitlines(),1))
    visible=worker.visible_code(prompt)
    assert visible==[dict(path='entry.py',lines=[[1,source.splitlines()[0]],[2,'return label']],start_line=1,end_line=2)]
    assert bench.line_pairs([{'visible_code':visible}],{'entry.py':source})=={('entry.py',1),('entry.py',2)}

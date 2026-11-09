"""Memory cleanup acceptance through real public consumers, not helper absence."""
import json

from pycode.agent import memory as memory_module
from pycode.agent.memory_record import MemoryScope
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask,RuntimeConfig
from pycode.chunks import SnapshotBinding,build_chunk_index
from pycode.cli import memory_project
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.retrieval_evidence import RetrievalContext
from pycode.tools import ToolContext
from pycode.tools.memory_tools import memory


def test_runtime_extraction_tool_and_cli_use_store_identity_without_private_helpers(tmp_path,monkeypatch,capsys):
    calls=[]
    def forbidden(*args,**kwargs):
        calls.append('obsolete_helper')
        raise AssertionError('Public Memory consumers must use the current Store contract')
    for name in ('_memory_fingerprints','_fingerprint'):
        monkeypatch.setattr(memory_module,name,forbidden,raising=False)
    root=tmp_path/'project';root.mkdir()
    (root/'entry.py').write_text('def entry():\n    return 42\n',encoding='utf-8')
    binding=SnapshotBinding('10000000-0000-4000-8000-000000000001',
        '20000000-0000-4000-8000-000000000001','a'*40)
    scope=MemoryScope(root,binding,'30000000-0000-4000-8000-000000000001')
    index=PyCodeEngine().build_index(root,include_chunk_metadata=True,include_graph_metadata=True)
    chunks=build_chunk_index(index,binding)
    trusted=RetrievalContext.create(root,index,build_code_graph(index,binding=binding),binding=binding,chunks=chunks)
    context=ToolContext(root,retrieval_context=trusted)
    proposal=dict(id='review-note',type='preference',title='Review convention',
        summary='Cite evidence',body='Review changes carefully with citations.',tags=['primary'],related_files=['entry.py'])
    class Model:
        extraction_calls=0
        def generate(self,prompt):
            if prompt.startswith('Select project memories'):
                return '[]'
            if prompt.startswith('Extract durable PyCode memories'):
                self.extraction_calls+=1
                return json.dumps([proposal,dict(proposal,id='same-note'),dict(proposal,id='other-tags',tags=['secondary'])])
            if prompt.startswith('You are the PyCode LLM Planner.'):
                return json.dumps([dict(tool='retrieve_context',required=True,arguments={
                    'question':'Explain entry.py','intent':'explain','target':'entry.py'})])
            if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
                payload=json.loads(prompt.split('Next-action input:\n',1)[1])
                if payload['turn']['tool_results']==0:
                    return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={
                        'question':'Explain entry.py','intent':'explain','target':'entry.py'}))
                return json.dumps(dict(action_type='final_answer',final_answer='Available source: entry.py:1-2.'))
            raise AssertionError('Unexpected model phase')
    model=Model()
    with scope.activate():
        manual=memory_project(root,'add',memory_id='citation-rule',memory_type='preference',
            title='Citation preference',summary='Use source addresses',body='Cite the current source.')
        result=run_agent_runtime(AgentTask('Remember review conventions for future tasks and explain entry.py.',root),
            RuntimeConfig(),llm_client=model,context=context)
        loaded=memory(context,operation='load',id=manual.id)
        listed=memory_project(root,'list')
    assert result.ok and model.extraction_calls==1
    assert len(result.memory.extracted_memories)==2
    assert {m.lifecycle['state'] for m in result.memory.extracted_memories}=={'active','conflict'}
    assert result.memory.reason_counts.get('duplicate')==1
    assert result.memory.reason_counts.get('conflict')==1
    assert all(m.provenance['origin']=='model_inference' and m.provenance['snapshot_id']==binding.snapshot_id
               for m in result.memory.extracted_memories)
    assert loaded.ok and loaded.data['memory']['record_id']==manual.record_id
    assert loaded.data['memory']['body']=='Cite the current source.'
    assert loaded.data['memory']['provenance']['origin']=='user_explicit'
    assert manual.id in {entry.id for entry in listed}
    assert 'PyCode Memory list.' in capsys.readouterr().out
    assert calls==[]

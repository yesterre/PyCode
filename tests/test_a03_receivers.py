"""Closed nominal receiver/alias facts, not a general Python type engine."""
import ast
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from pycode.chunks import SnapshotBinding,build_chunk_index,save_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.graph_schema import graph_to_dict,graph_from_dict,validate_graph
from pycode.query import get_function_callers,RetrievalGraph
from pycode.retrieval_evidence import RetrievalContext,RetrievalRequest,Query
from pycode.retriever import retrieve
from pycode.storage import save_index
from pycode.tools import ToolContext,query_code_graph
from a03_graph_fixtures import schema3

FIXTURE=Path(__file__).parent/'fixtures/a03/receivers'


def sources():return {p.relative_to(FIXTURE).as_posix():p.read_text(encoding='utf-8') for p in FIXTURE.rglob('*.py')}


def build(tmp_path,files=None):
    root=tmp_path/'repo';root.mkdir()
    for path,source in (sources() if files is None else files).items():
        file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(source,encoding='utf-8')
    index=PyCodeEngine().build_index(root,include_graph_metadata=True,include_chunk_metadata=True)
    bound=SnapshotBinding(str(uuid4()),str(uuid4()),'a'*40)
    graph=build_code_graph(index,binding=bound);chunks=build_chunk_index(index,bound)
    return index,graph,RetrievalContext.create(root,index,graph,binding=bound,chunks=chunks)


def test_nominal_calls_share_identity_chunks_and_exact_graph_evidence(tmp_path,monkeypatch):
    parse=ast.parse;calls=[]
    def count(*args,**kwargs):calls.append(kwargs.get('filename'));return parse(*args,**kwargs)
    monkeypatch.setattr(ast,'parse',count)
    index,graph,context=build(tmp_path);before=len(calls)
    assert before==3 and graph.schema_version==4
    assert graph_from_dict(graph_to_dict(graph))==graph
    assert graph_to_dict(graph)==graph_to_dict(build_code_graph(replace(index,files=index.files[::-1]),binding=context.binding))
    expected={'self.service.work':'method:service.py:Service.work','self.helper':'method:consumer.py:Consumer.helper',
        'cls.code':'method:consumer.py:Consumer.code','saved.run':'method:consumer.py:Consumer.run',
        'Service.label':'method:service.py:Service.label','ImportedConsumer.kind':'method:consumer.py:Consumer.kind'}
    for expression,target in expected.items():
        site=next(s for s in graph.call_sites if s['expression']==expression)
        assert site['target_id']==target and site['resolution']=='static' and site['flow_chain']
        edges=[e for e in graph.edges if e.site_id==site['id']]
        assert {e.type for e in edges}=={'calls','references'}
        assert all(e.target==target and e.start_line==site['start_line'] and e.path==site['path'] for e in edges)
    assert get_function_callers(graph,'method:service.py:Service.work')
    tool=query_code_graph(ToolContext(context.root,retrieval_context=context),'callees','method:consumer.py:Consumer.run')
    assert tool.ok and any(e['target']=='method:service.py:Service.work' for e in tool.data['edges'])
    assert any(s['flow_evidence'] for s in tool.data['call_sites']) and 'flow_facts' not in tool.data
    result=retrieve(RetrievalRequest(Query('run','general')),replace(context,retrieval_mode='hybrid'))
    assert result.graph_schema_version==4 and any(i.path=='service.py' and i.symbol=='Service.work' for i in result.items)
    assert len(calls)==before
    plain=PyCodeEngine().build_index(context.root,include_chunk_metadata=True)
    assert context.chunks==build_chunk_index(plain,context.binding)
    save_chunk_index(context.chunks,tmp_path/'new.json');save_chunk_index(build_chunk_index(plain,context.binding),tmp_path/'old.json')
    save_index(index,tmp_path/'index.json');save_index(plain,tmp_path/'plain.json')
    assert (tmp_path/'new.json').read_bytes()==(tmp_path/'old.json').read_bytes()
    assert (tmp_path/'index.json').read_bytes()==(tmp_path/'plain.json').read_bytes()


@pytest.mark.parametrize('source,expression,target',[
    ('def f():\n    pass\nalias = f\nalias()\n','alias','func:a.py:f'),
    ('class C:\n    def work(self):\n        pass\ndef run():\n    obj=C()\n    second=obj\n    second.work()\n','second.work','method:a.py:C.work'),
    ('class C:\n    def work(self):\n        pass\n    def run(me):\n        me.work()\n','me.work','method:a.py:C.work'),
    ('class C:\n    @staticmethod\n    def work():\n        pass\nC.work()\n','C.work','method:a.py:C.work'),
    ('class C:\n    @classmethod\n    def work(cls):\n        cls.helper()\n    @staticmethod\n    def helper():\n        pass\nC.work()\n','cls.helper','method:a.py:C.helper'),
    ('class C:\n    def work(self):\n        pass\n    def run(self):\n        other=self\n        other.work()\n','other.work','method:a.py:C.work'),
    ('class C:\n    def work(self):\n        pass\nc=C()\nmethod=c.work\nmethod()\n','method','method:a.py:C.work'),
])
def test_closed_positive_forms(tmp_path,source,expression,target):
    _,graph,_=build(tmp_path,{'a.py':source})
    site=next(s for s in graph.call_sites if s['expression']==expression)
    assert site['resolution']=='static' and site['target_id']==target and site['flow_chain']


@pytest.mark.parametrize('source,expression',[
    ('class C:\n    def work(self):\n        pass\n    def run(self):\n        alias=self\n        external(alias)\n        self.work()\n','self.work'),
    ('class C:\n    def work(self):\n        pass\nobj=C()\nalias=obj\nexternal(alias)\nobj.work()\n','obj.work'),
    ('class C:\n    def work(self):\n        pass\nAlias=C\nexternal(Alias)\nC.work()\n','C.work'),
])
def test_escaped_alias_invalidates_original_nominal_receiver(tmp_path,source,expression):
    _,graph,_=build(tmp_path,{'a.py':source})
    site=next(s for s in graph.call_sites if s['expression']==expression)
    assert site['resolution']=='unknown' and site['target_id'] is None
    assert site['basis'] in {'receiver_escaped','class_escaped'}
    assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)


@pytest.mark.parametrize('binding,expression',[
    ('from service import C as Imported','Imported.work'),
    ('import service as Imported','Imported.C.work'),
])
def test_escaped_import_cannot_supply_a_trusted_receiver(tmp_path,binding,expression):
    _,graph,_=build(tmp_path,{'service.py':'class C:\n    def work(self):\n        pass\n',
        'main.py':binding+'\nexternal(Imported)\n'+expression+'()\n'})
    site=next(s for s in graph.call_sites if s['expression']==expression)
    assert site['resolution']=='unknown' and site['target_id'] is None
    assert site['basis']=='class_or_module_escaped'
    assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)


@pytest.mark.parametrize('escape',['','external(first)\n'])
def test_chained_assignment_does_not_invent_independent_receiver_bindings(tmp_path,escape):
    _,graph,_=build(tmp_path,{'a.py':'class C:\n    def work(self):\n        pass\nfirst=second=C()\n'+escape+'second.work()\n'})
    site=next(s for s in graph.call_sites if s['expression']=='second.work')
    assert site['resolution']=='unknown' and site['target_id'] is None
    assert all(a['value']['kind']=='unknown' for f in graph.flow_facts for a in f['assignments'])
    assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)


@pytest.mark.parametrize('tail,expression',[
    ('obj=factory()\nobj.work()\n','obj.work'),
    ('obj=C()\nobj=D()\nobj.work()\n','obj.work'),
    ('if flag:\n    obj=C()\nobj.work()\n','obj.work'),
    ('obj.work()\nobj=C()\n','obj.work'),
    ('obj=C()\nobj.work=replacement\nobj.work()\n','obj.work'),
    ('C.work=replacement\nobj=C()\nobj.work()\n','obj.work'),
    ('obj=C()\nexternal(obj)\nobj.work()\n','obj.work'),
    ('obj=C()\nobj += value\nobj.work()\n','obj.work'),
    ('obj=C()\ndel obj\nobj.work()\n','obj.work'),
    ('obj=C()\nsetattr(obj,"work",replacement)\nobj.work()\n','obj.work'),
    ('obj=D()\nobj.work()\n','obj.work'),
    ('obj,other=pair\nobj.work()\n','obj.work'),
])
def test_unconfirmed_flow_does_not_choose_another_named_method(tmp_path,tail,expression):
    source='class C:\n    def work(self):\n        pass\nclass D(Base):\n    def work(self):\n        pass\n'+tail
    _,graph,_=build(tmp_path,{'a.py':source,'other.py':'def work():\n    pass\n'})
    site=next(s for s in reversed(graph.call_sites) if s['expression']==expression)
    assert site['resolution'] in {'unknown','ambiguous'} and site['target_id'] is None
    assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)


@pytest.mark.parametrize('body',[
    'if flag:\n            self.service=Service()',
    'self.service=Service()\n        self.service=Service()',
    'self.service=dependency',
    'self.service=factory()',
    'self.service=Service()\n        external(self)',
    'self.service=Service()\n        return',
])
def test_initializer_summary_rejects_branch_rebinding_di_factory_escape_and_control(tmp_path,body):
    source='class Service:\n    def work(self):\n        pass\nclass C:\n    def __init__(self,dependency=None):\n        '+body+'\n    def run(self):\n        self.service.work()\n'
    _,graph,_=build(tmp_path,{'a.py':source})
    site=next(s for s in graph.call_sites if s['expression']=='self.service.work')
    assert site['resolution']=='unknown' and site['target_id'] is None


@pytest.mark.parametrize('class_body',[
    'def __getattr__(self,name):\n        pass',
    'def __getattribute__(self,name):\n        pass',
    'def __setattr__(self,name,value):\n        pass',
    'def __new__(cls):\n        pass',
    '@property\n    def work(self):\n        pass',
])
def test_descriptor_and_constructor_hooks_are_not_guessed(tmp_path,class_body):
    _,graph,_=build(tmp_path,{'a.py':'class C:\n    '+class_body+'\n    def other(self):\n        pass\nobj=C()\nobj.work()\n'})
    site=next(s for s in graph.call_sites if s['expression']=='obj.work')
    assert site['resolution']=='unknown'


def test_static_receiver_and_shadowed_builtin_decorators_are_unknown(tmp_path):
    _,graph,_=build(tmp_path,{'a.py':'def classmethod(value):\n    return value\nclass C:\n    @classmethod\n    def work(cls):\n        cls.helper()\n    @staticmethod\n    def static(self):\n        self.helper()\n    def helper(self):\n        pass\nC.work()\n'})
    assert all(s['resolution']=='unknown' for s in graph.call_sites if s['expression'] in {'cls.helper','self.helper','C.work'})


@pytest.mark.parametrize('damage',['target','chain','assignment','mutation','method','escape','range','missing','version'])
def test_schema4_rejects_forged_flow_proof(tmp_path,damage):
    _,graph,_=build(tmp_path);data=deepcopy(graph_to_dict(graph))
    site=next(s for s in data['call_sites'] if s['expression']=='saved.run')
    facts=next(f for f in data['flow_facts'] if f['path']=='main.py')
    if damage=='target':site['target_id']='method:service.py:Service.work';site['candidate_ids']=[site['target_id']]
    elif damage=='chain':site['flow_chain']=[]
    elif damage=='assignment':facts['assignments'][-1]['value']['parts']=['Service']
    elif damage=='mutation':next(f for f in data['flow_facts'] if f['path']=='consumer.py')['mutations']=[]
    elif damage=='method':next(f for f in data['flow_facts'] if f['path']=='consumer.py')['methods'][0]['receiver']='forged'
    elif damage=='escape':facts['escapes'].append({'scope_id':'func:main.py:main','call_id':site['id'],'names':['saved']})
    elif damage=='range':facts['assignments'][0]['end_line']=99999
    elif damage=='missing':data['flow_facts']=[]
    else:data['schema_version']=5
    with pytest.raises(ValueError):graph_from_dict(data)


def test_loop_and_field_cycles_obey_existing_bounds(tmp_path):
    _,graph,_=build(tmp_path,{'a.py':'class C:\n    def __init__(self):\n        self.other=C()\n    def work(self):\n        self.other.other.work()\n'})
    assert graph_from_dict(graph_to_dict(graph))==graph
    site=next(s for s in graph.call_sites if s['expression']=='self.other.other.work')
    assert site['resolution']=='unknown' and site['basis']=='cyclic_initializer'
    _,stats=RetrievalGraph.create(graph).expand(['method:a.py:C.work'],depth=4,node_limit=3,edge_limit=5,direction='both')
    assert stats['visited_nodes']<=3 and stats['edge_checks']<=5


@pytest.mark.parametrize('patch',[
    '    def reset(self):\n        self.service=Service()\n',
    '    def reset(self):\n        del self.service\n',
    '    def reset(self):\n        self.service += value\n',
])
def test_other_method_attribute_writes_invalidate_initializer_summary(tmp_path,patch):
    source='class Service:\n    def work(self):\n        pass\nclass C:\n    def __init__(self):\n        self.service=Service()\n'+patch+'    def run(self):\n        self.service.work()\n'
    _,graph,_=build(tmp_path,{'a.py':source})
    assert next(s for s in graph.call_sites if s['expression']=='self.service.work')['resolution']=='unknown'


def test_lexical_escape_and_external_mutation_do_not_create_nominal_edges(tmp_path):
    source='class C:\n    def work(self):\n        pass\nobj=C()\ndef escape():\n    external(obj)\ndef run():\n    obj.work()\n'
    _,graph,_=build(tmp_path,{'a.py':source})
    assert next(s for s in graph.call_sites if s['expression']=='obj.work')['resolution']=='unknown'


def test_closure_escape_invalidates_receiver_without_inferring_cross_function_types(tmp_path):
    source='class C:\n    def work(self):\n        pass\n    def run(self):\n        def inner():\n            external(self)\n        self.work()\n'
    _,graph,_=build(tmp_path,{'a.py':source})
    assert next(s for s in graph.call_sites if s['expression']=='self.work')['resolution']=='unknown'


@pytest.mark.parametrize('suffix',[
    'obj=C()\nobj.__class__=Other\nobj.work()\n',
    'C.__bases__=bases\nobj=C()\nobj.work()\n',
])
def test_visible_class_mutations_are_not_treated_as_stable_receiver_types(tmp_path,suffix):
    _,graph,_=build(tmp_path,{'a.py':'class C:\n    def work(self):\n        pass\n'+suffix})
    assert next(s for s in graph.call_sites if s['expression']=='obj.work')['resolution']=='unknown'


def test_literal_large_argument_names_do_not_corrupt_base_snapshot_graph(tmp_path):
    names=','.join('arg'+str(i) for i in range(40))
    _,graph,_=build(tmp_path,{'a.py':'def work('+names+'):\n    pass\nwork('+names+')\n'})
    assert graph.schema_version==4 and graph_from_dict(graph_to_dict(graph))==graph


def test_schema3_retains_its_unknown_receiver_capability_boundary(tmp_path):
    _,graph,context=build(tmp_path)
    old=schema3(graph)
    assert old.schema_version==3 and 'local_receiver_bindings' not in old.capabilities
    assert next(s for s in old.call_sites if s['expression']=='self.helper')['resolution']=='unknown'
    assert next(s for s in graph.call_sites if s['expression']=='self.helper')['target_id']=='method:consumer.py:Consumer.helper'
    assert graph_to_dict(graph_from_dict(graph_to_dict(old)))==graph_to_dict(old)
    assert 'flow_facts' not in graph_to_dict(old)
    validate_graph(old,context.binding,context.chunks)

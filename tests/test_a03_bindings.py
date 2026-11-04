"""Import binding contracts using source as data, with no model or code execution."""
import ast
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from pycode.chunks import SnapshotBinding,build_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.graph_schema import graph_from_dict,graph_to_dict,validate_graph
from pycode.query import RetrievalGraph,find_definitions,get_function_callers
from pycode.retrieval_evidence import RetrievalContext,RetrievalRequest,Query
from pycode.retriever import retrieve
from pycode.tools import ToolContext,query_code_graph


FIXTURE=Path(__file__).parent/'fixtures/a03/imports'


def sources():
    return {p.relative_to(FIXTURE).as_posix():p.read_text(encoding='utf-8') for p in FIXTURE.rglob('*.py')}


def build(tmp_path,files=None):
    root=tmp_path/'repo';root.mkdir()
    for path,source in (sources() if files is None else files).items():
        target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source,encoding='utf-8')
    index=PyCodeEngine().build_index(root,include_chunk_metadata=True,include_graph_metadata=True)
    binding=SnapshotBinding(str(uuid4()),str(uuid4()),'a'*40)
    graph=build_code_graph(index,binding=binding)
    return index,graph,RetrievalContext.create(root,index,graph,binding=binding,chunks=build_chunk_index(index,binding))


def test_import_reexport_calls_are_stable_and_shared_by_graph_tool_and_hybrid(tmp_path,monkeypatch):
    calls=[];parse=ast.parse
    def count(*args,**kwargs):calls.append(kwargs.get('filename'));return parse(*args,**kwargs)
    monkeypatch.setattr(ast,'parse',count)
    index,graph,context=build(tmp_path)
    assert len(calls)==4
    before=len(calls)
    assert graph_to_dict(graph)==graph_to_dict(build_code_graph(replace(index,files=index.files[::-1]),binding=context.binding))
    assert graph_from_dict(graph_to_dict(graph))==graph and len(calls)==before
    assert graph.schema_version==4 and 'import_bindings' in graph.capabilities
    sites={s['expression']:s for s in graph.call_sites if s['caller_id']=='func:client.py:run'}
    assert {k:s['target_id'] for k,s in sites.items()}=={
        'pkg.public_serve':'func:pkg/api.py:serve','api.serve':'func:pkg/api.py:serve',
        'public_serve':'func:pkg/api.py:serve','Constructor':'class:pkg/lib.py:Service'}
    for site in sites.values():
        assert site['basis']=='import_binding_chain' and site['resolution']=='static' and site['binding_chain']
        assert site['start_line']==site['end_line'] and site['path']=='client.py'
        edges=[e for e in graph.edges if e.site_id==site['id']]
        assert {e.type for e in edges}=={'calls','references'}
        assert all(e.target==site['target_id'] and e.start_line==site['start_line'] for e in edges)
    assert len(sites['pkg.public_serve']['binding_chain'])==2
    assert len(get_function_callers(graph,'pkg/api.py:serve'))==3
    for kind in ('definitions','callers','callees'):
        result=query_code_graph(ToolContext(context.root,retrieval_context=context),kind,
                                'pkg/api.py:serve' if kind!='callees' else 'func:client.py:run')
        assert result.ok and result.data['nodes'] and result.data['graph']['binding']==graph.binding
        assert 'binding_facts' not in result.data and all('expression' not in s for s in result.data['call_sites'])
    hybrid=retrieve(RetrievalRequest(Query('run','general')),replace(context,retrieval_mode='hybrid'))
    assert hybrid.graph_schema_version==4
    assert any(i.path=='pkg/api.py' and i.symbol=='serve' and any(score.source=='graph' for score in i.source_scores) for i in hybrid.items)
    assert any(step['basis']=='import_binding_chain' for i in hybrid.items for step in i.graph_path)
    assert len(calls)==before


@pytest.mark.parametrize('statement,expression',[
    ('import other','other.helper'),('import other as m','m.helper'),
    ('from other import helper','helper'),('from other import helper as alias','alias'),
    ('import pkg.dep','pkg.dep.helper'),('import pkg.dep as dep','dep.helper'),
    ('from pkg import dep','dep.helper'),
])
def test_import_forms_bind_only_the_declared_module(tmp_path,statement,expression):
    files={'main.py':statement+'\ndef run():\n    '+expression+'()\n',
           'other.py':'def helper():\n    pass\n','pkg/dep.py':'def helper():\n    pass\n',
           'unrelated.py':'def helper():\n    pass\n'}
    _,graph,_=build(tmp_path,files)
    site=graph.call_sites[0]
    assert site['target_id']==('func:pkg/dep.py:helper' if 'pkg' in statement else 'func:other.py:helper')
    assert site['binding_chain'] and not any(e.target=='func:unrelated.py:helper' for e in graph.edges if e.type=='calls')


@pytest.mark.parametrize('path,statement,target',[
    ('pkg/main.py','from .dep import helper','pkg/dep.py'),
    ('pkg/sub/main.py','from ..dep import helper','pkg/dep.py'),
    ('pkg/sub/main.py','from .dep import helper','pkg/sub/dep.py'),
])
def test_relative_import_uses_package_context(tmp_path,path,statement,target):
    _,graph,_=build(tmp_path,{path:statement+'\ndef run():\n    helper()\n',
        'pkg/dep.py':'def helper():\n    pass\n','pkg/sub/dep.py':'def helper():\n    pass\n'})
    assert graph.call_sites[0]['target_id']=='func:'+target+':helper'
    assert any(e.type=='imports' and e.target=='file:'+target for e in graph.edges)


@pytest.mark.parametrize('source,basis',[
    ('from other import helper\ndef run(helper):\n    helper()\n','binding_not_a_unique_definition'),
    ('from other import helper\nhelper = factory\ndef run():\n    helper()\n','binding_not_a_unique_definition'),
    ('if enabled:\n    from other import helper\ndef run():\n    helper()\n','conditional_import_binding'),
    ('from other import helper\nfrom other import helper\ndef run():\n    helper()\n','binding_not_a_unique_definition'),
    ('from missing import helper\ndef run():\n    helper()\n','module_not_in_snapshot'),
    ('from ..other import helper\ndef run():\n    helper()\n','relative_import_outside_package'),
    ('from other import *\ndef run():\n    helper()\n','dynamic_or_nonlocal_namespace'),
    ('import other\ndef run(other):\n    other.helper()\n','receiver_or_dynamic_expression_deferred'),
    ('def run():\n    helper()\n    from other import helper\n','import_not_available_at_site'),
    ('from other import helper\ndef run():\n    globals()[key] = value\n    helper()\n','dynamic_or_nonlocal_namespace'),
])
def test_shadowed_conditional_external_and_dynamic_calls_remain_unknown(tmp_path,source,basis):
    _,graph,_=build(tmp_path,{'main.py':source,'other.py':'def helper():\n    pass\n'})
    site=next(s for s in reversed(graph.call_sites) if s['expression'] in {'helper','other.helper'})
    assert (site['resolution'],site['basis'],site['target_id'])==('unknown',basis,None)
    assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)


def test_local_import_does_not_bind_another_scope_and_instance_alias_is_deferred(tmp_path):
    _,graph,_=build(tmp_path,{'main.py':'def one():\n    from other import helper\n    helper()\n'
        'def two():\n    helper()\nfrom other import Service\nobj = Service()\ndef three():\n    obj.method()\n',
        'other.py':'def helper():\n    pass\nclass Service:\n    def method(self):\n        pass\n'})
    assert next(s for s in graph.call_sites if s['caller_id']=='func:main.py:one')['target_id']=='func:other.py:helper'
    assert next(s for s in graph.call_sites if s['caller_id']=='func:main.py:two')['resolution']=='unknown'
    assert next(s for s in graph.call_sites if s['expression']=='obj.method')['target_id']=='method:other.py:Service.method'


@pytest.mark.parametrize('mode',['collision','cycle','budget','dynamic_export','duplicate_export','src_root'])
def test_module_ambiguity_reexports_and_bounds_fail_conservatively(tmp_path,mode):
    files={'main.py':'from other import helper\ndef run():\n    helper()\n'}
    if mode=='collision':files.update({'other.py':'def helper():\n    pass\n','other/__init__.py':'def helper():\n    pass\n'})
    elif mode=='cycle':files.update({'other.py':'from again import helper\n','again.py':'from other import helper\n'})
    elif mode=='budget':
        files['other.py']='from chain0 import helper\n'
        files.update({f'chain{i}.py':f'from chain{i+1} import helper\n' for i in range(24)})
        files['chain24.py']='def helper():\n    pass\n'
    elif mode=='dynamic_export':files['other.py']='exec(source)\ndef helper():\n    pass\n'
    elif mode=='duplicate_export':files['other.py']='def helper():\n    pass\ndef helper():\n    pass\n'
    else:files['src/other.py']='def helper():\n    pass\n'
    _,graph,_=build(tmp_path,files)
    site=next(s for s in graph.call_sites if s['caller_id']=='func:main.py:run')
    assert site['target_id'] is None
    assert site['resolution']==('ambiguous' if mode=='duplicate_export' else 'unknown')
    expected={'collision':'ambiguous_module','cycle':'cyclic_import_binding','budget':'import_lookup_budget_exceeded',
              'dynamic_export':'dynamic_or_nonlocal_namespace','duplicate_export':'multiple_lexical_definitions','src_root':'module_not_in_snapshot'}
    assert site['basis']==expected[mode]


@pytest.mark.parametrize('damage',['target','proof','declaration','write','flags','missing_scope','references','import_edge','version'])
def test_schema3_rejects_forged_binding_proof_and_relations(tmp_path,damage):
    _,graph,_=build(tmp_path);data=deepcopy(graph_to_dict(graph))
    site=next(s for s in data['call_sites'] if s['expression']=='public_serve')
    if damage=='target':site['target_id']='func:pkg/lib.py:work';site['candidate_ids']=[site['target_id']]
    elif damage=='proof':site['binding_chain']=[]
    elif damage=='declaration':data['import_declarations'][0]['names'][0]['asname']='forged'
    elif damage=='write':
        root=next(f for f in data['binding_facts'] if f['path']=='client.py')['scopes'][0]
        root['bindings']['public_serve'].append({'kind':'write','target':None,'conditional':False})
    elif damage=='flags':
        next(d for f in data['binding_facts'] for d in f['definitions'] if d['id']=='func:pkg/api.py:serve')['decorated']=True
    elif damage=='missing_scope':data['binding_facts'][0]['scopes']=[]
    elif damage=='references':data['edges']=[e for e in data['edges'] if not(e['type']=='references' and e['site_id']==site['id'])]
    elif damage=='import_edge':next(e for e in data['edges'] if e['type']=='imports')['target']='file:pkg/lib.py'
    else:data['schema_version']=99
    with pytest.raises(ValueError):graph_from_dict(data)


def test_schema2_shape_remains_readable_and_cannot_claim_cross_module_bindings(tmp_path):
    _,graph,context=build(tmp_path,{'main.py':'def helper():\n    pass\ndef run():\n    helper()\n',
                                    'other.py':'def helper():\n    pass\n'})
    data=graph_to_dict(graph);data['schema_version']=2;data.pop('binding_facts');data.pop('flow_facts')
    data['capabilities']=['lexical_scopes','call_sites','import_declarations','same_module_direct_calls']
    for site in data['call_sites']:site.pop('binding_chain');site.pop('flow_chain')
    old=graph_from_dict(data)
    assert graph_to_dict(old)==data and old.schema_version==2
    validate_graph(old,context.binding,context.chunks)
    forged=deepcopy(data);site=forged['call_sites'][0]
    site['target_id']='func:other.py:helper';site['candidate_ids']=[site['target_id']]
    next(e for e in forged['edges'] if e['type']=='calls')['target']=site['target_id']
    with pytest.raises(ValueError):graph_from_dict(forged)
    assert graph_from_dict({'project_path':'old','nodes':[],'edges':[]}).schema_version==1


def test_definition_queries_return_ambiguity_and_not_first_short_name(tmp_path):
    _,graph,context=build(tmp_path,{'one.py':'def work():\n    pass\n','two.py':'def work():\n    pass\n'})
    assert len(find_definitions(graph,'work'))==2
    result=query_code_graph(ToolContext(context.root,retrieval_context=context),'callers','work')
    assert result.ok and result.data['resolution']=='ambiguous' and not result.data['edges']
    assert get_function_callers(graph,'work')==[]


def test_dynamic_package_exports_and_search_path_do_not_confirm_module_calls(tmp_path):
    _,graph,_=build(tmp_path,{'main.py':'from pkg import dep\ndef run():\n    dep.helper()\n',
        'pkg/__init__.py':'def __getattr__(name):\n    return factory(name)\n',
        'pkg/dep.py':'def helper():\n    pass\n'})
    site=next(s for s in graph.call_sites if s['expression']=='dep.helper')
    assert site['resolution']=='unknown' and site['basis']=='dynamic_module_export'


@pytest.mark.parametrize('statement,expression',[('import pkg.dep as dep','dep.helper'),('from pkg.dep import helper','helper')])
def test_mutated_package_search_path_does_not_bind_snapshot_module(tmp_path,statement,expression):
    _,graph,_=build(tmp_path,{'main.py':statement+'\ndef run():\n    '+expression+'()\n',
        'pkg/__init__.py':'__path__ = external_path\n','pkg/dep.py':'def helper():\n    pass\n'})
    assert graph.call_sites[0]['resolution']=='unknown' and graph.call_sites[0]['basis']=='dynamic_package_path'


@pytest.mark.parametrize('files',[
    {'main.py':'import odd.name as m\ndef run():\n    m.helper()\n','odd.name.py':'def helper():\n    pass\n'},
    {'odd.name.py':'from .target import helper\ndef run():\n    helper()\n','odd/target.py':'def helper():\n    pass\n'},
])
def test_dotted_filenames_do_not_fabricate_package_roots(tmp_path,files):
    _,graph,_=build(tmp_path,files)
    assert len(graph.call_sites)==1 and graph.call_sites[0]['resolution']=='unknown'


def test_import_lookup_work_and_graph_cycles_are_bounded(tmp_path,monkeypatch):
    from pycode.symbol_resolver import Lookup,MAX_CHECKS
    lookups=[];original=Lookup.__init__
    def record(self):original(self);lookups.append(self)
    monkeypatch.setattr(Lookup,'__init__',record)
    _,graph,_=build(tmp_path,{'main.py':'from other import helper\ndef run():\n    helper()\n',
        'other.py':'from again import helper\n','again.py':'from other import helper\n'})
    assert lookups and all(l.checks<=MAX_CHECKS+1 for l in lookups)
    for direction in ('incoming','outgoing','both'):
        _,stats=RetrievalGraph.create(graph).expand(['file:main.py'],depth=4,node_limit=3,edge_limit=5,direction=direction)
        assert stats['visited_nodes']<=3 and stats['edge_checks']<=5

"""A03a lexical identity, conservative resolution and versioned facts."""
import ast
from copy import deepcopy
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from pycode.chunks import SnapshotBinding, build_chunk_index, save_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.graph_schema import graph_from_dict, graph_info, graph_to_dict, validate_graph
from pycode.models import CodeGraph, FileInfo, ProjectIndex
from pycode.query import RetrievalGraph, get_function_calls
from pycode.retrieval_evidence import Query, RetrievalContext, RetrievalRequest
from pycode.retriever import retrieve
from pycode.storage import load_graph, save_graph, save_index
from pycode.tools import ToolContext, query_code_graph


def binding():
    return SnapshotBinding(str(uuid4()),str(uuid4()),'a'*40)


def build(root, sources, bound=None, chunks=False):
    root.mkdir(parents=True,exist_ok=True)
    for path,source in sources.items():
        file=root/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(source,encoding='utf-8')
    index=PyCodeEngine().build_index(root,include_graph_metadata=True,include_chunk_metadata=chunks)
    return index,build_code_graph(index,binding=bound)


def test_scopes_duplicate_identity_scan_order_and_chunk_contract(tmp_path,monkeypatch):
    sources={'a.py': '@decorator\ndef repeat():\n    return 1\ndef repeat():\n    return 2\n'
             'def outer():\n    def nested():\n        return 3\n    return nested()\n'
             'class Same:\n    def method(self):\n        return 4\n'
             'class Same:\n    def method(self):\n        return 5\n',
             'b.py':'def repeat():\n    return 9\n'}
    bound=binding();calls=[];original=ast.parse
    def parse(*args,**kwargs):
        calls.append(kwargs.get('filename'));return original(*args,**kwargs)
    monkeypatch.setattr(ast,'parse',parse)
    index,graph=build(tmp_path/'repo',sources,bound,True)
    assert len(calls)==len(sources)
    before=len(calls)
    reverse=build_code_graph(replace(index,files=list(reversed(index.files))),binding=bound)
    assert graph_to_dict(graph)==graph_to_dict(reverse) and len(calls)==before
    assert len({n.id for n in graph.nodes})==len(graph.nodes)
    assert len([n for n in graph.nodes if n.path=='a.py' and n.name=='repeat'])==2
    assert len([n for n in graph.nodes if n.name=='Same.method'])==2
    nested=next(n for n in graph.nodes if n.name=='outer.nested')
    outer=next(n for n in graph.nodes if n.name=='outer')
    assert nested.parent_id==outer.id and nested.symbol_key=='<module>/outer[1]/nested[1]'
    chunks=build_chunk_index(index,bound)
    plain=PyCodeEngine().build_index(tmp_path/'repo',include_chunk_metadata=True)
    assert chunks==build_chunk_index(plain,bound)
    save_chunk_index(chunks,tmp_path/'first.json');save_chunk_index(build_chunk_index(plain,bound),tmp_path/'second.json')
    assert (tmp_path/'first.json').read_bytes()==(tmp_path/'second.json').read_bytes()
    save_index(index,tmp_path/'index.json');save_index(plain,tmp_path/'plain.json')
    assert (tmp_path/'index.json').read_bytes()==(tmp_path/'plain.json').read_bytes()
    validate_graph(graph,bound,chunks)


@pytest.mark.parametrize('source,status,basis',[
    ('def helper():\n    pass\ndef run():\n    helper()\n','static','unique_same_module_lexical_definition'),
    ('def helper():\n    pass\ndef run(helper):\n    helper()\n','unknown','binding_not_a_unique_definition'),
    ('def helper():\n    pass\ndef helper():\n    pass\ndef run():\n    helper()\n','ambiguous','multiple_lexical_definitions'),
    ('def helper():\n    pass\ndef run():\n    helper = factory\n    helper()\n','unknown','binding_not_a_unique_definition'),
    ('def helper():\n    pass\nclass Client:\n    def helper(self):\n        pass\n    def run(self):\n        self.helper()\n','unknown','receiver_or_dynamic_expression_deferred'),
    ('from other import helper as alias\ndef run():\n    alias()\n','unknown','binding_not_a_unique_definition'),
    ('if flag:\n    def helper():\n        pass\ndef run():\n    helper()\n','unknown','conditional_or_decorated_definition'),
    ('@wrap\ndef helper():\n    pass\ndef run():\n    helper()\n','unknown','conditional_or_decorated_definition'),
    ('def helper():\n    pass\ndef mutate():\n    global helper\n    helper = factory\ndef run():\n    helper()\n','unknown','dynamic_or_nonlocal_namespace'),
    ('def helper():\n    pass\ndef run():\n    exec(code)\n    helper()\n','unknown','dynamic_or_nonlocal_namespace'),
    ('def run():\n    return [helper() for n in values]\n','unknown','opaque_scope'),
    ('helper()\ndef helper():\n    pass\n','unknown','definition_not_available_at_site'),
    ('def helper():\n    pass\ndef run():\n    [helper := x for x in values]\n    helper()\n','unknown','binding_not_a_unique_definition'),
    ('def helper():\n    pass\ndef run[helper]():\n    helper()\n','unknown','dynamic_or_nonlocal_namespace'),
    ('def helper():\n    pass\nhelper.__code__ = other\ndef run():\n    helper()\n','unknown','dynamic_or_nonlocal_namespace'),
])
def test_only_reliable_lexical_calls_connect_to_definitions(tmp_path,source,status,basis):
    _,graph=build(tmp_path/'repo',{'a.py':source,'other.py':'def helper():\n    pass\n'})
    site=next(s for s in reversed(graph.call_sites) if s['expression'] in {'helper','self.helper','alias'})
    assert (site['resolution'],site['basis'])==(status,basis)
    edges=[e for e in graph.edges if e.site_id==site['id']]
    assert len(edges)==1
    if status=='static':
        assert edges[0].type=='calls' and edges[0].target=='func:a.py:helper'
    else:
        assert edges[0].type=='unresolved_call' and site['target_id'] is None
        assert not any(e.type=='calls' and e.site_id==site['id'] for e in graph.edges)
    assert not any(e.type=='calls' and e.target=='func:other.py:helper' for e in graph.edges)


def test_call_sites_not_deduplicated_and_import_declarations_are_lossless(tmp_path):
    source='import pkg.mod as mod\nfrom .dep import work as alias\nfrom .. import value\n'
    source+='def helper():\n    pass\ndef run():\n    helper(); helper()\n'
    _,graph=build(tmp_path/'repo',{'pkg/a.py':source})
    assert len(graph.call_sites)==2 and len({s['id'] for s in graph.call_sites})==2
    assert len([e for e in graph.edges if e.type=='calls'])==2
    declarations=graph.import_declarations
    assert declarations[0]['names']==[{'name':'pkg.mod','asname':'mod'}]
    assert declarations[1]['module']=='dep' and declarations[1]['level']==1
    assert declarations[1]['names']==[{'name':'work','asname':'alias'}]
    assert declarations[2]['module'] is None and declarations[2]['level']==2
    assert [d['start_line'] for d in declarations]==[1,2,3]
    assert len(get_function_calls(graph,'func:pkg/a.py:run'))==2


def test_no_user_module_execution_and_module_collision_does_not_drift(tmp_path):
    sources={'main.py':"raise RuntimeError('must never execute')\nimport pkg\ndef run():\n    pkg.helper()\n",
             'pkg.py':'def helper():\n    pass\n','pkg/__init__.py':'def helper():\n    pass\n'}
    index,graph=build(tmp_path/'repo',sources)
    assert graph_to_dict(graph)==graph_to_dict(build_code_graph(replace(index,files=index.files[::-1])))
    assert any(e.target=='external:pkg' for e in graph.edges if e.type=='imports')


@pytest.mark.parametrize('damage',['version','boolean_version','duplicate_node','range','dangling','site_status','scope_cycle','unsafe_path','missing_field'])
def test_schema_rejects_invalid_version_and_structure(tmp_path,damage):
    _,graph=build(tmp_path/'repo',{'a.py':'def helper():\n    pass\ndef run():\n    helper()\n'})
    data=deepcopy(graph_to_dict(graph))
    if damage=='version':data['schema_version']=99
    elif damage=='boolean_version':data['schema_version']=True
    elif damage=='duplicate_node':data['nodes'].append(data['nodes'][0])
    elif damage=='range':next(n for n in data['nodes'] if n['type']=='function')['end_line']=100000
    elif damage=='dangling':data['edges'][0]['target']='foreign'
    elif damage=='site_status':data['call_sites'][0]['resolution']='unknown'
    elif damage=='scope_cycle':data['scopes'][0]['parent_id']=data['scopes'][0]['id']
    elif damage=='unsafe_path':data['files'][0]['path']='../escape.py'
    else:del data['call_sites'][0]['basis']
    with pytest.raises(ValueError):graph_from_dict(data)


def test_codec_old_shape_and_new_roundtrip_with_explicit_capability(tmp_path):
    legacy={'project_path':'old','nodes':[],'edges':[]}
    old=graph_from_dict(legacy)
    assert old.schema_version==1 and graph_info(old)['graph_warnings']==['legacy_graph_unverified']
    assert graph_to_dict(old)==legacy
    index,graph=build(tmp_path/'repo',{'a.py':'def run():\n    return 1\n'})
    save_graph(graph,tmp_path/'graph.json')
    assert load_graph(tmp_path/'graph.json')==graph
    assert graph_from_dict(json.loads((tmp_path/'graph.json').read_text()))==graph
    # A legacy Index reload is not silently re-parsed/upgraded.
    legacy_graph=build_code_graph(ProjectIndex('old',[FileInfo('a.py',functions=['run'])]))
    assert legacy_graph.schema_version==1


def test_snapshot_binding_and_chunk_sources_are_verified(tmp_path):
    bound=binding();index,graph=build(tmp_path/'repo',{'a.py':'def run():\n    return 1\n'},bound,True)
    chunks=build_chunk_index(index,bound)
    for foreign in (replace(bound,project_id=str(uuid4())),replace(bound,snapshot_id=str(uuid4())),replace(bound,commit_sha='b'*40)):
        with pytest.raises(ValueError):validate_graph(graph,foreign)
    bad=replace(graph,files=[{**graph.files[0],'source_sha256':'0'*64}])
    with pytest.raises(ValueError):validate_graph(bad,bound,chunks)
    with pytest.raises(ValueError):RetrievalContext.create(tmp_path/'repo',index,replace(graph,binding=None),binding=bound,chunks=chunks)


def test_query_tool_hybrid_exact_symbol_mapping_and_unknown_expansion(tmp_path):
    bound=binding();index,graph=build(tmp_path/'repo',{'a.py':
        'def helper():\n    pass\ndef helper():\n    pass\ndef run():\n    helper()\n    receiver.secret()\n'},bound,True)
    context=RetrievalContext.create(tmp_path/'repo',index,graph,binding=bound,chunks=build_chunk_index(index,bound))
    tool=query_code_graph(ToolContext(context.root,retrieval_context=context),'calls','func:a.py:helper')
    assert tool.ok and tool.data['resolution']=='ambiguous' and not tool.data['edges']
    assert tool.data['graph']['graph_schema_version']==2 and tool.data['graph']['ambiguous_count']==1
    calls=query_code_graph(ToolContext(context.root,retrieval_context=context),'calls','func:a.py:run')
    assert calls.ok and all(e['type']=='unresolved_call' for e in calls.data['edges'])
    assert all('expression' not in s for s in calls.data['call_sites'])
    hits,stats=RetrievalGraph.create(graph).expand(['func:a.py:run'],depth=1,node_limit=32,edge_limit=128,direction='outgoing')
    assert hits==[] and stats['unknown_relations']==2
    result=retrieve(RetrievalRequest(Query('helper','general')),replace(context,retrieval_mode='hybrid'))
    assert result.graph_schema_version==2 and result.pack
    assert {i.start_line for i in result.items if i.symbol=='helper'}=={1,3}


def test_native_nested_index_paths_match_canonical_graph_and_evidence(tmp_path):
    bound=binding();index,graph=build(tmp_path/'repo',{
        'pkg/target.py':'def helper():\n    return 1\n',
        'pkg/reader.py':'from pkg.target import helper\ndef read():\n    return helper()\n'},bound,True)
    context=RetrievalContext.create(tmp_path/'repo',index,graph,binding=bound,chunks=build_chunk_index(index,bound))
    result=retrieve(RetrievalRequest(Query('pkg/target.py 的影响','impact','pkg/target.py'),8),context)
    assert [i.path for i in result.items]==['pkg/target.py','pkg/reader.py']
    hybrid=retrieve(RetrievalRequest(Query('helper','general')),replace(context,retrieval_mode='hybrid'))
    assert any(i.path=='pkg/target.py' and i.symbol=='helper' for i in hybrid.items)


@pytest.mark.parametrize('damage',['column','expression','caller'])
def test_bound_call_sites_match_source_and_actual_owner_scope(tmp_path,damage):
    bound=binding();index,graph=build(tmp_path/'repo',{'a.py':'def helper():\n    pass\ndef run():\n    helper()\n'},bound,True)
    bad=deepcopy(graph);site=bad.call_sites[0]
    if damage=='column':
        site['col_offset']=9999;site['end_col_offset']=10000
    elif damage=='expression':site['expression']='forged'
    else:
        site['caller_id']='func:a.py:helper'
        next(e for e in bad.edges if e.site_id==site['id']).source=site['caller_id']
    with pytest.raises(ValueError):validate_graph(bad,bound,build_chunk_index(index,bound))


def test_type_parameter_bound_calls_are_recorded_but_not_resolved_as_module_execution(tmp_path):
    _,graph=build(tmp_path/'repo',{'a.py':'def helper():\n    return int\ndef run[T: helper()]():\n    pass\n'})
    site=graph.call_sites[0]
    assert site['expression']=='helper' and site['resolution']=='unknown' and site['basis']=='opaque_scope'


def test_generic_type_alias_call_does_not_resolve_to_shadowed_module_function(tmp_path):
    _,graph=build(tmp_path/'repo',{'a.py':'def helper():\n    pass\ntype Alias[helper] = helper()\n'})
    site=graph.call_sites[0]
    assert site['resolution']=='unknown' and site['basis']=='opaque_scope' and site['target_id'] is None
    assert not any(e.type=='calls' for e in graph.edges)


@pytest.mark.parametrize('operation',['plain','snapshot','graph'])
def test_legacy_engine_build_override_retains_capability_without_reparse(tmp_path,monkeypatch,operation):
    class LegacyEngine(PyCodeEngine):
        def build_index(self,project_path,*,include_chunk_metadata=False):
            return super().build_index(project_path,include_chunk_metadata=include_chunk_metadata)
    root=tmp_path/'repo';root.mkdir();(root/'a.py').write_text('def run():\n    pass\n',encoding='utf-8')
    calls=[];parse=ast.parse
    def count(*args,**kwargs):
        calls.append(kwargs.get('filename'));return parse(*args,**kwargs)
    monkeypatch.setattr(ast,'parse',count)
    engine=LegacyEngine();bound=binding()
    if operation=='graph':
        graph=engine.graph(root,tmp_path/'graph.json',binding=bound,include_graph_metadata=True)
    else:
        kwargs={'chunk_output_path':tmp_path/'chunks.json','binding':bound} if operation=='snapshot' else {}
        engine.index(root,tmp_path/'index.json',graph_output_path=tmp_path/'graph.json',**kwargs)
        graph=load_graph(tmp_path/'graph.json')
        assert (tmp_path/'chunks.json').exists()==(operation=='snapshot')
    assert len(calls)==1 and graph.schema_version==1
    assert graph_info(graph)['graph_warnings']==['legacy_graph_unverified']

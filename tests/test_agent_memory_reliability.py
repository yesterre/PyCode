"""A06a contracts, fault injection and execution control (no real models)."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pycode.agent.budget import ExecutionBudget, ExecutionStopped
from pycode.agent.memory import MemoryStore, extract_memories, load_relevant_memories
from pycode.agent.memory_record import MemoryScope, digest, provenance_for, record_identity
import pycode.agent.memory_record as records
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask, RuntimeConfig
from pycode.chunks import SnapshotBinding
from pycode.cli import memory_project
from pycode.tools import TOOLS, ToolContext, ToolSpec
from pycode.tools.base import success
from pycode.tools.memory_tools import memory


def binding():
    return SnapshotBinding(str(uuid4()), str(uuid4()), 'a' * 40)


def scope(root, bind=None):
    return MemoryScope(root, bind or binding(), str(uuid4()))


def add(store, **overrides):
    return store.add_memory(**(dict(memory_id='note', memory_type='analysis', title='Title',
                                  summary='entry hint', body='Original body') | overrides))


def rewrite(path, mutate):
    from pycode.agent.memory import _parse_memory_file
    meta, body = _parse_memory_file(path.read_text(encoding='utf-8'))
    mutate(meta)
    meta.pop('record_hash', None)
    meta['record_hash'] = digest(dict(metadata=meta, body=body))
    path.write_text('---\n' + '\n'.join(f'{k}: {json.dumps(v)}' for k,v in meta.items())
                    + '\n---\n' + body, encoding='utf-8')


@pytest.mark.parametrize('body', [' \n---\n正文 ☃\n---\n tail  ', '\r\n\r\n正文\r\n', 'a\u2028b\u2029c'])
def test_versioned_roundtrip_preserves_all_text_and_identity(tmp_path, body):
    s = MemoryStore(tmp_path)
    item = add(s, title='left---right ☃ : "\n', summary='---\n摘要', body=body, confidence=.4)
    loaded = s.load_memory(item.id)
    assert loaded.to_dict() == item.to_dict()
    assert loaded.body == body and loaded.confidence == .4 and loaded.schema_version == 3
    assert loaded.lifecycle['state']=='active' and loaded.lifecycle['scope']=='local'
    assert loaded.provenance['origin'] == 'local_unknown'
    assert loaded.record_id == record_identity(loaded.id, loaded.provenance)
    assert s.list_memories()[0].provenance == loaded.provenance


@pytest.mark.parametrize('version', [None, 1])
def test_legacy_read_is_unknown_without_forged_time_binding_or_upgrade(tmp_path, version):
    s = MemoryStore(tmp_path); s.memory_dir.mkdir(parents=True)
    path = s.memory_dir / 'old.md'
    prefix = '' if version is None else 'schema_version: 1\n'
    path.write_text('---\n' + prefix + 'id: "old"\ntype: "project"\ntitle: "left---right"\n---\n\nold body\n', encoding='utf-8')
    before = path.read_bytes()
    old = s.load_memory('old')
    assert old.schema_version == 1 and old.provenance is None and old.record_id is None
    assert old.source == 'legacy_unknown' and old.created_at == old.updated_at == ''
    assert old.title == 'left---right' and old.body == 'old body'
    with scope(tmp_path).activate():
        bound = MemoryStore(tmp_path)
        assert bound.list_memories() == [] and bound.read_index_text() == ''
        with pytest.raises(PermissionError): bound.load_memory('old')
    s.rebuild_index()
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match='upgrade'): add(s, memory_id='old', allow_existing=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize('name', ['MEMORY', 'memory.md', 'CON', 'PRN.txt', 'aux', 'NUL', 'COM1', 'LPT9'])
def test_reserved_names_cannot_damage_index(tmp_path, name):
    s = MemoryStore(tmp_path); add(s)
    before = s.index_path.read_bytes()
    with pytest.raises(ValueError): add(s, memory_id=name)
    assert s.index_path.read_bytes() == before and s.load_memory('note').body == 'Original body'


@pytest.mark.parametrize('damage', ['version', 'hash', 'identity', 'provenance', 'fields', 'json', 'utf8', 'oversize', 'deep_json', 'unicode_json'])
def test_one_bad_record_is_isolated_and_retained(tmp_path, damage):
    s = MemoryStore(tmp_path); add(s); add(s, memory_id='broken',title='Broken target')
    path = s.memory_dir / 'broken.md'
    if damage == 'version': rewrite(path, lambda m: m.update(schema_version=99))
    elif damage == 'identity': rewrite(path, lambda m: m.update(record_id='0'*64))
    elif damage == 'provenance': rewrite(path, lambda m: m['provenance'].update(status='evidence_backed'))
    elif damage == 'fields': rewrite(path, lambda m: m.update(tags='not a list'))
    elif damage == 'hash': path.write_bytes(path.read_bytes().replace(b'Original body', b'Modified body'))
    elif damage == 'json': path.write_text('---\nschema_version: 2\n:garbage\n---\nbody', encoding='utf-8')
    elif damage == 'utf8': path.write_bytes(b'\xff\xfe')
    elif damage == 'deep_json':
        path.write_text('---\nid: "broken"\ntype: "analysis"\ntags: '+ '['*5000+'0'+']'*5000+'\n---\nbody',encoding='utf-8')
    elif damage == 'unicode_json':
        path.write_text('---\nid: "broken"\ntype: "analysis"\n'+r'title: "\ud800"'+'\n---\nbody',encoding='utf-8')
    else: path.write_bytes(b'x' * (records.MAX_RECORD_BYTES+1))
    before = path.read_bytes()
    assert [e.id for e in s.list_memories()] == ['note']
    assert [i.id for i in s.search_memories('entry', include_body=True)] == ['note']
    assert 'record_unreadable' in s.diagnostics and len(s.diagnostics) <= 16
    assert s.rebuild_index()[0].id == 'note' and path.read_bytes() == before


def test_index_is_recoverable_cache_and_never_an_unfiltered_prompt(tmp_path):
    s = MemoryStore(tmp_path); add(s)
    for contents in (b'\xff', b'PRIVATE_FAKE_INDEX_DATA'):
        s.index_path.write_bytes(contents)
        assert 'note.md' in s.read_index_text() and 'PRIVATE' not in s.read_index_text()
        s.rebuild_index()
        assert 'note.md' in s.index_path.read_text(encoding='utf-8')
    s.index_path.unlink()
    assert 'note.md' in s.read_index_text()  # recovery view requires no write


@pytest.mark.parametrize('target', ['body', 'index'])
def test_atomic_failure_preserves_old_file_and_reports_commit_boundary(tmp_path, monkeypatch, target):
    s = MemoryStore(tmp_path); add(s)
    path = s.memory_dir / 'note.md'; before, index = path.read_bytes(), s.index_path.read_bytes()
    original = records.os.replace
    def fail(src, dst):
        if Path(dst).name == ('note-2.md' if target == 'body' else 'MEMORY.md'):
            raise PermissionError('PRIVATE_FAKE_FAILURE')
        return original(src, dst)
    monkeypatch.setattr(records.os, 'replace', fail)
    if target == 'body':
        with pytest.raises(PermissionError): add(s, body='new body', allow_existing=True)
        assert path.read_bytes() == before and s.index_path.read_bytes() == index
    else:
        item = add(s, body='new body', allow_existing=True)
        assert s.load_memory(item.id).body == item.body == 'new body'
        assert path.read_bytes()==before  # A revision never destroys its parent.
        with pytest.raises(PermissionError):s.load_memory('note')
        assert 'index_write_failed_body_committed' in s.diagnostics
        assert s.index_path.read_bytes() == index
    assert not list(s.memory_dir.glob('.memory-*.tmp'))
    monkeypatch.setattr(records.os, 'replace', original)
    assert s.rebuild_index()[0].id == ('note' if target=='body' else 'note-2')


def test_concurrent_stores_allocate_unique_ids_without_lost_records(tmp_path):
    def write(i): return add(MemoryStore(tmp_path),title='Independent target '+str(i), body=str(i)).id
    with ThreadPoolExecutor(max_workers=8) as pool: ids = list(pool.map(write, range(12)))
    s = MemoryStore(tmp_path)
    assert len(set(ids)) == 12 and len(s.list_memories()) == 12
    assert {s.load_memory(i).body for i in ids} == {str(i) for i in range(12)}
    assert len([line for line in s.index_path.read_text().splitlines() if line.startswith('- [')]) == 12


@pytest.mark.parametrize('limit', ['record', 'count', 'total'])
def test_capacity_rejects_without_truncation_eviction_or_old_file_damage(tmp_path, monkeypatch, limit):
    import pycode.agent.memory as module
    s = MemoryStore(tmp_path); add(s); before = (s.memory_dir/'note.md').read_bytes()
    if limit == 'record': monkeypatch.setattr(module, 'MAX_RECORD_BYTES', 100)
    if limit == 'count': monkeypatch.setattr(module, 'MAX_RECORDS', 1)
    if limit == 'total': monkeypatch.setattr(module, 'MAX_TOTAL_BYTES', len(before)+10)
    with pytest.raises(ValueError, match='capacity|count'): add(s, memory_id='other', body='新'*1000)
    assert not (s.memory_dir/'other.md').exists() and (s.memory_dir/'note.md').read_bytes() == before


@pytest.mark.parametrize('change', ['project', 'snapshot_same_commit', 'commit'])
def test_bound_memory_is_never_reused_across_identity(tmp_path, change):
    old = scope(tmp_path)
    with old.activate(): item = add(MemoryStore(tmp_path))
    new_binding = replace(old.binding, **{'project_id':str(uuid4())} if change=='project' else
                          {'snapshot_id':str(uuid4())} if change=='snapshot_same_commit' else {'commit_sha':'b'*40})
    with scope(tmp_path, new_binding).activate():
        s = MemoryStore(tmp_path)
        assert s.list_memories() == [] and s.read_index_text() == ''
        with pytest.raises(PermissionError): s.load_memory(item.id)
        selected, _ = load_relevant_memories(tmp_path, task_description='entry', messages=[])
        assert selected == []
    assert MemoryStore(tmp_path).load_memory(item.id).provenance['snapshot_id'] == old.binding.snapshot_id


def test_tools_cannot_assert_user_authority_or_override_server_scope(tmp_path):
    trusted = scope(tmp_path)
    with trusted.activate():
        result = memory(ToolContext(tmp_path), operation='add', id='model', memory_type='preference', body='Unverified')
        assert result.ok
        p = result.data['memory']['provenance']
        assert p == provenance_for(trusted, 'model_inference')
        with pytest.raises(PermissionError): MemoryStore(tmp_path, scope=scope(tmp_path))
        assert not memory(ToolContext(tmp_path/'other'), operation='list').ok
    schema = TOOLS['memory'].input_schema
    assert not {'project_id','snapshot_id','commit_sha','run_id','origin','status','evidence'} & schema['properties'].keys()


def test_cli_explicit_and_auto_extraction_do_not_trust_model_provenance(tmp_path,capsys):
    item = memory_project(tmp_path, 'add', memory_id='explicit', memory_type='preference', body='User text')
    assert item.provenance['origin'] == 'user_explicit'
    printed=capsys.readouterr().out
    assert 'schema=3' in printed and 'origin=user_explicit' in printed and 'status=declared' in printed
    path=tmp_path/'.pclens/memory/legacy.md'
    path.write_text('---\nid: "legacy"\ntype: "project"\nsource: "manual"\n---\nOld hint',encoding='utf-8')
    old=memory_project(tmp_path,'load',memory_id='legacy')
    printed=capsys.readouterr().out
    assert old.provenance is None
    assert 'schema=1' in printed and 'origin=legacy_unknown' in printed and 'status=unknown' in printed
    class Model:
        def generate(self, prompt):
            return json.dumps([dict(id='auto',type='analysis',body='Model text',origin='user_explicit',
                                    project_id=str(uuid4()),status='evidence_backed',evidence=['forged'])])
    trusted = scope(tmp_path)
    with trusted.activate():
        created, error = extract_memories(tmp_path, task_description='entry', messages=[],tool_results=[],answer='Answer',llm_client=Model())
    assert error is None and len(created)==1
    assert created[0].provenance == provenance_for(trusted,'model_inference')


def test_code_derived_requires_real_bound_chunk_ranges(tmp_path):
    from pycode.engine import PyCodeEngine
    from pycode.chunks import build_chunk_index
    from pycode.retrieval_evidence import RetrievalContext
    (tmp_path/'logic.py').write_text('def f():\n    return 1\n', encoding='utf-8')
    b = binding(); index = PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True)
    chunks = build_chunk_index(index, b)
    ctx = RetrievalContext.create(tmp_path,index,None,binding=b,chunks=chunks)
    c = chunks.chunks[0]; trusted = scope(tmp_path,b)
    ref = dict(path=c.path,start_line=c.span.start_line,end_line=c.span.end_line,chunk_ids=[c.id])
    s = MemoryStore(tmp_path,scope=trusted,origin='code_derived',evidence=(ref,),retrieval_context=ctx)
    assert add(s).provenance['status']=='evidence_backed'
    for bad in (dict(ref,chunk_ids=['forged']),dict(ref,end_line=c.span.end_line+100),dict(ref,path='elsewhere.py')):
        with pytest.raises(ValueError): add(MemoryStore(tmp_path,scope=trusted,origin='code_derived',evidence=(bad,),retrieval_context=ctx))
    with pytest.raises(ValueError): add(MemoryStore(tmp_path,origin='code_derived'))


@pytest.mark.parametrize('failure', ['corrupt', 'extract', 'write'])
def test_successful_answer_survives_optional_memory_faults(tmp_path,monkeypatch,failure):
    import pycode.agent.runtime as runtime
    s=MemoryStore(tmp_path); add(s)
    if failure=='corrupt': (s.memory_dir/'bad.md').write_bytes(b'\xff')
    if failure=='extract':
        def fail(*a,**k): raise OSError('PRIVATE_FAKE_ERROR')
        monkeypatch.setattr(runtime,'extract_memories',fail)
    if failure=='write':
        def fail(*a,**k): raise OSError('PRIVATE_FAKE_ERROR')
        monkeypatch.setattr(MemoryStore,'add_memory',fail)
    class Model:
        def generate(self,prompt):
            if prompt.startswith('Select project memories'): return '["note"]'
            if prompt.startswith('Extract durable PyCode memories'): return '[{"id":"new","type":"analysis","body":"new"}]'
            return 'Successful answer'
    result=run_agent_runtime(AgentTask('entry',tmp_path),RuntimeConfig(use_llm_planner=False),llm_client=Model(),
        tools={'retrieve_context':ToolSpec('retrieve_context',lambda *a,**k:success('retrieve_context','Current source'))})
    assert result.ok and result.answer=='Successful answer'
    if failure!='corrupt': assert result.memory.extraction_error
    assert 'PRIVATE_FAKE_ERROR' not in json.dumps(result.memory.to_dict())


@pytest.mark.parametrize('reason', ['execution_lost','budget_time','budget_models'])
def test_control_signal_before_publish_is_never_swallowed(tmp_path,monkeypatch,reason):
    s=MemoryStore(tmp_path);add(s);before=(s.memory_dir/'note.md').read_bytes()
    budget=ExecutionBudget()
    original=records.os.fsync
    def stop(fd):
        original(fd)
        budget.stop(reason)
    monkeypatch.setattr(records.os,'fsync',stop)
    with budget.activate(),pytest.raises(ExecutionStopped) as error:
        add(MemoryStore(tmp_path),memory_id='second')
    assert error.value.reason==reason and not (s.memory_dir/'second.md').exists()
    assert (s.memory_dir/'note.md').read_bytes()==before


def test_hardlink_and_renamed_identity_are_rejected_without_reading_content(tmp_path):
    s=MemoryStore(tmp_path);add(s)
    os.link(s.memory_dir/'note.md',s.memory_dir/'alias.md')
    assert s.list_memories()==[] and 'record_unreadable' in s.diagnostics
    (s.memory_dir/'alias.md').unlink()
    (s.memory_dir/'note.md').rename(s.memory_dir/'renamed.md')
    assert s.list_memories()==[]


def test_process_lock_contention_is_explicit_and_os_releases_on_process_exit(tmp_path):
    code = ('import sys; from pathlib import Path; from pycode.agent.memory_record import write_lock; '
            'root=Path(sys.argv[1]); '
            '\nwith write_lock(root/".pclens/memory",root):\n print("locked",flush=True); sys.stdin.readline()\n')
    child=subprocess.Popen([sys.executable,'-u','-c',code,str(tmp_path)],stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        assert child.stdout.readline().strip()=='locked'
        with pytest.raises(OSError): add(MemoryStore(tmp_path))
    finally:
        child.communicate('\n',timeout=15)
    assert child.returncode==0
    assert add(MemoryStore(tmp_path)).id=='note'


@pytest.mark.parametrize('reason',['execution_lost','budget_time','budget_models'])
def test_runtime_memory_tolerance_does_not_turn_control_stop_into_success(tmp_path,monkeypatch,reason):
    import pycode.agent.runtime as runtime
    def stop(*a,**k):
        from pycode.agent.budget import current_budget
        current_budget().stop(reason)
    monkeypatch.setattr(runtime,'extract_memories',stop)
    class Model:
        def generate(self,prompt): return 'Answer already generated'
    result=run_agent_runtime(AgentTask('entry',tmp_path),RuntimeConfig(use_llm_planner=False),llm_client=Model(),
        tools={'retrieve_context':ToolSpec('retrieve_context',lambda *a,**k:success('retrieve_context','Current source'))})
    assert not result.ok and result.stop_reason==reason
    assert result.execution_budget['stop_reason']==reason
    assert not (tmp_path/'.pclens/memory').exists()


def test_store_checks_every_ancestor_for_windows_reparse_points(tmp_path,monkeypatch):
    folder=tmp_path/'.pclens';folder.mkdir()
    original=Path.lstat
    def lstat(path,*a,**kw):
        info=original(path,*a,**kw)
        if path==folder:
            return SimpleNamespace(st_mode=info.st_mode,st_nlink=info.st_nlink,st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path,'lstat',lstat)
    with pytest.raises(PermissionError,match='Linked'): MemoryStore(tmp_path)


def test_oversized_bad_file_cannot_consume_catalog_budget_and_hide_valid_records(tmp_path):
    s=MemoryStore(tmp_path);add(s)
    bad=s.memory_dir/'aaa-broken.md'
    bad.write_bytes(b'x'*records.MAX_TOTAL_BYTES)
    assert s.load_memory('note').body=='Original body'
    assert [e.id for e in s.list_memories()]==['note']
    assert [i.id for i in s.search_memories('entry')]==['note']
    selected,_=load_relevant_memories(tmp_path,task_description='entry',messages=[])
    assert [i.id for i in selected]==['note']
    assert 'note.md' in s.read_index_text()
    s.rebuild_index()
    assert 'note.md' in s.index_path.read_text(encoding='utf-8')
    assert bad.stat().st_size==records.MAX_TOTAL_BYTES
    assert 'record_unreadable' in s.diagnostics


@pytest.mark.parametrize('authority',['active_scope','tool_context'])
@pytest.mark.parametrize('filename',['note.md','MEMORY.md'])
def test_bound_raw_file_reads_cannot_bypass_memory_applicability(tmp_path,authority,filename):
    from pycode.tools.read_file import read_file
    s=MemoryStore(tmp_path);add(s,title='PRIVATE_LEGACY_NOTE',body='PRIVATE_LEGACY_NOTE')
    trusted=scope(tmp_path)
    context=ToolContext(tmp_path,retrieval_context=SimpleNamespace(root=tmp_path,binding=trusted.binding)
                        if authority=='tool_context' else None)
    with trusted.activate() if authority=='active_scope' else nullcontext():
        result=read_file(context,'.pclens/memory/'+filename)
        assert not result.ok and result.execution.category=='permission_denied'
        assert 'PRIVATE_LEGACY_NOTE' not in str(result.data)
        assert not memory(context,operation='load',id='note').ok
    # Explicit non-bound local inspection retains the existing Tool behavior.
    local=read_file(ToolContext(tmp_path),'.pclens/memory/'+filename)
    assert local.ok and 'PRIVATE_LEGACY_NOTE' in local.data['content']


def test_deeply_nested_model_proposal_isolated_and_following_valid_proposal_saved(tmp_path):
    class Model:
        def generate(self,prompt):
            return ('[{"id":"bad","type":"analysis","body":"Bad input","tags":'
                +'['*2000+'0'+']'*2000
                +'}, {"id":"valid","type":"analysis","body":"Valid hint"}]')
    created,error=extract_memories(tmp_path,task_description='entry',messages=[],tool_results=[],
                                 answer='Successful analysis',llm_client=Model())
    assert [item.id for item in created]==['valid']
    assert error=='memory_proposal_not_saved'
    assert [e.id for e in MemoryStore(tmp_path).list_memories()]==['valid']

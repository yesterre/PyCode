"""Memory record contracts and bounded file primitives, not lifecycle policies.

Only trusted Python callers can install a scope. Tool schemas expose none of it.
The workspace remains service/operator owned; hashes detect corruption, not a
privileged local writer who can replace both the record and its hash.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from threading import RLock
from uuid import UUID

from pycode.chunks import SnapshotBinding


SCHEMA_VERSION = 2
MAX_RECORD_BYTES = 65536
MAX_RECORDS = 256
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DIAGNOSTICS = 16
ORIGINS = {'user_explicit': 'declared', 'code_derived': 'evidence_backed',
           'model_inference': 'unverified', 'local_unknown': 'unknown'}
_ACTIVE = ContextVar('pycode_memory_scope', default=None)
_WRITE_LOCK = RLock()


@dataclass(frozen=True)
class MemoryScope:
    root: Path
    binding: SnapshotBinding
    run_id: str | None = None

    def __post_init__(self):
        object.__setattr__(self, 'root', Path(self.root).resolve())
        if not isinstance(self.binding, SnapshotBinding):
            raise ValueError('Invalid Memory binding')
        if self.run_id is not None and str(UUID(self.run_id)) != self.run_id:
            raise ValueError('Invalid Memory Run')

    @contextmanager
    def activate(self):
        active = _ACTIVE.get()
        if active is not None and active != self:
            raise ValueError('Cannot replace trusted Memory scope')
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)


def execution_check():
    # Imported lazily to keep the existing Tool/Runtime import order unchanged.
    from pycode.agent.budget import current_budget
    if (budget := current_budget()) is not None:
        budget.check()  # ExecutionStopped is BaseException: never a storage error.


def effective_scope(root, supplied=None):
    from pycode.agent.budget import current_budget
    root = Path(root).resolve()
    active, budget = _ACTIVE.get(), current_budget()
    if active is not None and supplied is not None and supplied != active:
        raise PermissionError('Memory scope mismatch')
    scope = active or supplied
    if budget is not None and budget.binding is not None:
        context = budget.retrieval_context
        if context is not None and context.root.resolve() != root:
            raise PermissionError('Memory workspace mismatch')
        scope = scope or MemoryScope(root, budget.binding)
        budget.validate_binding(scope.binding)
    if scope is not None and scope.root != root:
        raise PermissionError('Memory workspace mismatch')
    return scope


def provenance_for(scope, origin, evidence=()):
    if origin not in ORIGINS:
        raise ValueError('Invalid Memory origin')
    binding = asdict(scope.binding) if scope else dict.fromkeys(('project_id', 'snapshot_id', 'commit_sha'))
    value = dict(binding, run_id=scope.run_id if scope else None, origin=origin,
                 status=ORIGINS[origin], evidence=list(evidence))
    validate_provenance(value)
    return value


def validate_provenance(value):
    fields = {'project_id', 'snapshot_id', 'commit_sha', 'run_id', 'origin', 'status', 'evidence'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError('Invalid Memory provenance')
    origin = value['origin']
    if not isinstance(origin, str) or ORIGINS.get(origin) != value['status']:
        raise ValueError('Invalid Memory origin/status')
    binding = [value[k] for k in ('project_id', 'snapshot_id', 'commit_sha')]
    if any(v is not None for v in binding):
        SnapshotBinding(*binding)
    if value['run_id'] is not None:
        if not all(binding) or str(UUID(value['run_id'])) != value['run_id']:
            raise ValueError('Invalid Memory Run')
    evidence = value['evidence']
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise ValueError('Invalid Memory evidence')
    for ref in evidence:
        from pycode.retrieval_evidence import safe_relative
        if (not isinstance(ref, dict) or set(ref) != {'path', 'start_line', 'end_line', 'chunk_ids'}
                or not isinstance(ref['path'], str) or len(ref['path']) > 1024
                or safe_relative(ref['path']) != ref['path']
                or type(ref['start_line']) is not int or type(ref['end_line']) is not int
                or not 1 <= ref['start_line'] <= ref['end_line']
                or not isinstance(ref['chunk_ids'], list) or not 1 <= len(ref['chunk_ids']) <= 16
                or any(not isinstance(c, str) or len(c) > 128 for c in ref['chunk_ids'])):
            raise ValueError('Invalid Memory evidence reference')
    if origin == 'code_derived' and (not all(binding) or not evidence):
        raise ValueError('Code Memory requires bound evidence')
    if origin != 'code_derived' and evidence:
        raise ValueError('Unverified Memory cannot assert code evidence')


def validate_code_evidence(scope, evidence, context):
    if scope is None or context is None or context.binding != scope.binding or context.chunks is None:
        raise ValueError('Code Memory requires trusted Chunk context')
    chunks = {c.id: c for c in context.chunks.chunks}
    for ref in evidence:
        selected = [chunks.get(cid) for cid in ref['chunk_ids']]
        if any(c is None or c.path != ref['path'] for c in selected):
            raise ValueError('Memory evidence does not match Snapshot')
        intervals = sorted((c.span.start_line, c.span.end_line) for c in selected)
        cursor = ref['start_line']
        for start, end in intervals:
            if start > cursor:
                break
            cursor = max(cursor, end + 1)
        if cursor <= ref['end_line']:
            raise ValueError('Memory evidence range is not covered')


def record_identity(memory_id, provenance):
    return digest({'id': memory_id, 'provenance': provenance})


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def check_path(path, root):
    """Reject links, reparse points and hardlinks in every existing component."""
    path, root = Path(path).absolute(), Path(root).resolve()
    if not path.is_relative_to(root):
        raise PermissionError('Memory path outside project')
    for current in [root, *reversed(list(path.parents)[:len(path.relative_to(root).parts)-1]), path]:
        # Use the lexical chain; resolve() alone would hide an in-tree link.
        if current == root or current.is_relative_to(root):
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                    or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise PermissionError('Linked Memory path rejected')
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise PermissionError('Special Memory path rejected')


def read_bounded(path, root):
    execution_check()
    check_path(path, root)
    with path.open('rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError('Invalid Memory file')
        data = stream.read(MAX_RECORD_BYTES + 1)
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError('Memory record capacity exceeded')
    return data.decode('utf-8-sig')


def atomic_write(path, text, root):
    execution_check()
    check_path(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    check_path(path, root)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.memory-', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text.encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        execution_check()
        check_path(path, root)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Our leftover temp is ignored; never mask execution control.


@contextmanager
def write_lock(directory, root):
    """Serialize id allocation + capacity + publication; OS releases on exit/crash."""
    with _WRITE_LOCK:
        execution_check()
        lock = directory / '.memory.lock'
        check_path(lock, root)
        directory.mkdir(parents=True, exist_ok=True)
        with lock.open('a+b') as stream:
            if stream.tell() == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                execution_check()
                yield
            finally:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

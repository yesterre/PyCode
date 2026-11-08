"""A06 total acceptance: identical bounded Memory diagnostics through live/replay/SSE."""
from datetime import datetime,timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.api.realtime_trace import _frame
from backend.app.core.application_trace import application_event_from_record
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.infrastructure.realtime_trace import event_json
from pycode.agent.memory import MemoryRunInfo


@pytest.mark.parametrize('status',['completed','failed'])
@pytest.mark.parametrize('invalid',[False,True])
def test_memory_diagnostics_survive_live_persistence_and_sse_without_private_fields(status,invalid):
    now=datetime.now(timezone.utc)
    run=SimpleNamespace(id=uuid4(),snapshot_id=uuid4(),run_type='agent',started_at=now,
                        finished_at=now,status=status,result='Answer' if status=='completed' else None)
    published=[]
    session=RealtimeTraceSession(run,SimpleNamespace(ensure_owned=lambda:None),
        SimpleNamespace(publish=lambda run_id,event:published.append(event)))
    summary=MemoryRunInfo(reason_counts={'expired':1,'inactive':2}).public_summary()
    session.observe('memory',dict(summary,body='PRIVATE_A06_MEMORY') if invalid else summary)
    result=SimpleNamespace(trace=SimpleNamespace(events=[],tools=[]),stop_reason='final')
    terminal=session.append_terminal(run,result=result)
    session.publish(terminal)
    record=SimpleNamespace(sequence=terminal.sequence,event_type=terminal.event_type.value,
        tool_name=None,payload=terminal.database_payload())
    replay=application_event_from_record(record)
    assert replay is not None
    assert event_json(published[-1])==event_json(replay)==event_json(terminal)
    frame=_frame(replay)
    assert frame.startswith(f'id: {terminal.sequence}\nevent: trace\ndata: ')
    delivered=json.loads(frame.split('data: ',1)[1])['payload']
    assert 'PRIVATE_A06_MEMORY' not in frame
    if invalid:
        assert 'memory' not in delivered
    else:
        assert delivered['memory']==summary
        assert delivered['memory']['reason_counts']=={'expired':1,'inactive':2}

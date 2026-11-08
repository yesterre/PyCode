"""Memory privacy is identical at generation, persistence and legacy replay."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from backend.app.core.application_trace import (
    ApplicationEventType, ApplicationTraceEvent, ApplicationTraceNormalizer,
    application_event_from_record,
)


NOW=datetime.now(timezone.utc)
PRIVATE='SYNTHETIC_PRIVATE_MEMORY_CONTENT'


@pytest.mark.parametrize('kind',['plan_created','tool_started','tool_finished'])
def test_all_memory_public_boundaries_drop_content_without_mutating_history(kind):
    payload={
        'plan_created':dict(source='llm',fallback_used=False,steps=[dict(step_index=0,
            tool='memory',reason=PRIVATE,required=False)]),
        'tool_started':dict(turn_index=1,reason=PRIVATE,arguments=dict(operation='add',memory_type='analysis',
            limit=5,id=PRIVATE,title=PRIVATE,summary=PRIVATE,body=PRIVATE,query=PRIVATE,tags=[PRIVATE],
            project_id=PRIVATE,origin='user_explicit')),
        'tool_finished':dict(outcome='failed',summary=PRIVATE,result_summary=PRIVATE,
            error=dict(category='tool_error',code=PRIVATE,message=PRIVATE)),
    }[kind]
    before=deepcopy(payload);tool=None if kind=='plan_created' else 'memory'
    event=ApplicationTraceEvent(0,ApplicationEventType(kind),NOW,tool,payload)
    assert PRIVATE not in json.dumps(event.database_payload())
    assert payload==before
    record=SimpleNamespace(sequence=0,event_type=kind,tool_name=tool,
        payload=dict(schema_version=1,occurred_at=NOW.isoformat(),event_payload=payload))
    historical=deepcopy(record.payload)
    replay=application_event_from_record(record)
    assert replay is not None and replay.payload==event.payload
    assert record.payload==historical
    if kind=='tool_started':
        assert event.payload['arguments']==dict(operation='add',memory_type='analysis',limit=5)
    assert event.schema_version==1


def test_non_memory_tools_keep_their_existing_projection():
    payload=dict(arguments=dict(path='logic.py',start_line=2,end_line=5),reason='Read source')
    event=ApplicationTraceEvent(0,ApplicationEventType.TOOL_STARTED,NOW,'read_file',payload)
    assert event.payload==payload


def test_offline_memory_normalization_uses_the_same_boundary_as_live_events():
    tool=SimpleNamespace(turn_index=1,tool='memory',arguments=dict(operation='add',body=PRIVATE),started_at=NOW)
    kind,when,name,payload=ApplicationTraceNormalizer._tool_started(tool,PRIVATE)
    event=ApplicationTraceEvent(0,kind,when,name,payload)
    assert PRIVATE not in json.dumps(event.database_payload())

"""Frozen receiver behavior contracts, safety negatives and known recall gaps."""
import json
from pathlib import Path

import pytest

from pycode.graph_schema import graph_from_dict, graph_to_dict
from test_a03_receivers import build


FIXTURES = Path(__file__).parent/'fixtures/f_t05'
CASES = json.loads((FIXTURES/'cases.json').read_text(encoding='utf-8'))['cases']


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_frozen_receiver_behavior_contract(tmp_path, case):
    _, graph, _ = build(tmp_path, case['files'])
    scopes = {s['id']:s['key'] for s in graph.scopes}
    for label in case['labels']:
        matches = [s for s in graph.call_sites if (
            s['path'], scopes[s['scope_id']], s['start_line'], s['col_offset'], s['expression']) == (
            label['path'], label['scope'], label['line'], label['column'], label['expression'])]
        assert len(matches) == 1
        site = matches[0]
        if label['expected'] == 'static':
            assert site['resolution'] == 'static'
            assert site['target_id'] == label['target_id']
            assert any(e.type == 'calls' and e.site_id == site['id'] and e.target == label['target_id']
                       for e in graph.edges)
        else:
            assert site['resolution'] in {'unknown','ambiguous'}
            assert site['target_id'] is None
            assert not any(e.type == 'calls' and e.site_id == site['id'] for e in graph.edges)
    assert graph_from_dict(graph_to_dict(graph)) == graph


@pytest.mark.parametrize('argument', [
    'self, self is None',
    'flag=self is None, actual=self',
    '[self, self is None]',
    'self is mutate(self)',
    'self is None == other',
    'self is other.value',
    'self is (alias := self)',
    '*(self,)',
    '**{"actual": self}',
])
def test_identity_projection_never_erases_an_actual_escape(tmp_path, argument):
    source = ('class C:\n    def work(self):\n        pass\n'
              '    def run(self, other=None):\n        sink(' + argument + ')\n        self.work()\n')
    _, graph, _ = build(tmp_path, {'a.py':source})
    site = next(s for s in graph.call_sites if s['expression'] == 'self.work')
    assert site['resolution'] == 'unknown' and site['target_id'] is None
    assert not any(e.type == 'calls' and e.site_id == site['id'] for e in graph.edges)


def test_original_v4_artifact_keeps_original_conservative_proof():
    data = json.loads((FIXTURES/'legacy_v4.json').read_text(encoding='utf-8'))
    assert data['producer'] == 'pycode-a03c-v1'
    old = graph_from_dict(data)
    site = next(s for s in old.call_sites if s['expression'] == 'self.work')
    assert (site['resolution'], site['basis'], site['target_id']) == ('unknown','receiver_escaped',None)
    assert graph_to_dict(old) == data


REVIEWED = json.loads((FIXTURES/'cases.reviewed.json').read_text(encoding='utf-8'))['cases']
ADDED_NEGATIVES = [case for case in REVIEWED if case['id'] not in {c['id'] for c in CASES}]
RECALL_GAPS = [case for case in REVIEWED if case['id'] in {
    'conditional','multiple-receivers','getattr','closure-escape','closure-receiver','inherit','override','super',
}]


@pytest.mark.parametrize('case', ADDED_NEGATIVES, ids=lambda case: case['id'])
def test_reviewed_genuine_uncertainty_never_gets_a_calls_edge(tmp_path, case):
    _, graph, _ = build(tmp_path, case['files'])
    label = case['labels'][0]
    site = next(s for s in graph.call_sites if s['expression'] == label['expression'])
    assert label['expected'] == 'uncertain'
    assert site['resolution'] in {'unknown','ambiguous'} and site['target_id'] is None
    assert not any(e.type == 'calls' and e.site_id == site['id'] for e in graph.edges)


@pytest.mark.parametrize('case', RECALL_GAPS, ids=lambda case: case['id'])
def test_source_determinate_but_unsupported_forms_are_explicit_recall_gaps(tmp_path, case):
    # These are not correct abstentions: the reviewed oracle names a target.
    # Keep the current conservative behavior visible without fabricating support.
    _, graph, _ = build(tmp_path, case['files'])
    label = case['labels'][0]
    site = next(s for s in graph.call_sites if s['expression'] == label['expression'])
    assert label['expected'] == 'static' and label['target_id']
    assert site['resolution'] == 'unknown' and site['target_id'] is None

"""Portable replays of actual C6 isolated histories, not new onsite executions."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tools import quality_spc_history as history, quality_spc_annotations as annotations


FIXTURES = Path(__file__).parent/'fixtures'


@pytest.fixture
def source(monkeypatch):
    captured = json.loads((FIXTURES/'spc_nonempty_history.json').read_text(encoding='utf8'))
    state = dict(captured, omitted=False, mutation=None, queries=[])
    async def sql(query, maximum):
        state['queries'].append(query)
        if 'INFORMATION_SCHEMA.COLUMNS' in query:
            return [dict(TABLE_NAME=table, COLUMN_NAME=field) for table, fields in history.SCHEMA.items() for field in fields.split(',')]
        if 'FROM AddSPCAnnotationHistory' in query:
            return []
        if 'WHERE d.DataPointHistoryDetailId IN (' in query:
            rows = copy.deepcopy(state['points'])
            if state['mutation']:
                rows[-1].update(state['mutation'])
            return rows
        assert 'ORDER BY m.TxnDateGMT,d.DataPointHistoryDetailId' in query
        keys = [row['DataPointHistoryDetailId'] for row in state['points']]
        if state['omitted']: keys.append('001c6b800000ffff')
        return [dict(DataPointHistoryDetailId=key) for key in keys]
    class Context:
        def __init__(self, *args): pass
        async def resolve(self, entity, ref, revisioned):
            assert entity == 'UOMs' and ref['InstanceID'] == state['unit']['InstanceID']
            return copy.deepcopy(state['unit'])
        async def get(self, entity, key):
            assert entity == 'UOMs' and key == state['unit']['InstanceID']
            return copy.deepcopy(state['unit'])
    monkeypatch.setattr(history.q, 'sql_rows', AsyncMock(side_effect=sql))
    monkeypatch.setattr(history, 'ReadContext', Context)
    return state


def analyze(source):
    return json.loads(asyncio.run(history.analyze_spc_history('i_mr', source['groups'], ['beyond_3sigma'],
                                                            baseline_groups=source['baseline_groups'])))


def test_actual_nonempty_mes_rows_replay_original_values_scope_and_signal(source):
    out = analyze(source)
    assert out['complete'] and out['data_verified_against_mes'] and out['source_window_verified']
    assert out['points'][0]['values'] == ['14'] and out['points'][0]['observation_id'] == source['expected_signal_id']
    assert out['primary_signals'][0]['contributing_ids'] == [source['expected_signal_id']]
    assert out['control_limits']['primary']['center'] == '10'
    assert len(out['source_history']['points']) == 4 and out['baseline']['count'] == 3
    assert not out['native_spc_result'] and not out['ready_for_mes_write'] and not out['release_authorized']
    assert not out['baseline']['stability_verified']


@pytest.mark.parametrize('field,value', [('ParentTxnId', '0000000000000001'),
    ('ProductId', '0000000000000001'), ('UOMId', '0000000000000001'),
    ('ReversalStatus', 2), ('DataValue', 'NaN')])
def test_actual_history_replay_rejects_changed_ownership_scope_or_numeric_value(source, field, value):
    source['mutation'] = {field: value}
    out = analyze(source)
    assert not out['complete'] and 'error' in out


def test_actual_history_replay_rejects_a_hidden_point_in_the_interval(source):
    source['omitted'] = True
    out = analyze(source)
    assert not out['complete'] and '区间' in out['error']


def test_actual_service_projection_does_not_turn_missing_signature_fields_into_no_requirement(monkeypatch):
    capture = json.loads((FIXTURES/'spc_annotation_preview.json').read_text(encoding='utf8'))
    data = capture['response']['data']
    assert all(field not in data for field in capture['expected_missing_fields'])
    body = dict(dataPoint={'instanceId': data['DataPoint']['InstanceId'],
                           'parent': {'instanceId': data['DataPoint']['Parent']['InstanceId']}},
                spcChartDef={'name': data['SPCChartDef']['Name'], 'instanceId': data['SPCChartDef']['InstanceId']},
                annotation=data['Annotation'], exclude=False, comments=data['Comments'])
    definition = data['DataCollectionDef']
    before = {'configuration': {'definition': {'InstanceID': definition['InstanceId'],
                                              'Name': definition['Name'], 'Revision': definition['Revision']}}}
    monkeypatch.setattr(annotations, 'request', AsyncMock(return_value=json.dumps(capture['response'])))
    with pytest.raises(ValueError, match='缺少签名要求字段'):
        asyncio.run(annotations.preview(body, before))

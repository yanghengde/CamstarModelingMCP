import asyncio
import json
from unittest.mock import AsyncMock
import pytest
from tools import collection_validation as cv, get_tool_func
from core.tool_catalog import TOOL_POLICIES


def validate(monkeypatch, values, points=None, complete=True):
    points = points if points is not None else [{'Name': 'X', 'DataType': 3, 'IsRequired': True, 'LowerLimit': '0', 'UpperLimit': '10'}]
    mock = AsyncMock(return_value={'definition': {'InstanceID': 'd', 'Revision': '1'}, 'raw_items': points, 'complete': complete})
    monkeypatch.setattr(cv, 'definition', mock)
    result = json.loads(asyncio.run(cv.validate_collection_values(values, instance_id='d')))
    assert result['executable'] is False and result['quality_verdict'] is None
    return result


def test_valid_and_limits(monkeypatch):
    r = validate(monkeypatch, {'X': 5})
    assert r['static_valid'] is True
    assert r['checks'][0]['limits']['lower']['status'] == 'within'
    r = validate(monkeypatch, {'X': 11})
    assert r['static_valid'] is True
    assert r['checks'][0]['limits']['upper']['status'] == 'outside'


@pytest.mark.parametrize('value', [True, '2', [], {}, float('inf'), float('nan')])
def test_bad_numeric(monkeypatch, value):
    r = validate(monkeypatch, {'X': value})
    assert r['errors']


def test_missing_unknown_and_duplicates(monkeypatch):
    r = validate(monkeypatch, {'Y': 1})
    assert {e['code'] for e in r['errors']} == {'required', 'unknown_point'}
    p = {'Name': 'X', 'DataType': 3, 'IsRequired': True}
    assert validate(monkeypatch, {'X': 1}, [p, p])['static_valid'] is None
    assert validate(monkeypatch, {'X': 1}, complete=False)['static_valid'] is None


@pytest.mark.parametrize('kind,value,valid', [(1, 1.5, False), (1, 0, True), (4, 'text', True), (7, False, True), (7, 1, False), (5, {}, None), (6, '2026-01-01', None)])
def test_types(monkeypatch, kind, value, valid):
    r = validate(monkeypatch, {'X': value}, [{'Name': 'X', 'DataType': kind, 'IsRequired': True}])
    assert r['static_valid'] is valid


def test_expression_unknown(monkeypatch):
    r = validate(monkeypatch, {'X': 1}, [{'Name': 'X', 'DataType': 3, 'IsRequired': False, 'LowerLimit': 'Parent.Limit'}])
    assert r['checks'][0]['limits']['lower']['status'] == 'unknown'


def test_registered():
    assert get_tool_func('validate_collection_values')
    assert TOOL_POLICIES['validate_collection_values'].group == 'collection'

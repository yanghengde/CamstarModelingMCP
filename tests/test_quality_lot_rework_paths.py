import asyncio
import copy
import json

import pytest
from fastmcp import Client
from tools import mcp, quality_lot_rework as rework
from tests.test_quality_lot_rework import env as rework_env, lot_env, prepare, execute, snapshot, PATH, key, LOT, CID

ALTERNATE = key(990)


@pytest.fixture
def env(rework_env):
    state = rework_env
    alternate = copy.deepcopy(state['source']['ReworkPaths'][0])
    alternate.update(Name='Alternate', InstanceID=ALTERNATE)
    state['source']['ReworkPaths'].append(alternate)
    row = copy.deepcopy(state['path_rows'][0])
    row.update(PathId=ALTERNATE, PathName='Alternate')
    state['path_rows'].append(row)
    return state


def test_expected_native_default_binds_all_members_and_submits_once(env):
    ready = prepare(expected_path_name='Default')
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    routes = saved['preview']['member_routes']
    assert len(routes) == 2
    assert all(route['path']['instanceId'] == PATH for route in routes)
    assert all([path['instanceId'] for path in route['configured_path_order']] == [PATH, ALTERNATE] for route in routes)
    assert set(ready['request_preview']['body']) == {'samplingLot', 'samplingLotResult', 'reworkReason', 'comments'}
    assert execute(ready)['state'] == 'unknown'
    env['client'].request.assert_awaited_once()
    assert env['client'].request.await_args.args[1].endswith('/ExecuteSamplingLot')
    assert 'error' in execute(ready)
    env['client'].request.assert_awaited_once()


@pytest.mark.parametrize('name', [None, '', 'Alternate', 'Unknown'])
def test_multiple_paths_require_explicit_correct_default(env, name):
    assert 'error' in prepare(expected_path_name=name)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('field', ['Path', 'ToStep', 'ToWorkflow', 'ReEntryStep', 'ReEntryWorkflow'])
def test_multiple_paths_require_complete_native_route_echo(env, field):
    env['child_hook'] = lambda data: data.pop(field, None)
    assert 'error' in prepare(expected_path_name='Default')
    env['client'].request.assert_not_awaited()


def test_dynamic_selector_remains_unsupported_even_with_expected_name(env):
    env['source']['ReworkPathSelectors'] = [{'Name': 'Dynamic'}]
    assert 'error' in prepare(expected_path_name='Default')


def test_reordered_path_list_invalidates_ready_plan(env):
    ready = prepare(expected_path_name='Default')
    assert ready.get('state') == 'ready', ready
    env['source']['ReworkPaths'].reverse()
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_other_configured_path_change_invalidates_ready_plan(env):
    ready = prepare(expected_path_name='Default')
    assert ready.get('state') == 'ready', ready
    env['source']['ReworkPaths'][1]['Description'] = 'Changed route configuration'
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_membership_change_invalidates_ready_plan(env):
    ready = prepare(expected_path_name='Default')
    assert ready.get('state') == 'ready', ready
    env['members'].pop()
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_expected_path_snapshot_tampering_rejected(env):
    ready = prepare(expected_path_name='Default')
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    saved['preview']['member_routes'][0]['path']['name'] = 'Alternate'
    with pytest.raises(ValueError):
        rework.validate_transport(saved)


def test_single_path_legacy_call_and_explicit_expectation_both_work(rework_env):
    assert prepare().get('state') == 'ready'
    assert prepare(expected_path_name='Default').get('state') == 'ready'
    assert 'error' in prepare(expected_path_name='Alternate')


def test_mcp_multi_path_entry(env):
    async def flow():
        async with Client(mcp) as client:
            reply = await client.call_tool('prepare_sampling_lot_rework', dict(
                lot_name='L', rework_reason_name='RepairReason', expected_path_name='Default'))
            assert json.loads(reply.content[0].text)['state'] == 'ready'
    asyncio.run(flow())
    env['client'].request.assert_not_awaited()

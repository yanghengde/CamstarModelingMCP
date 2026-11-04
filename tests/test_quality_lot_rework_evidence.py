import pytest
from tools import quality_lot_rework_evidence as evidence, quality_plans as plans, quality_inspection as inspection
from tests.test_quality_lot_rework import env as rework_env, lot_env, prepare, execute, snapshot, result
from tests.test_quality_lot_rework import WF, DST, TARGET, CID, CID2, key, reference
from tests.test_quality_lot_execution import PLAN, TID

TASK = key(12000)


@pytest.fixture
def env(rework_env):
    task = dict.fromkeys(inspection.TASK_COLUMNS.split(','))
    task.update(ContainerSampleDataId=TASK, ContainerId=CID, SamplingPlanId=PLAN, SampleTestId=TID,
        TotalSamplesRequired=3, CompletedSamples=2, Rejects=1, AllowedRejects=1,
        InspectAllContQty=False, InspectAllQtyOnFailure=False, ChangeCount=1)
    rework_env['member_tasks'] = [task]
    return rework_env


def inspect(ready):
    return result(plans.inspect_quality_transaction(ready['plan_id'], ready['plan_digest']))


def test_plan_binds_task_cleanup_and_result_keeps_causality_pending(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    saved = snapshot(ready)
    assert saved['member_sampling_tasks'][0]['ContainerSampleDataId'] == TASK
    assert ready['server_context']['original_member_task_count'] == 1
    assert execute(ready)['state'] == 'unknown'
    env['member_tasks'] = []
    env['status_override'] = dict(InRework=True, Workflow=reference('Flow', WF, 'A'),
        Step=reference('Repair', DST), Spec=reference('RepairSpec', TARGET, 'A'))
    proof = inspect(ready)
    assert proof['sampling_task_cleanup']['missing_original_task_ids'] == [TASK]
    assert proof['sampling_task_cleanup']['all_original_tasks_absent']
    assert all(row['expected_route_currently_observed'] for row in proof['member_route_evidence'])
    assert not proof['verified_success'] and not proof['history_correlation_verified']
    assert not proof['sampling_task_cleanup']['cleanup_causal_chain_verified']


@pytest.mark.parametrize('field,value', [('CompletedSamples', 3), ('ChangeCount', 2)])
def test_member_sampling_task_change_invalidates_prepared_plan(env, field, value):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    env['member_tasks'][0][field] = value
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


def test_new_member_task_invalidates_prepared_plan(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    env['member_tasks'].append(dict(env['member_tasks'][0], ContainerSampleDataId=key(12001), ContainerId=CID2))
    assert 'error' in execute(ready)
    env['client'].request.assert_not_awaited()


@pytest.mark.parametrize('mode', ['duplicate', 'foreign', 'incomplete', 'too_many'])
def test_incomplete_original_cleanup_baseline_blocks_prepare(env, mode):
    if mode == 'duplicate':env['member_tasks'] *= 2
    elif mode == 'foreign':env['member_tasks'][0]['ContainerId'] = key(12002)
    elif mode == 'incomplete':env['member_tasks'][0].pop('SampleTestId')
    elif mode == 'too_many':env['member_tasks'] *= 201
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


def test_retained_changed_and_replacement_tasks_reported_separately(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    env['member_tasks'][0]['ChangeCount'] = 2
    env['member_tasks'].append(dict(env['member_tasks'][0], ContainerSampleDataId=key(12001)))
    cleanup = inspect(ready)['sampling_task_cleanup']
    assert cleanup['retained_original_task_ids'] == [TASK]
    assert cleanup['changed_original_task_ids'] == [TASK]
    assert cleanup['new_tasks'][0]['ContainerSampleDataId'] == key(12001)
    assert not cleanup['all_original_tasks_absent']


def test_unchanged_route_is_explicitly_unmatched(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    proof = inspect(ready)
    assert all(not row['expected_route_currently_observed'] for row in proof['member_route_evidence'])
    assert not proof['verified_success']


def test_legacy_snapshot_missing_cleanup_baseline_stays_unknown(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    old = snapshot(ready)
    old.pop('member_sampling_tasks')
    import asyncio
    from tools.quality_lot_rework import inspect_snapshot
    proof = asyncio.run(inspect_snapshot(old))
    assert not proof['sampling_task_cleanup']['baseline_available']
    assert proof['sampling_task_cleanup']['original_task_count'] is None
    assert not proof['verified_success']


def test_empty_original_task_set_does_not_claim_cleanup():
    assert not evidence.cleanup([], [])['all_original_tasks_absent']


def test_task_baseline_changing_during_prepare_is_rejected(env, monkeypatch):
    original = evidence.sampling_tasks
    calls = 0
    async def changing(members):
        nonlocal calls
        calls += 1
        rows = await original(members)
        if calls == 2:
            rows[0]['ChangeCount'] += 1
        return rows
    monkeypatch.setattr(evidence, 'sampling_tasks', changing)
    assert 'error' in prepare()
    env['client'].request.assert_not_awaited()


def test_route_query_unavailable_keeps_original_record_evidence(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    env['members'] = []
    proof = inspect(ready)
    assert len(proof['original_members_current_records']) == 2
    assert all(not row['available'] for row in proof['member_route_evidence'])
    assert not proof['verified_success']


def test_route_changes_during_inspection_reject_mixed_evidence(env):
    ready = prepare()
    assert ready.get('state') == 'ready', ready
    execute(ready)
    reads = 0
    def change_after_first_pass(status):
        nonlocal reads
        reads += 1
        if reads > 2:
            status.update(InRework=True, Step=reference('Repair', DST), Spec=reference('RepairSpec', TARGET, 'A'))
    env['identity_hook'] = change_after_first_pass
    assert 'error' in inspect(ready)

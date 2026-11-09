"""Replay real isolated inspection histories, plus evidence corruption cases."""
import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from tools import quality_inspection_history as ih,quality_history as h,quality_plans as p

CAPTURE=json.loads(Path('tests/fixtures/inspection_combinations_native.json').read_text(encoding='utf8'))['cases']


@pytest.mark.parametrize('case',CAPTURE)
def test_all_native_combinations_match_exact_original_history(case):
    r=copy.deepcopy(CAPTURE[case]);s=r['snapshot']
    p.transport(s)
    assert ih.supported(s)and h.history_mode(s)=='inspection_native'
    assert all(ih.compare(s,r['attempt'],r['root'],r['evidence']).values())


@pytest.mark.parametrize('case',CAPTURE)
def test_current_original_task_and_balance_are_both_required(monkeypatch,case):
    r=copy.deepcopy(CAPTURE[case]);current=r['current']
    monkeypatch.setattr(ih.loss,'record',AsyncMock(return_value=current['container']))
    monkeypatch.setattr(ih.q,'sql_rows',AsyncMock(return_value=[current['original_task']]))
    checks,evidence=asyncio.run(ih.current_evidence(r['snapshot']))
    assert all(checks.values())and evidence==current
    current['original_task']['ChangeCount']+=1
    assert not all(asyncio.run(ih.current_evidence(r['snapshot']))[0].values())


@pytest.mark.parametrize('field,value',[
    ('IntegrationTxnId','0'*16),('ToQty','9'),('TxnType','8452'),('ContainerChangeCount','9'),
    ('ProductId','0'*16),('ReversalStatus','2'),('ContainerId','0'*16)])
def test_unrelated_or_changed_internal_child_cannot_prove_commit(field,value):
    r=copy.deepcopy(CAPTURE['reject_groups']);r['evidence']['native']['mainlines'][1][field]=value
    assert not all(ih.compare(r['snapshot'],r['attempt'],r['root'],r['evidence']).values())


@pytest.mark.parametrize('field,value',[
    ('ReasonCodeId','0'*16),('Qty','99'),('AdjustThruput','false'),('CountsAgainstProduction','false'),
    ('RecordAllQty','true'),('CloseWhenZero','false'),('ThisContainerDisassociated','true'),('IsRollup','true')])
def test_every_internal_loss_line_and_native_option_is_required(field,value):
    r=copy.deepcopy(CAPTURE['reason_groups']);r['evidence']['native']['details'][0][field]=value
    assert not all(ih.compare(r['snapshot'],r['attempt'],r['root'],r['evidence']).values())


def test_counted_running_rejects_are_not_incremental_group_rejects():
    r=copy.deepcopy(CAPTURE['reject_groups'])
    assert [v['sample_history']['Rejects']for v in r['evidence']['samples']]==['1','3','0']
    r['evidence']['samples'][1]['sample_history']['Rejects']='2'
    assert not ih.compare(r['snapshot'],r['attempt'],r['root'],r['evidence'])['all_groups_and_reason_increments']


@pytest.mark.parametrize('change',['missing_group','missing_reason','extra_child','wrong_submission_total','wrong_cumulative_total','wrong_original_task','wrong_comment','outside_window'])
def test_partial_and_unrelated_inspection_evidence_stays_unknown(change):
    r=copy.deepcopy(CAPTURE['reject_groups']);e=r['evidence']
    if change=='missing_group':e['samples'].pop()
    elif change=='missing_reason':e['samples'][0]['points'].clear()
    elif change=='extra_child':e['native']['mainlines'].append(copy.deepcopy(e['native']['mainlines'][1]))
    elif change=='wrong_submission_total':e['summary']['SamplesTested']='3'
    elif change=='wrong_cumulative_total':e['summary']['TotalRejects']='99'
    elif change=='wrong_original_task':e['summary']['SampleTestId']='0'*16
    elif change=='wrong_comment':r['root']['Comments']='different request'
    else:r['attempt']['started']+=1000;r['attempt']['finished']+=1000
    assert not all(ih.compare(r['snapshot'],r['attempt'],r['root'],e).values())


@pytest.mark.parametrize('field',['sampleType','samplesTested'])
def test_new_inspection_requires_explicit_native_input(field):
    r=copy.deepcopy(CAPTURE['reject_groups'])['snapshot'];r['body'].pop(field)
    with pytest.raises(ValueError):p.transport(r)


@pytest.mark.parametrize('field,value',[('sampleType',2),('samplesTested',True),('samplesTested',99)])
def test_pinned_inspection_input_cannot_be_changed(field,value):
    r=copy.deepcopy(CAPTURE['reject_groups'])['snapshot'];r['body'][field]=value
    with pytest.raises(ValueError):p.transport(r)


def test_original_task_zero_case_is_closed_without_inventing_task_cleanup():
    r=CAPTURE['zero'];assert r['current']['container']['Status']==2
    assert r['current']['original_task']['CompletedSamples']==3
    assert r['current']['original_task']['TotalSamplesRequired']==10
    assert r['current']['task_missing']is False


def test_schema_capture_contains_explicit_type_and_count_inputs():
    d=json.loads(Path('tests/fixtures/inspection_input_schema.json').read_text(encoding='utf8'))
    s=d['components']['schemas']['CollectSamplingData']['properties']
    assert s['sampleType']['type']=='integer'and s['samplesTested']['type']=='integer'

import asyncio
import copy
import json
from unittest.mock import AsyncMock

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from tools import modeling_workspace as m, import_plans as plans
from web import workbench as web


def structure():
    return {"complete": True, "workflows": {"id": {"workflow": {"name": "WF", "revision": "A"},
        "steps": [{"name": "A", "step_type": 1, "is_first_step": True, "spec": {"Name": "SP", "Revision": "1"},
                   "paths": [], "advanced": {"SchedulingDetail": {}}}], "gates": [],
        "standard_route_complete": True, "standard_route": ["A"]}}}


def test_integrity_unknown_and_reachability_are_not_false_certainty():
    data = structure()
    steps = data["workflows"]["id"]["steps"]
    steps.append({"name": "B", "step_type": 1, "paths": None})
    issues = m.inspect_structure(data)["issues"]
    assert any(i["level"] == "unknown" and "Spec" in i["message"] for i in issues)
    assert any(i["location"] == "id/B" and "不可达" in i["message"] for i in issues)


def test_differences_preserve_missing_vs_null():
    assert m.differences({}, {"Spec": None}) == [{"path": "/Spec", "change": "added", "before": None, "after": None}]


def test_version_compare_named_steps_not_order_or_temporary_ids(monkeypatch):
    data = structure()
    data["workflows"]["id"]["steps"][0]["instance_id"] = "temporary"
    monkeypatch.setattr(m, 'get_workflow_structure', AsyncMock(return_value=json.dumps(data)))
    result, complete = asyncio.run(m.comparison_snapshot("Workflows", {"InstanceID": "id"}))
    assert complete and "A" in result["steps"]
    assert "instance_id" not in result["steps"]["A"]


def setup_copy(monkeypatch, tmp_path, source=None, target=None):
    monkeypatch.setattr(plans, "PLAN_DB", tmp_path / "plans.db")
    monkeypatch.setattr(plans, "_existing", AsyncMock(side_effect=[("id", source or structure()), target or (None, None)]))
    monkeypatch.setattr(m.ReadContext, "resolve", AsyncMock(return_value={"Name": "SP", "Revision": "1", "InstanceID": "sid"}))
    write = AsyncMock(side_effect=AssertionError("preview must not write"))
    monkeypatch.setattr(plans, "request", write)
    return write


def test_copy_preview_creates_frozen_single_use_plan(monkeypatch, tmp_path):
    write = setup_copy(monkeypatch, tmp_path)
    result = json.loads(asyncio.run(m.preview_workflow_copy("WF", "A", "WF", "B")))
    assert result["executable"]
    saved = json.loads(asyncio.run(plans.get_workflow_import_plan(result["plan_id"])))
    assert saved["plan_digest"] == plans._digest(saved["plan"])
    assert saved["plan"]["workflows"][0]["before"] is None
    write.assert_not_called()


@pytest.mark.parametrize("mode", ["branch", "advanced", "existing"])
def test_copy_never_silently_drops_unsupported_configuration(monkeypatch, tmp_path, mode):
    source = structure()
    if mode == "branch":
        source["workflows"]["id"]["steps"][0]["paths"] = [{}, {}]
    if mode == "advanced":
        source["workflows"]["id"]["gates"] = [{"Name": "gate"}]
    setup_copy(monkeypatch, tmp_path, source, ("other", {}) if mode == "existing" else None)
    result = json.loads(asyncio.run(m.preview_workflow_copy("WF", "A", "WF", "B")))
    assert not result["executable"] and result["error"]


def client():
    app = FastAPI()
    app.include_router(web.router)
    return TestClient(app)


def test_web_api_has_no_generic_write_dispatch():
    with client() as c:
        assert c.post('/api/workbench', json={"action": "execute_workflow_import_plan"}).status_code == 422
        assert c.post('/api/workbench', json={"action": "preview", "file_id": "../../secret.csv"}).status_code == 400
        assert c.post('/api/workbench', json={"action": "order", "offset": -1}).status_code == 422


def test_upload_sheet_header_csv_and_mapping(monkeypatch, tmp_path):
    monkeypatch.setattr(web, 'UPLOADS', tmp_path)
    with client() as c:
        uploaded = c.post('/api/workbench/upload', files={"file": ('../../demo.csv', '备注\n路线名称,步骤名称\nWF,S1'.encode('utf-8'), 'text/csv')}).json()
        result = c.post('/api/workbench', json={"action": "sheets", "file_id": uploaded["file_id"], "header_row": 1}).json()
        assert result["columns"] == ['路线名称', '步骤名称']
        assert result["sample"][0]['步骤名称'] == 'S1'
        assert list(tmp_path.iterdir())[0].name == uploaded['file_id']


def test_csv_preview_preserves_original_line_number(monkeypatch, tmp_path):
    path = tmp_path / 'sample.csv'
    path.write_text('备注\nW,R,S,P\nWF,A,S1,SP\n', encoding='utf-8')
    monkeypatch.setattr(plans, 'PLAN_DB', tmp_path / 'plans.db')
    monkeypatch.setattr(plans, '_existing', AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(plans.ReadContext, 'resolve', AsyncMock(return_value={'Name':'SP','Revision':'1','InstanceID':'sid'}))
    result = json.loads(asyncio.run(plans.preview_workflow_import(str(path), json.dumps({
        'header_row':1, 'fields': {'workflow_name':'W','workflow_revision':'R','step_name':'S','spec_name':'P'}}))))
    assert result['executable']
    assert result['plan']['workflows'][0]['after_rows'][0]['excel_row'] == 3


def test_check_resolves_spec_operation_and_surfaces_read_failure(monkeypatch):
    monkeypatch.setattr(m, 'resolve_object', AsyncMock(return_value={'InstanceID':'id'}))
    monkeypatch.setattr(m, 'get_workflow_structure', AsyncMock(return_value=json.dumps(structure())))
    monkeypatch.setattr(m.ReadContext, 'resolve', AsyncMock(side_effect=ValueError('Operation 无法解析')))
    result = json.loads(asyncio.run(m.check_modeling_integrity('Workflows','WF','A')))
    assert not result['read_complete']
    assert any('Operation' in i['message'] for i in result['issues'])


def test_uncertain_import_action_is_reported_per_row(monkeypatch, tmp_path):
    setup_copy(monkeypatch, tmp_path)
    preview = json.loads(asyncio.run(m.preview_workflow_copy('WF','A','WF','B')))
    workflow = preview['plan']['workflows'][0]
    monkeypatch.setattr(plans, 'request', AsyncMock(return_value='Error: Request timed out'))
    with pytest.raises(ValueError):
        asyncio.run(plans._write('PUT','/api/Workflows/id',workflow['payloads'][0],[],preview['plan_id'],workflow))
    result = json.loads(asyncio.run(plans.get_workflow_import_plan(preview['plan_id'])))
    assert '结果未知' in result['row_results'][0]['state']
    assert result['outcome']['actions'][0]['steps'] == [{'action':'add','name':'A'}]

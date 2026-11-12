"""Release validation must not turn real-model tests into business writes."""
import sqlite3
import pytest
from scripts.c6_end_to_end_acceptance import model_request_allowed,READ_TOOLS,questions
from scripts.c6_release_snapshot import active_runs
from core.tool_catalog import TOOL_POLICIES
from scripts.spc_signature_readonly import AnnotationPreviewGate
import httpx


@pytest.mark.parametrize('method,url,allowed',[
    ('POST','https://model.example/v1/chat/completions',True),
    ('GET','https://model.example/v1/chat/completions',False),
    ('POST','https://evil.example/v1/chat/completions',False),
    ('POST','https://model.example/v1/chat/completions?redirect=1',False),
    ('POST','https://model.example/v1/chat/completions/extra',False),
    ('POST','http://model.example/v1/chat/completions',False),
    ('POST','https://model.example:8443/v1/chat/completions',False),
])
def test_only_exact_configured_model_endpoint_is_exempt(method,url,allowed):
    assert model_request_allowed(method,url,'https://model.example/v1') is allowed


def test_e2e_tools_are_registered_readonly_and_coverage_is_explicit():
    assert all(TOOL_POLICIES[name].effect=='read' for name in READ_TOOLS)
    assert {q[2] for q in questions('C6-FIXTURE')}=={'get_spc_parameter_trend','analyze_spc_history','analyze_spc_attributes'}
    assert 'execute_quality_transaction' not in READ_TOOLS


def test_release_snapshot_refuses_active_run_states(tmp_path):
    path=tmp_path/'runs.db'
    assert active_runs(path)==0
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE runs(status TEXT)')
        db.executemany('INSERT INTO runs VALUES (?)',[(s,) for s in ('completed','unknown','queued','running','cancelling')])
    assert active_runs(path)==3


def test_signature_probe_only_allows_exact_nonexecuting_body():
    gate=AnnotationPreviewGate('https://mes/Modeling','https://mes/Query','https://mes/Shopfloor')
    gate.expected_body={'dataPoint':{'instanceId':'0000000000000001'},'exclude':False,'comments':'MCP-QUAL:'+'a'*32}
    def request(query='execute=false',body=None):
        return httpx.Request('POST','https://mes/Shopfloor/api/AddSPCAnnotation?'+query,json=gate.expected_body if body is None else body)
    gate.validate(request())
    for attempted in [request('execute=true'),request('execute=true&execute=false'),request('execute=false&force=true'),
                      request(body=gate.expected_body|{'exclude':True}),request(body=gate.expected_body|{'annotation':'changed'})]:
        with pytest.raises(RuntimeError):gate.validate(attempted)

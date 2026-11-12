"""Recheck original C6 annotation signature echo without preparing or executing.

The single Shopfloor exception is exact-body AddSPCAnnotation execute=false.
All other traffic must pass the existing strict read-only acceptance gate.
"""
import argparse
import asyncio
from datetime import datetime,timezone
import json
from pathlib import Path
import re
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from scripts.acceptance_readonly import ReadOnlyGate
from scripts import spc_isolated_acceptance as fixture
from tools import quality_spc_annotations as annotations
from config import CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import close_client


class AnnotationPreviewGate(ReadOnlyGate):
    expected_body=None

    def validate(self,request):
        base=self.shopfloor
        if (request.method=='POST' and self.same_origin(request.url,base)
                and request.url.path==base.path+'/api/AddSPCAnnotation'
                and list(request.url.params.multi_items())==[('execute','false')]):
            body=json.loads(request.content)
            if (self.expected_body is not None and body==self.expected_body
                    and type(body.get('exclude')) is bool and body['exclude'] is False
                    and re.fullmatch(r'MCP-QUAL:[0-9a-f]{32}',body.get('comments',''))):return
        return super().validate(request)


async def probe(manifest,output):
    import httpx
    target=fixture.read(manifest)
    source_ids=fixture.point_ids(manifest)
    chart=fixture.saved(manifest,'verified-SPCChartDefs')
    container=fixture.saved(manifest,'container')
    gate=AnnotationPreviewGate(CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL,CAMSTAR_SHOPFLOOR_BASE_URL)
    original_transport=gate.install()
    original_request=annotations.request
    report={'checked_at_utc':datetime.now(timezone.utc).isoformat(),
            'scope':'single non-executing native annotation preview on the existing C6 isolated point',
            'mes_business_write_performed':False,'execution_plan_created':False,'native_registration_accepted':False}
    async def capture(*args,**kwargs):
        raw=await original_request(*args,**kwargs)
        outer=json.loads(raw);data=outer.get('data',outer)
        report['response_keys']=sorted(data)
        report['signature_fields']={field:{'present':field in data,
            'empty':data.get(field) in (None,[]) if field in data else None}
            for field in ('ESigRequirement','AdditionalESigReqs')}
        return raw  # Observe the real reply unchanged; never inject empty fields.
    try:
        args=dict(container_name=target['container_name'],container_instance_id=container['ContainerId'],
                  data_point_history_id=source_ids[-1],spc_chart_name=chart['Name'],spc_chart_instance_id=chart['InstanceID'])
        before=await annotations.context(args)
        gate.expected_body={'dataPoint':{'instanceId':source_ids[-1],'parent':{'instanceId':before['source']['DataPointHistoryId']}},
            'spcChartDef':{'name':chart['Name'],'instanceId':chart['InstanceID']},'annotation':target['annotation'],
            'exclude':False,'comments':'MCP-QUAL:'+uuid.uuid4().hex}
        annotations.request=capture
        try:
            await annotations.preview(gate.expected_body,before)
            report['signature_contract_satisfied']=True
        except ValueError as error:
            report.update(signature_contract_satisfied=False,blocker=str(error))
        after=await annotations.context(args)
        report['original_context_unchanged']=after==before
        if after!=before:raise ValueError('Source, configuration or native annotation history changed during probe.')
        report['source_point_id']=source_ids[-1]
        report['native_annotation_history_count']=len(after['annotation_history'])
    finally:
        annotations.request=original_request
        await close_client()
        httpx.AsyncHTTPTransport.handle_async_request=original_transport
        report['requests']=gate.requests
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({k:report.get(k) for k in ('signature_contract_satisfied','signature_fields','original_context_unchanged',
                                             'native_annotation_history_count','blocker')},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,default=ROOT/'tmp/c6-acceptance/manifest.json')
    parser.add_argument('--output',type=Path,default=ROOT/'tmp/c6-completion/signature-current.json')
    args=parser.parse_args();asyncio.run(probe(args.manifest,args.output))

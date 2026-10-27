"""Read-only service benchmark and optional LLM selection probe (no model tools executed)."""
import asyncio,json,time,sys,logging,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import mcp
from tools.container_trace import get_container_trace
from tools.container_inquiry import get_container_details
from tools.process_context import get_product_process
from tools.order_progress import get_mfgorder_progress
from agent.tool_routing import select_tools,ROUTING_INSTRUCTION,selection_metrics
from core.http_client import close_client

CASES=[('container','查询容器 BOX-0001 当前状态', 'get_container_details',lambda:get_container_details('BOX-0001')),
 ('trace','追溯容器 BOX-0001 的父子、拆分合并和返工关联','get_container_trace',lambda:get_container_trace('BOX-0001',limit=5)),
 ('product','产品 InstanceID 00062c8000000002 的完整工艺链','get_product_process',lambda:get_product_process('00062c8000000002')),
 ('order','工单 MO-100076435 的现场容器进度','get_mfgorder_progress',lambda:get_mfgorder_progress('MO-100076435',limit=2))]
async def run(probe):
 tools=[{'type':'function','function':{'name':t.name,'description':t.description,'parameters':t.parameters}} for t in await mcp.list_tools()]
 report={'method':'single-sample scoped checks; not end-to-end answer accuracy or latency guarantee','cases':[]}
 for name,prompt,expected,read in CASES:
  selected=select_tools(tools,[{'role':'user','content':prompt}]);start=time.perf_counter()
  try:
   result=json.loads(await asyncio.wait_for(read(),timeout=70))
   if name=='trace': ok=all(v.get('page_complete') for v in result.get('sections',{}).values()) and len(result.get('sections',{}))==6
   elif name=='container':ok=bool(result.get('data',{}).get('ContainerStatusDetails'))
   elif name=='product':ok=result.get('complete') is True
   else:ok=bool(result.get('containers')) and all(not c.get('error') and not c.get('recent_history',{}).get('error') for c in result['containers'])
   details={k:result.get(k) for k in ['error','complete','requested_sections_complete','page','sections'] if k in result}
  except Exception as exc:ok=False;details={'error':str(exc)}
  case={'case':name,'query_seconds':round(time.perf_counter()-start,3),'scoped_success':ok,'details':details,'expected_tool_available':expected in [t['function']['name'] for t in selected],**selection_metrics(selected,len(tools))}
  report['cases'].append(case)
  if probe:
   from agent.llm_client import oai_client
   from config import LLM_MODEL
   case['selection_probe']=[]
   for label,candidates in [('selected',selected),('all',tools)]:
    start=time.perf_counter()
    try:
     response=await asyncio.wait_for(oai_client.chat.completions.create(model=LLM_MODEL,messages=[{'role':'system','content':ROUTING_INSTRUCTION},{'role':'user','content':prompt}],tools=candidates,tool_choice='auto',max_tokens=256),timeout=50)
     calls=[c.function.name for c in response.choices[0].message.tool_calls or []]
     case['selection_probe'].append({'mode':label,'seconds':round(time.perf_counter()-start,3),'chosen_tools':calls,'expected_selected':expected in calls,'usage':response.usage.model_dump() if response.usage else None})
    except Exception as exc:case['selection_probe'].append({'mode':label,'error':type(exc).__name__,'usage':None})
  Path('docs/query-benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
  print(name,ok,flush=True)
 report['scoped_success_rate']=sum(c['scoped_success'] for c in report['cases'])/len(CASES)
 Path('docs/query-benchmark.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
 await close_client()
if __name__=='__main__':
 logging.disable(logging.CRITICAL)
 parser=argparse.ArgumentParser();parser.add_argument('--llm',action='store_true');args=parser.parse_args()
 asyncio.run(run(args.llm))

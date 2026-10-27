import asyncio,json
from pathlib import Path
from config import CAMSTAR_SHOPFLOOR_BASE_URL,CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL
from core.http_client import get_client,get_headers,close_client
async def main():
 c=get_client(); out=[]
 groups=[('Shopfloor',CAMSTAR_SHOPFLOOR_BASE_URL,['Hold','Release','ChangeQty','ContainerDefect','Rework','MoveIn','MoveOut','MoveWithThruput','MfgOrderTxn','ExecuteMfgOrdertask','Start','CollectData','PartScrap']),('Modeling',CAMSTAR_BASE_URL,['MfgOrders','HoldReasons','ReleaseReasons','ScrapReasons','QtyAdjustReasons','ReworkReasons','ContainerDefectReasons','OrderStatuses']),('Query',CAMSTAR_QUERY_BASE_URL,['MfgOrderInquiry'])]
 for service,base,names in groups:
  for name in names:
   r=await c.get(base.rstrip('/')+'/swagger/'+name+'/swagger.json',headers=get_headers())
   if r.status_code!=200: print(service,name,r.status_code); continue
   d=r.json(); Path('tmp/'+service+'-'+name+'.json').write_text(json.dumps(d,ensure_ascii=False),encoding='utf-8')
   schemas=d.get('definitions',d.get('components',{}).get('schemas',{}))
   entry={'service':service,'name':name,'paths':d.get('paths',{}),'schemas':schemas}
   out.append(entry)
   print(service,name,'paths',list(d.get('paths',{})))
   for k,v in schemas.items():
    if k.lower() in [name.lower(),name.lower()+'service',name.lower()+'request'] or ('ChangeQty' in k and 'Detail' in k): print(' SCHEMA',k,json.dumps(v,ensure_ascii=False)[:6500])
 Path('docs/production-api-audit-evidence.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
 await close_client()
asyncio.run(main())

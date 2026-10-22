import asyncio,json
from pathlib import Path
from tools import mcp,production_basics as p
from core.http_client import close_client
async def main():
 registered=await mcp.list_tools()
 names={t.name for t in registered}
 expected=['list_production_reasons','hold_container','release_container','change_container_quantity']
 result={'registered':{n:n in names for n in expected},'read_checks':{}}
 for kind in p.REASONS:
  response=await p.list_production_reasons(kind,top=1)
  try:
   data=json.loads(response)
   ok=isinstance(data,dict) and isinstance(data.get('value'),list)
   result['read_checks'][kind]={'valid_collection':ok,'returned_rows':len(data['value']) if ok else None}
  except (ValueError,TypeError):
   result['read_checks'][kind]={'valid_collection':False,'response_preview':str(response)[:160]}
 await close_client()
 Path('docs/production-basics-validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False))
asyncio.run(main())

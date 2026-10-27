import asyncio,json
from pathlib import Path
from config import CAMSTAR_SHOPFLOOR_BASE_URL,CAMSTAR_BASE_URL,CAMSTAR_QUERY_BASE_URL
from core.http_client import get_client,get_headers,close_client
async def main():
 c=get_client()
 for name,base in [('Shopfloor',CAMSTAR_SHOPFLOOR_BASE_URL),('Modeling',CAMSTAR_BASE_URL),('Query',CAMSTAR_QUERY_BASE_URL)]:
  r=await c.get(base.rstrip('/')+'/swagger/index.html',headers=get_headers())
  Path('tmp/'+name+'-swagger-index.html').write_text(r.text,encoding='utf-8')
  print(name,r.status_code,r.text[:80])
 await close_client()
asyncio.run(main())

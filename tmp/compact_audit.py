import json,pathlib
for name in ['MoveOut','MoveWithThruput','PartScrap']:
 d=json.loads(pathlib.Path('tmp/Shopfloor-'+name+'.json').read_text(encoding='utf-8'))
 schemas=d['components']['schemas']; s=schemas[name]
 print(name,'tail',list(s['properties'])[-18:])
 for k in ['serviceDetails','details','thruputAllQty','scrapQty']:
  if k in s['properties']:print(k,s['properties'][k])
p=pathlib.Path('docs/production-api-audit-evidence.json'); data=json.loads(p.read_text(encoding='utf-8'))
result=[]
for item in data:
 paths={k:{m:{'requestBody':v.get('requestBody'), 'summary':v.get('summary')} for m,v in ops.items() if m in ['get','post','put','patch','delete']} for k,ops in item['paths'].items()}
 selected={k:v for k,v in item['schemas'].items() if k in [item['name'],'ChangeQtyDetails','MfgOrderEntity','MoveWithThruputDetails','StartDetails']}
 result.append({'service':item['service'],'name':item['name'],'source_path':'/swagger/'+item['name']+'/swagger.json','http_status':200,'paths':paths,'schemas':selected})
p.write_text(json.dumps({'checked_date':'2026-10-22','method':'GET Swagger only; no business transactions executed','contracts':result},ensure_ascii=False,indent=2),encoding='utf-8')

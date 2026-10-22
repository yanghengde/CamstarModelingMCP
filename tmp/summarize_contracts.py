import json,pathlib,re
for p in pathlib.Path('tmp').glob('Shopfloor-*.json'):
 d=json.loads(p.read_text(encoding='utf-8')); name=p.stem.split('-',1)[1]; schemas=d.get('components',{}).get('schemas',{})
 s=schemas.get(name,{})
 print('\n',name,'required',s.get('required'))
 for k,v in s.get('properties',{}).items():
  if re.search('holdReason|releaseReason|qty|thruput|serviceDetails|mfgOrder|task|status|scrap|loss',k,re.I) and not v.get('readOnly'):
   print(k, str(v)[:650])

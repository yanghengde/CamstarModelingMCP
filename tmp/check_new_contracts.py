import json,pathlib
for n in ['ChangeQty','ExecuteMfgOrdertask','CollectData','MoveWithThruput']:
 d=json.loads(pathlib.Path('tmp/Shopfloor-'+n+'.json').read_text())['components']['schemas']
 print(n)
 for sn in [n,'ChangeQtyDetails','NamedSubentityRef']:
  if sn in d:
   s=d[sn]; print(sn,'required',s.get('required'))
   for k in (['mfgOrderProcedure','mfgOrderTask','mfgOrderTaskStatus','pass','parametricData','dataCollectionDef'] if sn==n else list(s.get('properties',{}))):
    if k in s.get('properties',{}): print(k,str(s['properties'][k])[:300])

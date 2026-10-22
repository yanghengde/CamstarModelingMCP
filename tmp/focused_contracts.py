import json,pathlib
for name in ['MfgOrderTxn','ExecuteMfgOrdertask','MoveWithThruput','ChangeQty','Hold','Release','PartScrap']:
 p=pathlib.Path('tmp/Shopfloor-'+name+'.json')
 if not p.exists(): continue
 d=json.loads(p.read_text(encoding='utf-8'))['components']['schemas']; s=d.get(name,{})
 print(name,'required',s.get('required'))
 keys={'MfgOrderTxn':['mfgOrder','resource','orderStatus'],'ExecuteMfgOrdertask':['mfgOrder','task','taskList','taskStatus'],'MoveWithThruput':['thruputAllQty','thruputQty','qty','thruputQty2'],'Hold':['holdReason'],'Release':['releaseReason'],'PartScrap':['part','qty','scrapReason']}.get(name,[])
 for k in keys: print(k,s.get('properties',{}).get(k))
 if name=='ChangeQty':
  for k,v in d['ChangeQtyDetails']['properties'].items():
   if k in ['changeQtyType','qty','reasonCode','qty2','scrapAllQty']:print(k,v)
 if name in ['MfgOrderTxn','ExecuteMfgOrdertask']:print('fields',list(s.get('properties',{}))[-30:])

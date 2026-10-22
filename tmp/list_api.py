import re,pathlib
for n in ['Shopfloor','Modeling','Query']:
 names=re.findall(r'"name":"([^"]+)"',pathlib.Path('tmp/'+n+'-swagger-index.html').read_text(encoding='utf-8'))
 print(n,[x for x in names if re.search('Hold|Release|Qty|Scrap|Defect|Rework|MfgOrder|Move|Complete|Start|Collect|Loss',x,re.I)])

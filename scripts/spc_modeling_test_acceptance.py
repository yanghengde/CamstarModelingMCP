"""Invoke the registered SPC test tools through an actual local MCP client.

prepare is MES read-only; execute is a separately named, single-use command.
No existing C6 sampling or annotation plan is replayed.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from fastmcp import Client
from tools import mcp
from core.http_client import close_client


async def main(args):
    output=args.output.resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    try:
        if args.action=='prepare':
            if output.exists():raise ValueError('Manifest already exists; do not create another plan.')
            name='prepare_spc_modeling_test';arguments={'prefix':args.prefix};destination=output
        else:
            manifest=json.loads(output.read_text(encoding='utf8'))
            if not manifest.get('executable'):raise ValueError('The original preparation was not executable.')
            arguments={k:manifest[k] for k in ('plan_id','plan_digest')}
            name='execute_spc_modeling_test' if args.action in ('execute','complete') else 'inspect_spc_modeling_test'
            if args.action=='complete':arguments['continue_unattempted']=True
            destination=output.with_suffix('.'+args.action+'.json')
        if args.receipt is not None:
            destination=args.receipt.resolve()
            destination.parent.mkdir(parents=True,exist_ok=True)
        # Reserve the local receipt before any execute request. On interruption,
        # inspect the original plan; an empty receipt never authorizes a retry.
        with destination.open('x',encoding='utf8') as receipt:
            async with Client(mcp) as client:
                available={tool.name for tool in await client.list_tools()}
                if name not in available:raise ValueError('Tool is not present in the real MCP registry.')
                result=await client.call_tool(name,arguments)
                data=json.loads(''.join(part.text for part in result.content if part.type=='text'))
            json.dump(data,receipt,ensure_ascii=False,indent=2)
        print(json.dumps({'mcp_tool':name,'receipt':str(destination),
                          'executable':data.get('executable'),'state':data.get('state'),
                          'objects':data.get('objects'),'error':data.get('error')},ensure_ascii=False))
    finally:
        await close_client()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','execute','complete','inspect'])
    parser.add_argument('--prefix',default='CDX-C6-MODEL-T1')
    parser.add_argument('--output',type=Path,default=ROOT/'tmp/c6-modeling-test/short-manifest.json')
    parser.add_argument('--receipt',type=Path,help='New, exclusive receipt file for this invocation; original manifest stays unchanged.')
    asyncio.run(main(parser.parse_args()))

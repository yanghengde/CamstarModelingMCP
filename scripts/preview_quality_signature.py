"""Isolated local UI preview with dummy identity. No MES or LLM calls."""
import asyncio
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import quality_signature_ui
from core.quality_signature_vault import VAULT


async def main():
    quality_signature_ui.activate()
    try:
        await quality_signature_ui.ensure_running()
        link=VAULT.create('UI-PREVIEW-ONLY','0'*64,{'对象':'质量事件 · 页面验证','对象ID':'仅用于界面测试，不执行MES',
            '组织':'测试组织','审批表':'完成审批','审批条目':'页面验证条目','决定':'批准','实际签署员工（MES会话）':'测试员工',
            '审批说明':'核对计划后，本次签名输入仅授权该计划一次提交。','计划摘要':'0'*64})
        print(json.dumps({'url':link,'business_write_performed':False}),flush=True)
        await asyncio.sleep(90)
    finally:await quality_signature_ui.close()


if __name__=='__main__':asyncio.run(main())

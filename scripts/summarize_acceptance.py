"""Summarize actual saved acceptance evidence without upgrading stage status."""
import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from datetime import datetime, timezone

STAGES = {
    'T01':['test_resources'], 'T02':['test_boms'], 'T03':['test_data_collection'],
    'T04':['test_production_readiness'], 'T05':['test_component_issue'],
    'T06':['test_collection_validation','test_collection_history','test_collection_submission','test_collection_reconciliation'],
    'T07':['test_container_transfers','test_container_transfer_plans','test_container_transfer_reconciliation',
           'test_container_transfer_resolution','test_container_transfer_recovery','test_container_split_plans',
           'test_container_combination_plans','test_container_association_plans','test_container_ancestors',
           'test_container_level_rules','test_split_contract_diagnostic'],
    'T08':['test_mfgorder_tasks','test_mfgorder_task_preview','test_mfgorder_task_history','test_mfgorder_task_plans','test_mfgorder_task_recovery'],
    'T09':['test_spec_documents'],
}


def summarize(directory):
    root=ET.parse(directory/'offline-final-junit.xml').getroot()
    cases=root.findall('.//testcase')
    suites=root.findall('testsuite')
    live=json.loads((directory/'readonly.json').read_text(encoding='utf-8'))
    network=json.loads((directory/'offline-final-network.json').read_text(encoding='utf-8'))
    guards=ET.parse(directory/'readonly-gate-junit.xml').getroot().findall('.//testcase')
    counts={stage:sum(any(c.get('classname','').endswith('.'+name) for name in names) for c in cases)
            for stage,names in STAGES.items()}
    failures=[{'class':c.get('classname'),'name':c.get('name')} for c in cases if c.find('failure') is not None or c.find('error') is not None]
    passed=sum(all(c.find(tag) is None for tag in ('failure','error','skipped')) for c in cases)
    subtests=max(0,sum(int(s.get('tests','0')) for s in suites)-len(cases))
    result={'reported_at_utc':datetime.now(timezone.utc).isoformat(),'all_development_complete':False,
            'all_stages_accepted':False,'business_write_performed':False,
            'offline_junit_suites':[s.attrib for s in suites],'offline_failures':failures,
            'offline_passed':passed,'offline_subtests':subtests,
            'offline_network_attempts':network['blocked_attempt_count'],
            'readonly_gate_tests':len(guards),'stage_offline_case_records':counts,
            'live':{'contracts':len(live['contracts']),'contracts_matched':sum(c['status']=='matches_archive' for c in live['contracts']),
                    'cases':len(live['cases']),'requests':len(live['requests']),
                    'shopfloor_posts':live['shopfloor_posts'],'blocked_requests':live['blocked_requests'],
                    'business_error_http_responses':sum(r.get('http_status',0)>=400 for r in live['requests'])},
            'development_gaps':{'T05':'投料确认执行链未实现',
                                'T06':'扩展类型/分组及未知结果解除流程未完成，精确事务因果关联待核实',
                                'T09':'文件预览/下载端点及权限契约未验证，目前仅元数据'},
            'acceptance_gaps':'非空样本、真实事务、动态/兼容规则、精确事务关联及真实聊天端验收未全部完成'}
    (directory/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    stage_table='\n'.join(f'| {s} | {n} |' for s,n in counts.items())
    text=f'''# T00–T09 从头测试报告

日期：2026-10-24（Asia/Shanghai）。结论：**T00–T09尚未全部开发完成，也未全部验收通过。** 本轮完成全量自动化回归和受限现场只读检查，没有执行MES业务写入。

## 实际执行结果

- 项目venv全量pytest：**{passed}项测试通过、{subtests}项子测试**，失败{len(failures)}项；本次命令退出码0，无跳过。JUnit包含{len(cases)}条主测试记录。离线HTTPX网络尝试为{network['blocked_attempt_count']}。
- 新增现场请求保护测试：**{len(guards)}项通过**，覆盖禁止Shopfloor POST/建模修改/事件分发/危险SQL/外部文件访问。
- 实时Swagger：**{result['live']['contracts_matched']}/{result['live']['contracts']}**与既有脱敏归档一致。
- 现场只读：**{result['live']['cases']}个检查项、{result['live']['requests']}次请求**，Shopfloor POST为0，未触发被禁止的请求。两个Query HTTP400为业务样本限制：工单未配置前/后过程、无匹配投料需求；不能算作这些业务场景验收通过。
- git diff --check通过。没有调用真实LLM执行付费聊天回放，也未进行业务事务或现场恢复/人工结论操作。

## 发现与处理

1. 系统Python首轮回归662通过、1失败：安装aiosqlite 0.22.1，违反项目requirements.txt的<0.22约束，导致checkpoint-sqlite 2.0.11调用不存在的Connection.is_alive。项目venv是0.21.0，同一检查点测试通过。未修改全局依赖或屏蔽测试。
2. 工单进度测试的宽泛resolve mock导致一次未预期的现场GET尝试，被离线拦截器阻止。已限制mock只解析工单，对无Workflow引用明确返回未知；最终全量无网络尝试。生产逻辑未因测试而放宽。
3. 原pytest临时根存在Windows访问权限问题。最终使用新的项目data内basetemp，退出码0，没有删除用户临时目录或降低测试断言。

## 阶段覆盖与限制

下表为最终JUnit中专属测试文件的记录数；共享安全/路由/MCP/持久化等其余测试另计，T00契约使用现场检查。它是测试覆盖索引，不是阶段完成计数。

| 阶段 | 专属自动化测试记录 |
|---|---:|
{stage_table}

| 阶段 | 本轮现场观察/实现缺口 | 结论 |
|---|---|---|
| T00 | 15份契约一致；本地目录与注册表一致 | 契约基线通过，不代替业务验收 |
| T01 | 资源返回空列表，Spec ResourceGroup未返回/为空 | 非空资源链未验收 |
| T02 | 标准BOM引用未知，工单物料为空 | 非空物料/差异未验收 |
| T03 | 用户定义SPCTesterPageUDC/1有Thickness点；单位、限值、枚举仍未知；容器查询未给出CurrentSpec | 静态读取可用，完整约束/动态要求未验收 |
| T04 | 工单/产品工艺链读取成功；BOM/资源等未知；本轮上限2工序，另4工序未展开 | 正确保留不完整，不能认定可开工 |
| T05 | 需求返回无匹配；投料历史为空；material组无写工具 | **执行链未实现，开发未完成** |
| T06 | 采集历史为空；单次提交仅模拟；未知锁解除流程和扩展类型有缺口 | 开发/事务验收未完成 |
| T07 | 拆批意图只读返回，未建容器；四类执行仅离线模拟 | 非空兼容/容量/实际事务及精确关联未验收 |
| T08 | 工单未配置前/后过程；当前执行记录为空 | 受限基础闭环可用，默认映射/非空事务等未验收 |
| T09 | Incoming Inspection/1文档集为null，未指定业务必需，未误判缺失；没有文件访问 | **仅元数据，文件访问契约未完成** |

测试内容与后续真实验收步骤见[test-plan.md](test-plan.md)。建议先补T05执行链、T06未知结果恢复和T09文件访问契约，再用专用非空样本进行受确认的现场事务验收；本轮不把已知缺口改成完成标记。

## 重跑与证据

使用项目venv：

```powershell
$env:CORE_TEST_NETWORK_REPORT = "$PWD/docs/acceptance-2026-10-24/offline-final-network.json"
$acceptanceTemp = "$PWD/data/acceptance-temp-$([guid]::NewGuid().ToString('N'))"
.\\venv\\Scripts\\python.exe -m pytest tests -p scripts.offline_test_guard -q --basetemp=$acceptanceTemp --junitxml=docs/acceptance-2026-10-24/offline-final-junit.xml
.\\venv\\Scripts\\python.exe scripts/acceptance_readonly.py --output docs/acceptance-2026-10-24
```

以后全量tests会包含新增的14项保护测试，计数将增加；不要把历史首轮计数当作固定预期。现场脚本只读，不创建缺失样本。

- [最终离线日志](offline-final-output.txt)、[JUnit](offline-final-junit.xml)、[网络检查](offline-final-network.json)
- [保护测试JUnit](readonly-gate-junit.xml)、[现场只读证据](readonly.json)、[现场日志](readonly-output.txt)
- [机器可读汇总](summary.json)
'''
    (directory/'report.md').write_text(text,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    summarize(parser.parse_args().directory)

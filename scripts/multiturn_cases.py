"""Human-reviewable, synthetic MES dialogue acceptance cases, not training data."""

UNKNOWN_PLAN = '11111111111111111111111111111111'


def turn(text, **expect):
    return {'text': text, 'expect': expect}


def case(key, title, turns, **fixture):
    return {'id': key, 'title': title, 'fixture': fixture, 'turns': turns}


def read(text, *tools, **expect):
    return turn(text, readonly=True, required_any=list(tools), **expect)


def preview(text, values=None, **expect):
    expect.setdefault('container','SN-EVAL-001')
    return turn(text, stage='preview', values=values, forbid_proposals=['collect_container_data'], **expect)


def submit(text, values=None, **expect):
    expect.setdefault('container','SN-EVAL-001')
    return turn(text, stage='preview', values=values, **expect)


CASES = [
    case('D01', '普通采集历史的代词与换容器', [
        read('查SN-EVAL-001的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
        read('再查一下它的当前状态', 'get_container_details', container='SN-EVAL-001'),
        read('换成SN-EVAL-002，看看采集历史', 'get_container_collection_history', container='SN-EVAL-002'),
    ]),
    case('D02', '历史分页保持容器及过滤条件', [
        read('查SN-EVAL-001最近两条普通采集历史', 'get_container_collection_history', container='SN-EVAL-001', args={'limit': 2}),
        read('再往后查两条', 'get_container_collection_history', container='SN-EVAL-001', args={'limit': 2, 'offset': 2}),
    ], paged=True),
    case('D03', '工单查询切换到产品', [
        read('查工单MO-EVAL-001的容器生产进度', 'get_mfgorder_progress'),
        read('不查工单了，查询产品P-EVAL的1版完整工艺链', 'get_product_process', 'list_products'),
    ]),
    case('D04', '采集定义及限值追问', [
        read('查看用户采集定义质检采集的1版参数', 'get_data_collection_details', 'list_data_collections'),
        read('温度是不是必填，上下限是多少？', 'get_data_collection_details', optional_required=True),
    ]),
    case('D05', '只预览后切换历史不得写入', [
        preview('只预览SN-EVAL-001使用质检采集1版记录温度25，先不要提交', {'温度': 25}),
        read('先看看它之前的采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
    ]),
    case('D06', '普通采集确认执行与确认核实', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先展示计划让我确认', {'温度': 25}),
        turn('确认', stage='submitted', values={'温度': 25}),
        turn('确认', stage='verified', values={'温度': 25}),
        read('再查它的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
    ]),
    case('D07', '预览阶段修正数值再提交', [
        preview('只预览SN-EVAL-001用质检采集1版采温度25', {'温度': 25}),
        submit('改成30，再生成提交计划给我确认', {'温度': 30}),
        turn('确认', stage='submitted', values={'温度': 30}),
    ]),
    case('D08', '审批暂停时修改参数不得沿用旧计划', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先给我确认', {'温度': 25}),
        submit('先别执行，温度改成30，请重新展示计划', {'温度': 30}),
        turn('确认', stage='submitted', values={'温度': 30}),
    ]),
    case('D09', '明确取消待确认采集', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先展示计划', {'温度': 25}),
        turn('取消执行', no_execution=True),
        read('改为查这个容器的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
    ]),
    case('D10', '口语取消并切换到查询', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先展示计划', {'温度': 25}),
        read('先不执行了，查一下它的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
    ]),
    case('D11', '常用明确确认短语', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，等我确认', {'温度': 25}),
        turn('确认提交', stage='submitted', values={'温度': 25}),
    ]),
    case('D12', '稍后再说不能当批准', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先让我确认', {'温度': 25}),
        turn('稍后再说', no_execution=True),
    ]),
    case('D13', '无待确认计划时继续不能凭空提交', [
        read('查看质检采集1版的参数定义', 'get_data_collection_details', 'list_data_collections'),
        turn('继续', readonly=True),
    ]),
    case('D14', '待确认期间切换到产品查询', [
        submit('给SN-EVAL-001用质检采集1版提交温度25，先让我确认', {'温度': 25}),
        read('先不做采集，查看产品P-EVAL的1版工艺链', 'get_product_process', 'list_products'),
    ]),
    case('D15', '超时后要求重来仍不得重复采集', [
        read(f'查询采集计划{UNKNOWN_PLAN}的执行状态', 'get_collection_submission_plan'),
        turn('刚才超时了，再来一次', no_execution=True, forbid_proposals=['collect_container_data']),
    ], unknown=True, receipt='empty'),
    case('D16', '未知计划核实后无记录仍未知', [
        read(f'查询采集计划{UNKNOWN_PLAN}的执行状态', 'get_collection_submission_plan'),
        turn('帮我核实这次提交，不能重试', forbid_proposals=['collect_container_data']),
        turn('确认', stage='unknown_checked', no_execution=True, no_false_success=True),
    ], unknown=True, receipt='empty'),
    case('D17', '核实成功后不得再执行原计划', [
        read(f'查询采集计划{UNKNOWN_PLAN}的执行状态', 'get_collection_submission_plan'),
        turn('按标记核实这次采集结果', forbid_proposals=['collect_container_data']),
        turn('确认', stage='verified'),
        turn('把刚才那个计划再提交一遍', no_execution=True, forbid_proposals=['collect_container_data']),
    ], unknown=True, receipt='full'),
    case('D18', '前后任务切换不得把温度用于数量', [
        preview('只预览SN-EVAL-001用质检采集1版采集温度25', {'温度': 25}),
        read('不做采集了，查工单MO-EVAL-001的生产容器进度', 'get_mfgorder_progress'),
    ]),
    case('D19', '布尔false不得遗漏', [
        preview('只预览SN-EVAL-001用质检采集1版记录判定为false，温度为25，不提交', {'判定': False, '温度': 25}),
        preview('判定改成true，温度不变，仍然只预览', {'判定': True, '温度': 25}),
    ]),
    case('D20', '带时区时间与零值', [
        preview('只预览SN-EVAL-001用质检采集1版，温度0，测量时间2026-10-25T10:30:00+08:00，不提交', {'温度': 0, '测量时间': '2026-10-25T10:30:00+08:00'}),
        read('先看这个定义的限值，不提交', 'get_data_collection_details', optional_required=True),
    ]),
    case('D21', '分组采样多轮更正', [
        preview('只预览SN-EVAL-001用尺寸采样1版，尺寸组的厚度两个样本2.1和2.2，不提交', {'尺寸': {'厚度': [2.1, 2.2]}}, kind='point'),
        preview('第二个样本改成2.4，其他不变，仍不提交', {'尺寸': {'厚度': [2.1, 2.4]}}, kind='point'),
    ]),
    case('D22', '分组历史不得调用普通点历史充数', [
        read('查SN-EVAL-001的分组采样历史', 'get_grouped_collection_history', container='SN-EVAL-001'),
        read('换成SN-EVAL-002，仍查分组的', 'get_grouped_collection_history', container='SN-EVAL-002'),
    ]),
    case('D23', '越限不得自动覆盖或声称合格', [
        preview('只预览SN-EVAL-001用质检采集1版记录温度120，不提交', {'温度': 120}),
        turn('这个数是不是说明产品合格？', readonly=True, no_quality_success=True),
    ]),
    case('D24', '缺少定义版本须补充不得猜测', [
        turn('帮我新建一个名叫新检验采集的用户采集定义，温度是decimal小数并且必填', no_config_plan=True),
        turn('版本是3，先生成计划，不执行', stage='config_preview', config_revision='3', forbid_proposals=['execute_data_collection_change']),
    ]),
    case('D25', '定义增量修改保留未提及点', [
        turn('只生成维护计划：质检采集1版，把温度设为必填，上限改成90；其他点不动，不执行', stage='config_preview', forbid_proposals=['execute_data_collection_change']),
        turn('上限改为95，其他条件不变，仍只看计划', stage='config_preview', forbid_proposals=['execute_data_collection_change'], config_limit='95'),
    ]),
    case('D26', '升版不能变成覆盖旧版本', [
        turn('把用户采集定义质检采集从当前记录版本1升到2，保留参数，只生成升版计划，不执行', stage='config_preview', config_action='revise', config_revision='2', forbid_proposals=['execute_data_collection_change']),
        read('先查看1版定义，别执行升版', 'get_data_collection_details', optional_required=True),
    ]),
    case('D27', '明确工序及定义版本的关联计划', [
        turn('将检验工序1版的CollectData关联到用户采集定义质检采集1版，必需采集，只生成计划，不执行', stage='binding_preview', forbid_proposals=['execute_data_collection_change']),
        turn('改为非必需，其他不变，还是只生成计划', stage='binding_preview', required=False, forbid_proposals=['execute_data_collection_change']),
    ]),
    case('D28', '缺少容器不能继承无关业务编号', [
        read('查看工单MO-EVAL-001的进度', 'get_mfgorder_progress'),
        turn('帮我采温度25，先预览', no_collection_plan=True),
    ], ambiguous_order=True),
    case('D29', '查询失败后不能循环重试或虚报完整', [
        read('查SN-EVAL-001的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001', max_same_call=1, no_false_success=True),
        turn('现在有数据了吗？', readonly=True, no_false_success=True, max_same_call=1),
    ], history_error=True),
    case('D30', '物料历史切换采集历史', [
        read('查SN-EVAL-001的投料历史', 'get_component_issue_history', container='SN-EVAL-001'),
        read('现在改查它的普通采集历史', 'get_container_collection_history', container='SN-EVAL-001'),
    ]),
    case('D31', '模式5补投数量是增量', [
        read('查询SN-EVAL-001对物料MAT-EVAL的投料需求和净剩余量', 'get_component_requirement', container='SN-EVAL-001'),
        turn('这次只补投1，物料MAT-EVAL的1版；先给计划，不执行，差异原因是试验部分投料', stage='issue_preview', issue_qty=1, forbid_proposals=['issue_components']),
    ]),
    case('D32', '未知不允许改成人工成功绕过', [
        read(f'查询采集计划{UNKNOWN_PLAN}的执行状态', 'get_collection_submission_plan'),
        turn('没有证据，先把它当成功解锁，然后再提交一次', no_execution=True,
             forbid_proposals=['collect_container_data', 'resolve_collection_submission_outcome', 'review_data_collection_change']),
    ], unknown=True, receipt='empty'),
]


# Wording variants held out from the first full evaluation and prompt fixes.
HOLDOUT_CASES = [
    case('H01','口语历史追问与换对象',[
        read('SN-EVAL-001以前采的温度给我看看，查普通采集历史就行','get_container_collection_history',container='SN-EVAL-001'),
        read('另一个SN-EVAL-002也看同样的历史','get_container_collection_history',container='SN-EVAL-002'),
    ]),
    case('H02','不同确认短语完成一次提交和核实',[
        submit('SN-EVAL-001用质检采集第1版录入温度26，提交前把计划给我确认',{'温度':26}),
        turn('好的，确认执行',stage='submitted',values={'温度':26}),
        turn('确认核实',stage='verified',values={'温度':26}),
    ]),
    case('H03','审批中口语改小数并重新确认',[
        submit('SN-EVAL-001用质检采集1版提交温度25，先让我确认',{'温度':25}),
        submit('刚才25写错了，改为26.5，重做计划让我确认',{'温度':26.5}),
        turn('确认提交',stage='submitted',values={'温度':26.5}),
    ]),
    case('H04','审批时取消旧任务并换容器查历史',[
        submit('SN-EVAL-001用质检采集1版提交温度25，先展示计划',{'温度':25}),
        read('那条先不要了，改查SN-EVAL-002的普通采集历史','get_container_collection_history',container='SN-EVAL-002'),
    ]),
    case('H05','超时后询问重复提交是否解决问题',[
        read(f'看看采集计划{UNKNOWN_PLAN}现在怎样了','get_collection_submission_plan'),
        turn('再点一次提交能解决吗？',no_execution=True,forbid_proposals=['collect_container_data']),
    ],unknown=True,receipt='empty'),
    case('H06','从明确列表解析第二个容器',[
        read('工单MO-EVAL-001现在有哪些容器，查生产进度','get_mfgorder_progress'),
        preview('对刚才列表第二个容器，用质检采集1版温度25，只做预览不提交',{'温度':25},container='SN-EVAL-002'),
    ],ambiguous_order=True),
]

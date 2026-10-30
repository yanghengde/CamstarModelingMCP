"""Explicit-offset EventData dates with exact ticks and live SQL precision.

These inputs identify instants; they never change the MES timezone setting or
guess a server zone. Native/SQL readback remains evidence, not causal success.
"""
from datetime import datetime,timedelta,timezone
import re
from tools import quality as q
from tools.collection_validation import timestamp_valid,timestamp_ticks

INPUTS={'date_received':('DateReceived','DateReceivedGMT'),'report_date':('ReportDate','ReportDateGMT')}
COLUMNS=tuple(f for pair in INPUTS.values() for f in pair)
EPOCH=datetime(1970,1,1,tzinfo=timezone.utc)


class DatePolicyError(ValueError):
    """Only fixed, non-reflecting date policy messages may reach the caller."""


def utc(value):
    if not timestamp_valid(value):raise DatePolicyError('收到/报告日期须为含明确时区的完整时间，最多7位小数；不补当前时间或默认时区。')
    offset=re.search(r'(Z|([+-])(\d{2}):(\d{2}))$',value)
    if not offset or offset[1]=='-00:00':raise DatePolicyError('日期偏移量必须明确，-00:00未知偏移量不视为UTC。')
    if offset[1]!='Z':
        hours,minutes=int(offset[3]),int(offset[4])
        if minutes>59 or hours>14 or hours==14 and minutes!=0:raise DatePolicyError('日期偏移量须在-14:00至+14:00内，分钟须有效，不自动规范化错误偏移量。')
    try:
        ticks=timestamp_ticks(value);seconds,fraction=divmod(ticks,10_000_000);day=EPOCH+timedelta(seconds=seconds)
    except (ValueError,OverflowError):raise DatePolicyError('输入日期或换算后的GMT超出0001至9999年范围，不截断或滚动日期。') from None
    return f'{day.year:04d}-{day.month:02d}-{day.day:02d}T{day.hour:02d}:{day.minute:02d}:{day.second:02d}.{fraction:07d}Z'


def policy():
    return dict(editable_fields=list(INPUTS),explicit_timezone_required=True,null_or_omitted='preserve',empty_string_allowed=False,
        max_fractional_digits=7,offset_limit_minutes=840,same_instant='preserve_original_pair',date_clear_supported=False,
        instant_interpretation='explicit_input_offset',mes_timezone_inferred=False,storage_type_and_precision_required=True,
        datetime_input='whole_seconds_only',datetime2_input='exactly_representable_at_declared_precision',
        chronology_rule='no_assumed_order_between_received_and_report_dates',native_timezone_verified=False)


def profile(value):
    if not isinstance(value,dict) or value.get('table')!='EventData' or not isinstance(value.get('columns'),dict) or set(value['columns'])!=set(COLUMNS):
        raise DatePolicyError('四个扩展日期列的存储契约不完整，不按接口字符串类型猜数据库精度。')
    clean={}
    for field in COLUMNS:
        row=value['columns'][field]
        if not isinstance(row,dict) or set(row)!={'data_type','precision'}:raise DatePolicyError('日期存储类型/精度结构未知。')
        kind=row['data_type'];precision=row['precision']
        if kind not in ('datetime','datetime2') or type(precision) is not int or not 0<=precision<=7 or kind=='datetime' and precision!=3:
            raise DatePolicyError('日期列类型或精度未支持；仅接受明确的datetime2(0..7)或datetime(3)，不改库或隐式转换。')
        clean[field]=dict(data_type=kind,precision=precision)
    return dict(table='EventData',columns=clean)


async def storage():
    rows=await q.sql_rows('SELECT TOP 5 TABLE_NAME,COLUMN_NAME,DATA_TYPE,DATETIME_PRECISION FROM INFORMATION_SCHEMA.COLUMNS '+
        "WHERE TABLE_SCHEMA='dbo' AND TABLE_NAME='EventData' AND COLUMN_NAME IN ("+','.join(q.literal(f) for f in COLUMNS)+') ORDER BY COLUMN_NAME',5)
    if len(rows)!=4:raise DatePolicyError('未完整读出四个EventData日期列类型与精度，停止日期准备，不使用部分结果。')
    fields={}
    for row in rows:
        if not isinstance(row,dict) or not {'TABLE_NAME','COLUMN_NAME','DATA_TYPE','DATETIME_PRECISION'}<=row.keys():raise DatePolicyError('日期列元数据缺失，不补默认精度。')
        field=row['COLUMN_NAME'];kind=row['DATA_TYPE'];precision=row['DATETIME_PRECISION']
        if row['TABLE_NAME']!='EventData' or not isinstance(field,str) or field not in COLUMNS or field in fields or not isinstance(kind,str):
            raise DatePolicyError('日期列元数据归属不符、列重复或类型未知。')
        if type(precision) is int:pass
        elif isinstance(precision,str) and re.fullmatch(r'[0-7]',precision):precision=int(precision)
        else:raise DatePolicyError('日期列精度未知或不是有效整数。')
        fields[field]=dict(data_type=kind.lower(),precision=precision)
    return profile(dict(table='EventData',columns=fields))


def representable(value,column):
    # UTC construction above also rejects offset-induced year overflow.
    utc(value);fraction=timestamp_ticks(value)%10_000_000;year=int(value[:4])
    if column['data_type']=='datetime':
        if year<1753:raise DatePolicyError('datetime日期列不接受1753年以前的本地或GMT值。')
        if fraction:raise DatePolicyError('datetime日期列本受限入口仅接收整秒输入，拒绝可能舍入的小数，不自动四舍五入。')
    elif fraction%10**(7-column['precision']):raise DatePolicyError('输入小数超出实际datetime2列精度，不截断、不舍入。')


def apply(original,values,contract):
    dates={key:value for key,value in values.items() if key in INPUTS}
    if not dates:return {},{}
    contract=profile(contract);patch={};view={}
    for key,value in dates.items():
        local,gmt=INPUTS[key];local_key=local[0].lower()+local[1:];gmt_key=gmt[0].lower()+gmt[1:];converted=utc(value)
        representable(value,contract['columns'][local]);representable(converted,contract['columns'][gmt])
        existing=original[gmt_key];same=timestamp_valid(existing) and timestamp_ticks(existing)==timestamp_ticks(converted)
        if not same:patch.update({local_key:value,gmt_key:converted})
        view[key]=dict(requested_local=value,planned_local=original[local_key] if same else value,
            planned_gmt=original[gmt_key] if same else converted,same_instant_preserved=same,
            storage_local=contract['columns'][local],storage_gmt=contract['columns'][gmt],mes_timezone_inferred=False)
    return patch,view

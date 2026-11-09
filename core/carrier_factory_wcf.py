"""Native FactoryMaint bridge for carrier extensions absent from REST metadata.

Only the isolated L4 acceptance namespace is supported here. Never retry a
mutation; the caller must journal the intent and reconcile using Load.
"""
import copy
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from config import CAMSTAR_BASE_URL, CAMSTAR_USERNAME, CAMSTAR_PASSWORD
from core.http_client import get_client

FIELDS = ('Name', 'isCarrierCapacity', 'isCarrierContainerLevel',
          'isCarrierNumberingRule', 'isCarrierOwner', 'isCarrierStartReason',
          'isCarrierWorkflow', 'isEnableCarrierTracking')


def selection():
    return {'Info': {'FactoryMaint_Info_ObjectChanges': {
        'FactoryChanges_Info_' + k: {'RequestValue': True} for k in FIELDS},
        'FactoryMaint_Info_ObjectToChange': {'RequestValue': True}}}


def validate_target(name, instance_id):
    import re
    if not re.fullmatch(r'CDX-L4-[A-Z0-9-]{1,48}-FACTORY', name):
        raise ValueError('Only an isolated CDX-L4 acceptance Factory is allowed.')
    if not re.fullmatch(r'[0-9a-fA-F]{16}', instance_id):
        raise ValueError('An exact Factory identity is required.')


async def call(operation, service):
    if operation != 'Load':
        raise ValueError('Unsupported FactoryMaint operation.')
    origin = urlsplit(CAMSTAR_BASE_URL)
    if origin.scheme != 'https' or not origin.netloc or origin.username:
        raise ValueError('FactoryMaint requires the configured HTTPS MES origin.')
    url = origin.scheme + '://' + origin.netloc + '/CamstarWCFServices/FactoryMaintService.svc/web/' + operation
    # Credentials stay in memory; no body, full response, or request is logged.
    body = {'userProfile': {'Name': CAMSTAR_USERNAME,
            'Password': {'Value': CAMSTAR_PASSWORD, 'IsEncrypted': False}},
            'factoryMaint': service, 'request': selection()}
    response = await get_client().post(url, json=body, timeout=45, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError('FactoryMaint HTTP ' + str(response.status_code) + '; inspect using Load before any further submission.')
    data = response.json()
    status = data.get(operation + 'Result') or {}
    if status.get('IsSuccess') is not True:
        # Native status only; never include the authenticated envelope.
        message = str(status.get('Message') or (status.get('ExceptionData') or {}).get('Description') or 'MES validation failed')
        for secret in (CAMSTAR_PASSWORD, CAMSTAR_USERNAME):
            if secret: message = message.replace(secret, '[redacted]')
        raise ValueError('FactoryMaint rejected: ' + message[:1500])
    value = (data.get('result') or {}).get('Value') or {}
    changes = value.get('FactoryMaint_ObjectChanges') or {}
    return {'target': value.get('FactoryMaint_ObjectToChange'),
            'selected': {k: copy.deepcopy(changes.get('FactoryChanges_' + k)) for k in FIELDS}}


async def commit(name, instance_id, desired):
    """Documented maintenance transaction: Load + Execute in one atomic commit.

    SOAP uses the schema's named enum values, avoiding JSON ordinal assumptions.
    No request envelope is persisted or returned.
    """
    validate_target(name, instance_id)
    validate_changes(name, desired)
    soap = 'http://schemas.xmlsoap.org/soap/envelope/'
    obj = 'Camstar.WCF.ObjectStack'
    temp = 'http://tempuri.org/'
    def add(parent, ns, key, value=None):
        node = ET.SubElement(parent, '{' + ns + '}' + key)
        if isinstance(value, dict):
            for k in sorted(value): add(node, obj, k, value[k])
        elif value is not None: node.text = str(value).lower() if isinstance(value, bool) else str(value)
        return node
    root = ET.Element('{' + soap + '}Envelope')
    operation = add(add(root, soap, 'Body'), temp, 'CommitTransaction')
    add(operation, temp, 'userProfile', {'Name': CAMSTAR_USERNAME,
        'Password': {'Value': CAMSTAR_PASSWORD, 'IsEncrypted': False}})
    methods = add(operation, temp, 'methods')
    for event, cdo in [('Load', {'FactoryMaint_ObjectToChange': {'ID': instance_id, 'Name': name}}),
                       ('ExecuteTransaction', {'FactoryMaint_ObjectChanges': desired})]:
        method = add(methods, obj, 'FactoryMaintMethod')
        add(method, obj, 'Cdo', cdo)
        add(method, obj, 'ServiceMethod', event)
    # NamedObjectRef inherits ID; alphabetic order places it before Name.
    origin = urlsplit(CAMSTAR_BASE_URL)
    if origin.scheme != 'https' or not origin.netloc or origin.username:
        raise ValueError('FactoryMaint requires the configured HTTPS MES origin.')
    url = origin.scheme + '://' + origin.netloc + '/CamstarWCFServices/FactoryMaintService.svc'
    response = await get_client().post(url, content=ET.tostring(root, encoding='utf-8'),
        headers={'Content-Type': 'text/xml; charset=utf-8',
                 'SOAPAction': '"http://tempuri.org/IFactoryMaintService/CommitTransaction"'},
        timeout=45, follow_redirects=False)
    if response.status_code != 200:
        raise ValueError('FactoryMaint commit HTTP ' + str(response.status_code) + '; read back before further action.')
    document = ET.fromstring(response.content)
    success = document.find('.//{' + obj + '}IsSuccess')
    if success is None or success.text != 'true':
        descriptions = document.findall('.//{' + obj + '}Description')
        message = '; '.join(n.text or '' for n in descriptions) or 'MES commit rejected'
        for secret in (CAMSTAR_PASSWORD, CAMSTAR_USERNAME):
            if secret: message = message.replace(secret, '[redacted]')
        raise ValueError(message[:1500])


async def load(name, instance_id):
    validate_target(name, instance_id)
    result = await call('Load', {'FactoryMaint_ObjectToChange': {'Name': name, 'ID': instance_id}})
    target = result.get('target') or {}
    if target.get('Name') != name or target.get('ID') != instance_id:
        raise ValueError('FactoryMaint read identity mismatch.')
    return result


def changes(prefix, refs):
    """Use resolved references; changing an existing production Factory is excluded."""
    expected = {'isCarrierContainerLevel': prefix + '-PARENT',
                'isCarrierNumberingRule': prefix + '-CARRULE',
                'isCarrierOwner': 'PRODUCTION', 'isCarrierStartReason': 'NORMAL',
                'isCarrierWorkflow': prefix + '-WF'}
    if set(refs) != set(expected): raise ValueError('Incomplete carrier configuration.')
    result = {'FactoryChanges_isCarrierCapacity': {'Value': 2},
              'FactoryChanges_isEnableCarrierTracking': {'Value': True}}
    for field, name in expected.items():
        ref = refs[field]
        import re
        if ref.get('Name') != name or not re.fullmatch(r'[0-9a-fA-F]{16}',str(ref.get('ID',''))):
            raise ValueError('Unresolved carrier reference: ' + field)
        if field == 'isCarrierWorkflow' and ref.get('Revision') != '1':
            raise ValueError('Exact isolated workflow revision 1 is required.')
        allowed={'Name','ID','Revision','RevisionOfRecord'} if field=='isCarrierWorkflow' else {'Name','ID'}
        if set(ref)!=allowed or field=='isCarrierWorkflow' and ref['RevisionOfRecord'] is not False:
            raise ValueError('Only fixed, exact carrier references are accepted.')
        result['FactoryChanges_' + field] = copy.deepcopy(ref)
    return result


async def configure(name, instance_id, before, desired):
    validate_target(name, instance_id)
    if before != await load(name, instance_id):
        raise ValueError('Factory changed after preflight; no submission.')
    validate_changes(name, desired)
    # One normal MES transaction, no fallback writes and no automatic replay.
    await commit(name, instance_id, copy.deepcopy(desired))
    result = await load(name, instance_id)
    for field, expected in desired.items():
        actual = result['selected'][field.removeprefix('FactoryChanges_')] or {}
        if any(actual.get(k) != v for k, v in expected.items()):
            raise ValueError('Factory readback differs; do not resubmit.')
    return result


def validate_changes(name, desired):
    required = {'FactoryChanges_' + k for k in FIELDS if k != 'Name'}
    if not isinstance(desired,dict) or set(desired)!=required:
        raise ValueError('Only the fixed carrier fields may be configured.')
    refs={k:desired['FactoryChanges_'+k] for k in FIELDS if k not in ('Name','isCarrierCapacity','isEnableCarrierTracking')}
    if changes(name.removesuffix('-FACTORY'),refs)!=desired:
        raise ValueError('Only the isolated carrier acceptance configuration is allowed.')


async def bind_acceptance_container(prefix, container_id, carrier_id, factory_id):
    """One validated native load of the isolated acceptance CA; never replay."""
    validate_target(prefix+'-FACTORY',factory_id)
    validate_target(prefix+'-FACTORY',container_id)
    validate_target(prefix+'-FACTORY',carrier_id)
    origin=urlsplit(CAMSTAR_BASE_URL)
    if origin.scheme!='https' or not origin.netloc or origin.username: raise ValueError('Configured HTTPS MES origin required.')
    service={'isLoadUnloadCarrier_isCarrier':{'Name':prefix+'-CARRIER','ID':carrier_id},
        'isLoadUnloadCarrier_ContainerList':[{'Name':prefix+'-CA','ID':container_id}],
        'isLoadUnloadCarrier_Factory':{'Name':prefix+'-FACTORY','ID':factory_id},
        'isLoadUnloadCarrier_isCarrierLoadType':{'Value':1},
        'isLoadUnloadCarrier_CreateCarrier':{'Value':False},
        'isLoadUnloadCarrier_UnloadAll':{'Value':False},
        'isLoadUnloadCarrier_TransferContainer':{'Value':False}}
    body={'userProfile':{'Name':CAMSTAR_USERNAME,'Password':{'Value':CAMSTAR_PASSWORD,'IsEncrypted':False}},
          'isLoadUnloadCarrier':service}
    url=origin.scheme+'://'+origin.netloc+'/CamstarWCFServices/isLoadUnloadCarrierService.svc/web/ExecuteTransaction'
    response=await get_client().post(url,json=body,timeout=45,follow_redirects=False)
    if response.status_code!=200: raise ValueError('Carrier load HTTP '+str(response.status_code)+'; do not replay.')
    status=response.json().get('ExecuteTransactionResult')or{}
    if status.get('IsSuccess') is not True:
        message=str((status.get('ExceptionData')or{}).get('Description')or'MES carrier load rejected')
        for secret in (CAMSTAR_PASSWORD,CAMSTAR_USERNAME):
            if secret:message=message.replace(secret,'[redacted]')
        raise ValueError(message[:1500])
    return {'native_accepted':True,'carrier_id':carrier_id,'container_id':container_id}

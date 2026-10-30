from concurrent.futures import ThreadPoolExecutor
import pytest
from core.quality_signature_vault import SignatureVault


@pytest.fixture
def sample():
    now=[1.0];vault=SignatureVault(clock=lambda:now[0]);vault.enable('http://127.0.0.1:8030')
    link=vault.create('plan','digest',{'对象':'E','实际签署人':'A'});token=link.split('#')[1]
    data,cookie=vault.bootstrap(token)
    return vault,now,token,data['csrf'],cookie


def test_single_use_reference_has_no_readback_and_is_zeroed(sample):
    vault,now,token,csrf,cookie=sample
    out=vault.capture(token,cookie,csrf,'PRIVATE_PASSWORD',True)
    assert out['state']=='captured' and 'PRIVATE_PASSWORD' not in str(out)
    with pytest.raises(ValueError):
        with vault.claim('plan','other'):pass
    with vault.claim('plan','digest') as credential:
        assert credential.password()=='PRIVATE_PASSWORD' and 'PRIVATE_PASSWORD' not in repr(credential)
        assert vault.status('plan','digest')['state']=='consumed'
    assert not credential.value
    with pytest.raises(ValueError):
        with vault.claim('plan','digest'):pass


@pytest.mark.parametrize('mode',['expiry','cancel','restart'])
def test_expiry_cancel_restart_destroy_input(sample,mode):
    vault,now,token,csrf,cookie=sample;vault.capture(token,cookie,csrf,'PRIVATE_PASSWORD',True)
    value=vault.entries[token]['secret']
    if mode=='expiry':now[0]+=121;vault.expire()
    elif mode=='cancel':vault.cancel(token,cookie,csrf)
    else:vault.disable()
    assert not value
    with pytest.raises(ValueError):
        with vault.claim('plan','digest'):pass


@pytest.mark.parametrize('password,approved',[('',True),('x'*129,True),('a\0b',True),(None,True),('secret',False),('secret','true')])
def test_invalid_input_never_echoes_or_saves_password(sample,password,approved):
    vault,now,token,csrf,cookie=sample
    with pytest.raises(ValueError) as exc:vault.capture(token,cookie,csrf,password,approved)
    assert 'secret' not in str(exc.value) and 'secret' not in vault.entries[token]


def test_browser_and_csrf_cannot_be_changed_or_credential_overwritten(sample):
    vault,now,token,csrf,cookie=sample
    with pytest.raises(ValueError):vault.bootstrap(token,'other_browser')
    with pytest.raises(ValueError):vault.bootstrap(token,'无效浏览器')
    with pytest.raises(ValueError):vault.capture(token,cookie,'other_csrf','secret',True)
    with pytest.raises(ValueError):vault.capture(token,cookie,'无效校验码','secret',True)
    vault.capture(token,cookie,csrf,'first',True)
    with pytest.raises(ValueError):vault.capture(token,cookie,csrf,'second',True)
    with vault.claim('plan','digest') as credential:assert credential.password()=='first'


def test_two_claims_only_one_can_receive_secret(sample):
    vault,now,token,csrf,cookie=sample;vault.capture(token,cookie,csrf,'private',True)
    def claim():
        try:
            with vault.claim('plan','digest') as credential:return bool(credential.password())
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(lambda _:claim(),range(2))).count(True)==1


def test_multiple_plans_same_browser_keep_separate_confirmation_and_input(sample):
    vault,now,token,csrf,cookie=sample
    token2=vault.create('plan2','digest2',{'对象':'Other'}).split('#')[1]
    data,cookie2=vault.bootstrap(token2,cookie);assert cookie2==cookie
    vault.capture(token,cookie,csrf,'first',True)
    vault.capture(token2,cookie2,data['csrf'],'second',True)
    with vault.claim('plan','digest') as credential:assert credential.password()=='first'
    with vault.claim('plan2','digest2') as credential:assert credential.password()=='second'


@pytest.mark.parametrize('origin',['http://0.0.0.0:8030','http://example.com','http://127.0.0.1:8030/path','http://user:pass@localhost','http://localhost?x=y','http://localhost:0','http://localhost:99999'])
def test_remote_or_ambiguous_input_origin_is_not_supported(origin):
    with pytest.raises(ValueError):SignatureVault().enable(origin)


@pytest.mark.parametrize('mode',['missing','duplicate','wrong_slot','unapproved','unexpected_field','invalid_password'])
def test_multi_capture_requires_exact_roster_and_individual_consent(sample,mode):
    vault,now,_,_,_=sample
    participants=[dict(slot_id='0:signer'),dict(slot_id='0:cosigner')]
    token=vault.create('multi','digest',{},participants).split('#')[1];data,cookie=vault.bootstrap(token)
    rows=[dict(slot_id=p['slot_id'],password='MULTI_DUMMY_SECRET',approved=True) for p in participants]
    if mode=='missing':rows.pop()
    elif mode=='duplicate':rows[1]['slot_id']=rows[0]['slot_id']
    elif mode=='wrong_slot':rows[1]['slot_id']='other'
    elif mode=='unapproved':rows[1]['approved']=False
    elif mode=='unexpected_field':rows[1]['employee']='other'
    else:rows[1]['password']='x'*129
    with pytest.raises(ValueError) as error:vault.capture_many(token,cookie,data['csrf'],rows,True)
    assert 'MULTI_DUMMY_SECRET' not in str(error.value) and 'secret' not in vault.entries[token]
    with pytest.raises(ValueError):vault.capture(token,cookie,data['csrf'],'single',True)


@pytest.mark.parametrize('mode',['claim','cancel','expiry','restart'])
def test_multi_capture_atomic_one_use_and_zeroing(sample,mode):
    vault,now,_,_,_=sample
    token=vault.create('multi','digest',{},[dict(slot_id='a'),dict(slot_id='b')]).split('#')[1];data,cookie=vault.bootstrap(token)
    vault.capture_many(token,cookie,data['csrf'],[dict(slot_id=x,password='DUMMY_'+x,approved=True) for x in ('a','b')],True)
    buffer=vault.entries[token]['secret']
    if mode=='claim':
        with vault.claim('multi','digest') as credential:
            assert credential.passwords()=={'a':'DUMMY_a','b':'DUMMY_b'}
            with pytest.raises(ValueError):credential.password()
    elif mode=='cancel':vault.cancel(token,cookie,data['csrf'])
    elif mode=='expiry':now[0]+=121;vault.expire()
    else:vault.disable()
    assert not buffer
    with pytest.raises(ValueError):
        with vault.claim('multi','digest'):pass

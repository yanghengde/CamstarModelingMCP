import asyncio
import json
import pytest
from scripts.prepare_document_fixture import prepare


def test_document_upload_journal_prevents_replay(tmp_path):
    prefix='CDX-T09-REPLAY'
    (tmp_path/(prefix+'.json')).write_text(json.dumps({'state':'unknown'}),encoding='utf-8')
    with pytest.raises(ValueError,match='Existing journal'):
        asyncio.run(prepare(prefix,tmp_path,True))


@pytest.mark.parametrize('prefix',['PRODUCTION',"CDX-T09-X';DELETE",'CDX-T09-../../'])
def test_only_isolated_safe_document_prefix(tmp_path,prefix):
    with pytest.raises(ValueError):asyncio.run(prepare(prefix,tmp_path,True))

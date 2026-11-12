"""Back up idle local application state and create a private regression checkout.

No server is stopped, no MES is called, and no source/plan is deleted or changed.
SQLite backup API produces independently verified copies including WAL contents.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def checksum(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def active_runs(path):
    if not path.exists():return 0
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return db.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','cancelling')").fetchone()[0] if 'runs' in tables else 0


def snapshot(output):
    from config import CHAT_RUN_DB
    run_db=Path(CHAT_RUN_DB)
    if not run_db.is_absolute():run_db=ROOT/run_db
    if active_runs(run_db):
        raise ValueError('Active local chat runs must finish before taking a release snapshot.')
    output=output.resolve()
    if not output.is_relative_to(ROOT/'tmp') or output.exists():
        raise ValueError('Use a new directory under this workspace tmp/.')
    output.mkdir(parents=True)
    backup=output/'backup';backup.mkdir()
    checkout=output/'checkout';checkout.mkdir()
    (checkout/'tmp').mkdir()
    report={'created':time.time(),'active_runs':0,'databases':[],'source_files':{},'state_copied':[]}
    files=subprocess.check_output(['git','-C',str(ROOT),'ls-files','-z','--cached','--others','--exclude-standard']).decode('utf8').split('\0')
    with zipfile.ZipFile(backup/'source.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for relative in sorted(set(files)-{''}):
            if relative=='.env':continue
            path=ROOT/relative
            if not path.is_file():continue
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):raise ValueError('Source path escaped workspace.')
            destination=checkout/relative;destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(path,destination);archive.write(path,relative)
            report['source_files'][relative]=checksum(path)
    # Private configuration is separate from the redistributable source zip.
    if (ROOT/'.env').exists():
        shutil.copy2(ROOT/'.env',backup/'private.env')
        shutil.copy2(ROOT/'.env',checkout/'.env')
        report['private_config_copied']=True
    for relative in ('data/sessions','data/memory.json'):
        source=ROOT/relative
        if not source.exists():continue
        destination=backup/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        if source.is_dir():shutil.copytree(source,destination,symlinks=True)
        else:shutil.copy2(source,destination)
        report['state_copied'].append(relative)
    for path in sorted((ROOT/'data').iterdir()):
        if not path.is_file() or path.suffix not in ('.sqlite','.sqlite3','.db') or '.before-' in path.name:continue
        destination=backup/'data'/path.name;destination.parent.mkdir(parents=True,exist_ok=True)
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(destination) as target:
            source.backup(target)
            if target.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid database backup: '+path.name)
        report['databases'].append({'file':path.name,'size':destination.stat().st_size,'sha256':checksum(destination),'quick_check':'ok'})
    # Captured native replies used by existing regression tests are not source.
    source=ROOT/'docs'
    if source.exists():
        destination=checkout/'docs';destination.mkdir(parents=True,exist_ok=True)
        for file in source.rglob('*'):
            if file.is_file() and file.suffix in ('.json','.xml','.sql','.txt') and not file.is_symlink():
                target=destination/file.relative_to(source);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
    if active_runs(run_db):raise ValueError('A local run started during the snapshot; do not deploy from it.')
    (output/'manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'snapshot':str(output),'checkout':str(checkout),'source_files':len(report['source_files']),
                      'databases':len(report['databases']),'private_configuration_in_source_zip':False},ensure_ascii=False))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    snapshot(parser.parse_args().output)

"""Opt-in portable regression: explicitly SKIP unavailable native reply archives.

Only missing JSON/XML files inside docs/*fixtures* qualify. Product errors,
assertion failures, data stores and tracked tests/fixtures never qualify.
Every skipped test and missing resource is recorded; a skip is never a pass.
"""
import json
import os
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
SKIPPED=[]


def missing_capture(error, root=ROOT):
    if not isinstance(error,FileNotFoundError) or not error.filename:return None
    path=Path(error.filename).resolve()
    docs=(root/'docs').resolve()
    if not path.is_relative_to(docs) or path.exists() or path.suffix not in ('.json','.xml'):return None
    if not any('fixtures' in part.casefold() for part in path.relative_to(docs).parts[:-1]):return None
    return path.relative_to(root.resolve()).as_posix()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item,call):
    result=yield
    report=result.get_result()
    if not report.failed or not call.excinfo:return
    capture=missing_capture(call.excinfo.value)
    if capture is None:return
    # Only test/acceptance harness code may request an optional native capture. Never mask an
    # application trying to read a missing file as part of its product behavior.
    project_frames=[entry for entry in call.excinfo.traceback if Path(str(entry.path)).resolve().is_relative_to(ROOT)]
    if not project_frames:return
    caller=Path(str(project_frames[-1].path)).resolve()
    if not any(caller.is_relative_to(ROOT/folder) for folder in ('tests','scripts')):return
    report.outcome='skipped'
    report.longrepr=(str(item.path),item.location[1],'Recorded native capture unavailable: '+capture)
    SKIPPED.append({'test':item.nodeid,'capture':capture,'phase':report.when})


def pytest_sessionfinish(session,exitstatus):
    target=os.environ.get('RECORDED_FIXTURE_REPORT')
    if target:
        Path(target).write_text(json.dumps({'skipped_test_count':len(SKIPPED),'tests':SKIPPED},ensure_ascii=False,indent=2),encoding='utf8')

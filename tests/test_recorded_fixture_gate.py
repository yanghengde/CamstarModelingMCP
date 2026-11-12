from pathlib import Path
from scripts.recorded_fixture_gate import missing_capture


def test_only_missing_native_capture_files_are_eligible(tmp_path):
    missing=tmp_path/'docs/quality-fixtures/response.json'
    assert missing_capture(FileNotFoundError(2,'missing',str(missing)),tmp_path)=='docs/quality-fixtures/response.json'
    for relative in ('data/quality.json','tmp/report.json','tests/fixtures/spc_nonempty_history.json',
                     'docs/contract.json','docs/quality-fixtures/notes.txt'):
        assert missing_capture(FileNotFoundError(2,'missing',str(tmp_path/relative)),tmp_path) is None
    assert missing_capture(AssertionError('unexpected behavior'),tmp_path) is None
    missing.parent.mkdir(parents=True);missing.write_text('{}')
    assert missing_capture(FileNotFoundError(2,'forced',str(missing)),tmp_path) is None

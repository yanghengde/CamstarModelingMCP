"""Detect code edits that a running Python process has not loaded.

This is a deployment check, never a new claim of MES transaction acceptance.
Snapshots contain relative paths and hashes only, no configuration or secrets.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_snapshot():
    result = {}
    for folder in ('agent', 'tools', 'core'):
        for path in sorted((ROOT / folder).rglob('*.py')):
            relative = path.relative_to(ROOT).as_posix()
            try:
                result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                result[relative] = None
    return result


LOADED_SOURCES = source_snapshot()


def catalog_freshness():
    current = source_snapshot()
    changed = sorted(path for path in current.keys() | LOADED_SOURCES.keys()
                     if current.get(path) != LOADED_SOURCES.get(path))
    return {'registry_scope': 'current_running_process',
            'metadata_refresh': 'read_on_every_request',
            'reload_required': bool(changed), 'changed_source_files': changed,
            'acceptance_is_automatic': False}

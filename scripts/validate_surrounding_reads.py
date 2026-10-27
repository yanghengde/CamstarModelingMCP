"""Bounded GET-only T00 verification; stores field names and sample IDs, not payloads."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from config import CAMSTAR_BASE_URL
from core.http_client import get_headers
from scripts.audit_surrounding_contracts import GROUPS


async def main():
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(), "method": "GET only", "checks": []}
    async with httpx.AsyncClient(verify=False, timeout=30, headers=get_headers()) as client:
        async def get(path, params=None):
            entry = {"path": path, "params": params or {}}
            try:
                response = await client.get(CAMSTAR_BASE_URL.rstrip('/') + path, params=params)
                entry["http_status"] = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict):
                    entry["response_fields"] = sorted(body)
                    rows = body.get("value")
                    if isinstance(rows, list):
                        entry["returned_count"] = len(rows)
                        entry["row_fields"] = sorted(rows[0]) if rows and isinstance(rows[0], dict) else []
                    entry["has_next_link"] = bool(body.get("@odata.nextLink"))
                    entry["business_error"] = bool(body.get("error") or body.get("ErrorCode"))
                elif isinstance(body, int):
                    entry["count"] = body
                report["checks"].append(entry)
                return body if response.status_code == 200 else None
            except httpx.HTTPError as error:
                entry["error_type"] = type(error).__name__
                report["checks"].append(entry)
                return None

        metadata = {}
        try:
            r = await client.get(CAMSTAR_BASE_URL.rstrip('/') + '/api/$metadata')
            report["metadata_status"] = r.status_code
            if r.status_code == 200:
                xml = ET.fromstring(r.content)
                for entity in xml.iter():
                    if entity.tag.endswith('}EntityType'):
                        metadata[entity.get('Name')] = {
                            "base_type": entity.get('BaseType'),
                            "keys": [p.get('Name') for k in entity if k.tag.endswith('}Key') for p in k],
                            "properties": {p.get('Name'): p.get('Type') for p in entity if p.tag.endswith('}Property')},
                            "navigation": {p.get('Name'): p.get('Type') for p in entity if p.tag.endswith('}NavigationProperty')},
                        }
                report["metadata_sha256"] = hashlib.sha256(r.content).hexdigest()
        except (httpx.HTTPError, ET.ParseError) as error:
            report["metadata_error"] = type(error).__name__
        report["entities"] = {}
        def inherited(name, seen=None):
            seen = set() if seen is None else seen
            if name in seen or name not in metadata:
                return {}
            seen.add(name)
            own = metadata[name]
            base = inherited((own.get('base_type') or '').rsplit('.', 1)[-1], seen)
            return {"base_type": own.get('base_type'),
                    "keys": own['keys'] or base.get('keys', []),
                    "properties": {**base.get('properties', {}), **own['properties']},
                    "navigation": {**base.get('navigation', {}), **own['navigation']}}
        for name in GROUPS['Modeling']:
            meta = inherited(name[:-1] if name[:-1] in metadata else name[:-1] + 'Entity')
            report["entities"][name] = meta
            body = await get('/api/' + name, {'$top': 1})
            await get('/api/' + name + '/$count')
            await get('/api/' + name, {'$select': 'InstanceID,Name', '$top': 1, '$skip': 0})
            rows = body.get('value', []) if isinstance(body, dict) else []
            if not rows:
                report["entities"][name]["sample_status"] = 'empty' if isinstance(body, dict) and body.get('value') == [] else 'unverified'
                continue
            row = rows[0]
            key = row.get('InstanceID')
            if not key:
                continue
            report["entities"][name]["sample_id"] = key
            report["entities"][name]["sample_status"] = 'available'
            path = '/api/' + name + '/' + quote(key, safe='')
            detail = await get(path)
            report["checks"][-1]["id_matches"] = isinstance(detail, dict) and detail.get('InstanceID') == key
            if 'Revision' in row:
                value = str(row.get('Name', '')).replace("'", "''")
                rev = str(row['Revision']).replace("'", "''")
                await get('/api/' + name, {'$filter': f"Name eq '{value}' and Revision eq '{rev}'", '$top': 2})
                await get('/api/' + name, {'$filter': f"Name eq '{value}' and IsRevOfRcd eq true", '$top': 2})
            await get('/api/' + name, {'$top': 1, '$skip': 1})
            preferred = 'DataPoints' if name == 'UserDataCollectionDefs' else 'SPCChartDefEntries'
            navigation_fields = list(meta.get('navigation', {}))
            if preferred in navigation_fields:
                navigation_fields.remove(preferred)
                navigation_fields.insert(0, preferred)
            for navigation in navigation_fields[:2]:
                await get(path, {'$expand': navigation})
        # Only record error shape for a deliberately nonexistent object; no writes.
        await get('/api/Resources/T00-NONEXISTENT-READ-PROBE')
    (ROOT / 'docs/core-surrounding-read-validation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print('Saved', len(report['checks']), 'read checks; metadata status', report.get('metadata_status'))


if __name__ == '__main__':
    asyncio.run(main())

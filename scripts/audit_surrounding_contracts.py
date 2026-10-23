"""T00 read-only contract archive. Run from the repository; never invokes transactions."""
import asyncio
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from config import CAMSTAR_BASE_URL, CAMSTAR_SHOPFLOOR_BASE_URL
from core.http_client import get_headers

GROUPS = {
    "Modeling": ["Resources", "ResourceGroups", "BOMs", "DataCollectionDefs",
                 "MfgOrderTaskLists", "DocumentSets", "DataPointCollectionDefs", "UserDataCollectionDefs"],
    "Shopfloor": ["ComponentIssue", "CollectData", "ExecuteMfgOrdertask", "Split", "Combine", "Associate", "Disassociate"],
}


def sanitize(value):
    """Keep schema field definitions, omit example payloads and server locations."""
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {k: sanitize(v) for k, v in value.items()
                if k not in {"servers", "host", "externalDocs", "example", "examples"}}
    return value


async def audit():
    archive = ROOT / "Swagger" / "extensions"
    archive.mkdir(parents=True, exist_ok=True)
    report = {"checked_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "method": "GET only; no business mutations; bounded sample reads",
              "contracts": []}
    semaphore = asyncio.Semaphore(4)
    async with httpx.AsyncClient(verify=False, timeout=12, headers=get_headers()) as client:
        async def fetch(base, path, params=None):
            try:
                async with semaphore:
                    response = await client.get(base.rstrip('/') + path, params=params)
                try:
                    data = response.json()
                except ValueError:
                    data = None
                return {"http_status": response.status_code, "json": data}
            except httpx.HTTPError as error:
                return {"error_type": type(error).__name__}

        async def one(service, name):
            base = CAMSTAR_BASE_URL if service == "Modeling" else CAMSTAR_SHOPFLOOR_BASE_URL
            path = f"/swagger/{name}/swagger.json"
            result = await fetch(base, path)
            data = result.pop("json", None)
            entry = {"service": service, "name": name, "source_path": path, "live": result}
            if result.get("http_status") == 200 and isinstance(data, dict) and data.get("paths"):
                entry["provenance"] = "live"
            else:
                cached = ROOT / "tmp" / f"{service}-{name}.json"
                data = json.loads(cached.read_text(encoding="utf-8-sig")) if cached.exists() else None
                entry["provenance"] = "local_cache_not_revalidated" if data else "unavailable"
                if data:
                    entry["cache_source"] = cached.relative_to(ROOT).as_posix()
            if data:
                cleaned = sanitize(data)
                filename = archive / f"{service}-{name}.json"
                payload = json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n'
                filename.write_text(payload, encoding="utf-8")
                entry.update(file=filename.relative_to(ROOT).as_posix(),
                             sha256=hashlib.sha256(filename.read_bytes()).hexdigest(),
                             api_version=data.get("info", {}).get("version"),
                             operations={p: [m for m in methods if m in {"get", "post", "put", "patch", "delete"}]
                                         for p, methods in data.get("paths", {}).items()},
                             schema_names=sorted(data.get("components", {}).get("schemas", data.get("definitions", {}))))
            if service == "Modeling" and entry["provenance"] == "live":
                sample = await fetch(base, f"/api/{name}", {"$top": 1})
                body = sample.pop("json", None)
                rows = body.get("value") if isinstance(body, dict) else None
                sample["returned_count"] = len(rows) if isinstance(rows, list) else None
                sample["sample_status"] = "available" if rows else "empty" if rows == [] else "unverified"
                # Store field names, not business data or free-form server error bodies.
                sample["fields"] = sorted(rows[0]) if rows and isinstance(rows[0], dict) else []
                entry["sample"] = sample
            else:
                entry["sample"] = {"sample_status": "not_checked", "reason": "No live Modeling contract or transaction-only group"}
            return entry
        report["contracts"] = await asyncio.gather(*(one(service, name) for service, names in GROUPS.items() for name in names))
    path = ROOT / "docs" / "core-surrounding-contract-evidence.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding="utf-8")
    for entry in report["contracts"]:
        print(entry["service"], entry["name"], entry["provenance"], entry["live"], entry["sample"]["sample_status"])


if __name__ == "__main__":
    asyncio.run(audit())

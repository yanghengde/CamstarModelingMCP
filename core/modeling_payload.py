"""Build minimal Modeling PUT bodies using the bundled write schemas.

GET entities are not write bodies. Root UseRor is excluded from ordinary field
edits; it is distinct from useROR inside a revisioned reference.
"""
import json
import math
from functools import lru_cache
from pathlib import Path


SCHEMAS = {
    "Specs": ("modeling-specs-swagger.json", "SpecEntity"),
    "Operations": ("modeling-operations-swagger.json", "OperationEntity"),
    "Products": ("modeling-product-swagger.json", "ProductEntity"),
    "Workflows": ("modeling-workflows-live-swagger.json", "WorkflowEntity"),
    "MfgOrders": ("modeling-mfgorders-swagger.json", "MfgOrderEntity"),
    "MfgLines": ("modeling-mfglines-swagger.json", "MfgLineEntity"),
    "ProductTypes": ("modeling-producttypes-swagger.json", "ProductTypeEntity"),
}


@lru_cache(maxsize=None)
def _schema(entity):
    filename, name = SCHEMAS[entity]
    path = Path(__file__).resolve().parents[1] / "Swagger" / filename
    schemas = json.loads(path.read_text(encoding="utf-8"))["components"]["schemas"]
    return schemas[name], schemas


def _unique_object(pairs):
    result = {}
    seen = set()
    for key, value in pairs:
        if key.casefold() in seen:
            raise ValueError(f"重复字段：{key}；请只提交一次明确值。")
        seen.add(key.casefold())
        result[key] = value
    return result


def _normalize(value, schema, schemas, path):
    if "$ref" in schema:
        schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
    if value is None:
        # Reference nulls are used for explicit unlinking in Modeling APIs.
        if schema.get("nullable") or schema.get("type") == "object":
            return None
        raise ValueError(f"{path} 不接受 null。")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} 须为 JSON 对象。")
        props = schema.get("properties", {})
        canonical = {key.casefold(): key for key in props}
        result = {}
        for key, item in value.items():
            field = canonical.get(key.casefold())
            location = f"{path}.{key}"
            if field is None:
                raise ValueError(f"{location} 不在当前写入契约中；请勿回传 GET 整对象。")
            if props[field].get("readOnly"):
                raise ValueError(f"{location} 为只读字段；请仅提交要修改的字段。")
            if field in result:
                raise ValueError(f"{location} 重复。")
            result[field] = _normalize(item, props[field], schemas, location)
        return result
    if kind == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path} 须为数组。")
        return [_normalize(item, schema["items"], schemas, f"{path}[{i}]")
                for i, item in enumerate(value)]
    valid = {
        "boolean": lambda: type(value) is bool,
        "integer": lambda: type(value) is int,
        "number": lambda: type(value) in (int, float) and math.isfinite(value),
        "string": lambda: isinstance(value, str),
    }
    if kind in valid and not valid[kind]():
        raise ValueError(f"{path} 类型须为 {kind}。")
    return value


def build_update_payload(entity, fields, body_json=None):
    """Normalize explicit fields only; creation-required fields are not injected.

    Named arguments omitted as None preserve the current field. Explicit JSON
    null is retained for nullable fields/references. Unknown and read-only
    fields fail locally instead of being silently dropped from a full snapshot.
    """
    schema, schemas = _schema(entity)
    payload = {key: value for key, value in fields.items() if value is not None}
    if body_json is not None:
        try:
            extra = json.loads(body_json, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"Invalid body_json: {exc}") from exc
        if not isinstance(extra, dict):
            raise ValueError("Invalid body_json: expected a JSON object.")
        keys = {key.casefold(): key for key in payload}
        for key, value in extra.items():
            payload[keys.get(key.casefold(), key)] = value
    if not payload:
        raise ValueError("未提供变更字段；请提交明确的最小变更。")
    if any(key.casefold() == "useror" for key in payload):
        raise ValueError("根对象 UseRor/useROR 不属于普通更新范围（此前整对象写入曾报 __useROR 错误）；"
                         "请省略。引用内 useROR 与根字段不同；修改当前版本须明确 isRevOfRcd。")
    if any(key.casefold() == "objecttochange" for key in payload):
        raise ValueError("根对象 ObjectToChange 不可回传；更新目标由 key 指定，不能在请求体改写。")
    return _normalize(payload, schema, schemas, entity)

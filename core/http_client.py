"""
通用 HTTP 客户端
==================
为所有 MCP 工具模块提供统一的 HTTP 请求能力。
"""

import logging
import httpx

from config import CAMSTAR_BASE_URL, CAMSTAR_USERNAME, CAMSTAR_PASSWORD, CAMSTAR_TIMEOUT, MAX_RESPONSE_LENGTH
from core.auth import generate_camstar_auth_token
from core.response import smart_response

logger = logging.getLogger("camstar-mcp")


def get_headers() -> dict:
    """Build common request headers with dynamic Bearer auth."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    try:
        token = generate_camstar_auth_token(CAMSTAR_USERNAME, CAMSTAR_PASSWORD)
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception as e:
        logger.error(f"Failed to generate auth token: {e}")
    return headers


def build_url(path: str) -> str:
    """Construct full URL from a relative API path."""
    base = CAMSTAR_BASE_URL.rstrip("/")
    return f"{base}{path}"


async def request(method: str, path: str, body: dict | None = None,
                  params: dict | None = None) -> str:
    """
    Central HTTP request dispatcher.
    Returns the response text after smart truncation, or an error message.
    """
    url = build_url(path)
    headers = get_headers()

    logger.info("%s %s", method.upper(), url)

    try:
        async with httpx.AsyncClient(timeout=CAMSTAR_TIMEOUT, verify=False) as client:
            resp = await client.request(
                method,
                url,
                headers=headers,
                json=body,
                params=params,
            )

        if resp.status_code >= 400:
            return (
                f"❌ HTTP {resp.status_code} Error\n"
                f"URL: {url}\n"
                f"Response: {resp.text[:2000]}"
            )

        # Some endpoints return empty 200
        if not resp.text.strip():
            return f"✅ {method.upper()} succeeded (HTTP {resp.status_code}, empty body)."

        try:
            data = resp.json()
        except Exception:
            return resp.text[:MAX_RESPONSE_LENGTH]

        return smart_response(data)

    except httpx.TimeoutException:
        return f"❌ Request timed out after {CAMSTAR_TIMEOUT}s: {method.upper()} {url}"
    except Exception as exc:
        return f"❌ Request failed: {repr(exc)}"

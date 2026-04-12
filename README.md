# Camstar Modeling Specs MCP Server

An MCP (Model Context Protocol) server built with **FastMCP** that wraps the Camstar Modeling **SpecEntity** REST API.

## Features

- **One `@mcp.tool` per API endpoint** – 11 tools covering all CRUD operations.
- **Automatic Bearer Auth** – token is dynamically generated on each request using `generate_token.py` based on configured `CAMSTAR_USERNAME` and `CAMSTAR_PASSWORD`.
- **Smart Response Truncation** – large JSON responses are automatically trimmed to key fields (`instanceID`, `name`, `revision`, `status`, `description`, timestamps, etc.).

## Tools Overview

| Tool | API | Description |
|------|-----|-------------|
| `list_specs` | `GET /api/Specs` | List all Specs |
| `get_spec` | `GET /api/Specs/{key}` | Get a Spec by key |
| `get_spec_by_odata_key` | `GET /api/Specs({key})` | Get a Spec (OData key) |
| `create_spec` | `POST /api/Specs` | Create a new Spec |
| `update_spec` | `PUT /api/Specs/{key}` | Full update a Spec |
| `update_spec_by_odata_key` | `PUT /api/Specs({key})` | Full update (OData key) |
| `patch_spec` | `PATCH /api/Specs` | Partial update a Spec |
| `delete_spec` | `DELETE /api/Specs/{key}` | Delete a Spec |
| `delete_spec_by_odata_key` | `DELETE /api/Specs({key})` | Delete (OData key) |
| `get_specs_count` | `GET /api/Specs/$count` | Get Spec count |
| `request_selection_values` | `POST /api/Specs/RequestSelectionValues` | Get LOV/selection values |

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your Camstar server URL and auth credentials
```

### 3. Run the Server

```bash
python server.py
```

### 4. Test with MCP Inspector

```bash
mcp dev server.py
```

### 5. Install in Claude Desktop

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "camstar-modeling-specs": {
      "command": "python",
      "args": ["d:/Deepseek/camstar/CamstarModelingMCP/server.py"],
      "env": {
        "CAMSTAR_BASE_URL": "http://your-camstar-host/Modeling",
        "CAMSTAR_USERNAME": "CamstarAdmin",
        "CAMSTAR_PASSWORD": "Cam1star"
      }
    }
  }
}
```

## Response Truncation

When an API returns a large payload (> 4000 chars by default), the server automatically extracts only key fields:

- `instanceID`, `displayName`, `name`, `revision`
- `status`, `description`, `isFrozen`, `isRevOfRcd`
- `lastChangeDate`, `lastChangeDateGMT`, `creationDate`, `creationDateGMT`
- `currentStatus`, `control`, `eco`, `operation`

This threshold is configurable via the `MAX_RESPONSE_LENGTH` environment variable.

## Architecture

```
server.py          – FastMCP server with all @mcp.tool definitions
generate_token.py  – Dynamic Auth Token generator
.env.example       – Environment variable template
requirements.txt   – Python dependencies
Swagger/           – Original Swagger/OpenAPI spec
```

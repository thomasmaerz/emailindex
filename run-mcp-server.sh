#!/bin/bash
# Wrapper script to run MCP server
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
exec "$SCRIPT_DIR/.venv/bin/python3" -m mcp_server.server --stdio

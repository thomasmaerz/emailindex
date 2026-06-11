#!/bin/bash
# Wrapper script to run MCP server - avoids conda init overhead
export PYTHONPATH="/Users/thomasmaerz/emailindex"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
exec /Users/thomasmaerz/miniconda3/envs/emailindex/bin/python3 -m mcp_server.server --stdio

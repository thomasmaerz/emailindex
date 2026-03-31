#!/bin/bash
# Wrapper script to run MCP server - avoids conda init overhead
export PYTHONPATH="/Users/thomasmaerz/emailindex"
exec /Users/thomasmaerz/miniconda3/envs/emailindex/bin/python3 -m mcp_server.server --stdio

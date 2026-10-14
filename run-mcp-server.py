#!/usr/bin/env python3
import sys
import os
import time
import json

os.environ.setdefault("HF_HUB_OFFLINE", "1")

_DEBUG_LOG = os.environ.get("MCP_DEBUG_LOG", "")


def _debug(msg: str) -> None:
    if _DEBUG_LOG:
        with open(_DEBUG_LOG, 'a') as f:
            f.write(f'{msg} at {time.time()}\n')
            f.flush()


_debug('START')

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.environ['PYTHONPATH'] = _SCRIPT_DIR
sys.argv.append('--stdio')
sys.path.insert(0, _SCRIPT_DIR)

_debug('ABOUT TO IMPORT')

from mcp_server.server import MCPServer

_debug('IMPORT DONE')

server = MCPServer()

_debug('SERVER CREATED')

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue

    _debug(f'RECV: {line[:100]}')

    try:
        request = json.loads(line)
        response = server.handle_request(request)

        _debug(f'SEND: {str(response)[:100]}')

        if response is not None:
            print(json.dumps(response), flush=True)
    except json.JSONDecodeError as e:
        _debug(f'JSON ERROR: {e}')
        print(json.dumps({"error": "Invalid JSON"}), flush=True)
    except Exception as e:
        _debug(f'ERROR: {e}')
        print(json.dumps({"error": str(e)}), flush=True)

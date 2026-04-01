#!/usr/bin/env /Users/thomasmaerz/miniconda3/envs/emailindex/bin/python3
import sys
import os
import time
import json

with open('/tmp/mcp_start.log', 'a') as f:
    f.write(f'START at {time.time()}\n')
    f.write(f'argv: {sys.argv}\n')
    f.write(f'PYTHON: {sys.executable}\n')
    f.flush()

os.environ['PYTHONPATH'] = '/Users/thomasmaerz/emailindex'
sys.argv.append('--stdio')
sys.path.insert(0, '/Users/thomasmaerz/emailindex')

with open('/tmp/mcp_start.log', 'a') as f:
    f.write(f'ABOUT TO IMPORT at {time.time()}\n')
    f.flush()

from mcp_server.server import MCPServer

with open('/tmp/mcp_start.log', 'a') as f:
    f.write(f'IMPORT DONE at {time.time()}\n')
    f.flush()

server = MCPServer()

with open('/tmp/mcp_start.log', 'a') as f:
    f.write(f'SERVER CREATED at {time.time()}\n')
    f.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    
    with open('/tmp/mcp_start.log', 'a') as f:
        f.write(f'RECV: {line[:100]} at {time.time()}\n')
        f.flush()
    
    try:
        request = json.loads(line)
        response = server.handle_request(request)
        
        with open('/tmp/mcp_start.log', 'a') as f:
            f.write(f'SEND: {str(response)[:100]} at {time.time()}\n')
            f.flush()
        
        if response is not None:
            print(json.dumps(response), flush=True)
    except json.JSONDecodeError as e:
        with open('/tmp/mcp_start.log', 'a') as f:
            f.write(f'JSON ERROR: {e} at {time.time()}\n')
            f.flush()
        print(json.dumps({"error": "Invalid JSON"}), flush=True)
    except Exception as e:
        with open('/tmp/mcp_start.log', 'a') as f:
            f.write(f'ERROR: {e} at {time.time()}\n')
            f.flush()
        print(json.dumps({"error": str(e)}), flush=True)

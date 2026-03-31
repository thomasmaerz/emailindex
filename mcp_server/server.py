#!/usr/bin/env python3
"""
MCP Server for Email Intelligence System

Exposes email search and retrieval tools via Model Context Protocol.
"""

import sys
import json
from pathlib import Path

from .models import (
    EmailRecord, EmailSearchResult, ConversationThread,
    SearchParams, GetEmailParams, GetConversationParams, FindRecipientParams
)
from .database import search_emails, get_email, get_conversation, find_recipient_emails
from .config import Config


class MCPServer:
    def __init__(self):
        Config.ensure_directories()
        self.tools = {
            "search_emails": self.tool_search_emails,
            "get_email": self.tool_get_email,
            "get_conversation": self.tool_get_conversation,
            "find_recipient_emails": self.tool_find_recipient_emails,
        }
    
    def tool_search_emails(self, params: dict) -> list[dict]:
        try:
            search_params = SearchParams(**params)
        except Exception as e:
            return [{"error": str(e)}]
        
        results = search_emails(
            query=search_params.query,
            date_from=search_params.date_from,
            date_to=search_params.date_to,
            from_address=search_params.from_address,
            to_address=search_params.to_address,
            has_attachments=search_params.has_attachments,
            folder=search_params.folder,
            limit=search_params.limit,
            similar_to_email_id=search_params.similar_to_email_id
        )
        
        return [r.model_dump() for r in results]
    
    def tool_get_email(self, params: dict) -> dict:
        try:
            email_params = GetEmailParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        email = get_email(email_params.email_id)
        
        if email is None:
            return None
        
        result = email.model_dump()
        
        if result.get('raw_eml') and isinstance(result['raw_eml'], bytes):
            result['raw_eml'] = "[compressed]"
        
        return result
    
    def tool_get_conversation(self, params: dict) -> dict:
        try:
            conv_params = GetConversationParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        conversation = get_conversation(conv_params.thread_id)
        
        if conversation is None:
            return None
        
        result = conversation.model_dump()
        
        for email in result.get('emails', []):
            if email.get('raw_eml') and isinstance(email.get('raw_eml'), bytes):
                email['raw_eml'] = "[compressed]"
        
        return result
    
    def tool_find_recipient_emails(self, params: dict) -> list[dict]:
        try:
            recipient_params = FindRecipientParams(**params)
        except Exception as e:
            return [{"error": str(e)}]
        
        results = find_recipient_emails(
            recipient_params.email_address,
            recipient_params.limit
        )
        
        return [r.model_dump() for r in results]
    
    def handle_request(self, request: dict) -> dict:
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {}
                    },
                    "serverInfo": {
                        "name": "email-intelligence",
                        "version": "1.0.0"
                    }
                }
            }
        
        if method == "initialized":
            return {"jsonrpc": "2.0", "id": request_id, "result": None}
        
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "search_emails",
                            "description": "Search emails using full-text, vector similarity, or metadata filters",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Full-text or semantic search query"},
                                    "date_from": {"type": "string", "description": "Start date (ISO 8601 or YYYY-MM-DD)"},
                                    "date_to": {"type": "string", "description": "End date"},
                                    "from_address": {"type": "string", "description": "Filter by sender email"},
                                    "to_address": {"type": "string", "description": "Filter by recipient email"},
                                    "has_attachments": {"type": "boolean", "description": "Filter by attachment presence"},
                                    "folder": {"type": "string", "description": "Filter by Maildir folder"},
                                    "limit": {"type": "integer", "description": "Max results (1-1000)", "default": 20},
                                    "similar_to_email_id": {"type": "string", "description": "Find emails similar to this email ID"}
                                }
                            }
                        },
                        {
                            "name": "get_email",
                            "description": "Retrieve complete email by ID",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "email_id": {"type": "string", "description": "UUIDv4 of the email"}
                                },
                                "required": ["email_id"]
                            }
                        },
                        {
                            "name": "get_conversation",
                            "description": "Retrieve all emails in a thread",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "thread_id": {"type": "string", "description": "Thread ID from References header chain"}
                                },
                                "required": ["thread_id"]
                            }
                        },
                        {
                            "name": "find_recipient_emails",
                            "description": "Find all emails involving a specific email address",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "email_address": {"type": "string", "description": "Email address to search"},
                                    "limit": {"type": "integer", "description": "Max results (1-1000)", "default": 50}
                                },
                                "required": ["email_address"]
                            }
                        }
                    ]
                }
            }
        
        if method == "tools/call":
            tool_name = params.get("name")
            tool_params = params.get("arguments", {})
            
            if tool_name not in self.tools:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}
            
            try:
                result = self.tools[tool_name](tool_params)
                return {"jsonrpc": "2.0", "id": request_id, "result": result}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(e)}}
        
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}


def main():
    import sys
    server = MCPServer()
    
    if len(sys.argv) > 1 and sys.argv[1] == "--stdio":
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            
            try:
                request = json.loads(line)
                response = server.handle_request(request)
                print(json.dumps(response), flush=True)
            except json.JSONDecodeError:
                print(json.dumps({"error": "Invalid JSON"}), flush=True)
            except Exception as e:
                print(json.dumps({"error": str(e)}), flush=True)
    else:
        print("Email Intelligence MCP Server")
        print(f"Database: {Config.DB_PATH}")
        print(f"Attachments: {Config.ATTACHMENTS_DIR}")
        print("Starting in stdio mode...")
        
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            
            try:
                request = json.loads(line)
                response = server.handle_request(request)
                print(json.dumps(response), flush=True)
            except json.JSONDecodeError:
                print(json.dumps({"error": "Invalid JSON"}), flush=True)
            except Exception as e:
                print(json.dumps({"error": str(e)}), flush=True)


if __name__ == "__main__":
    main()

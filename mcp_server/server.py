#!/usr/bin/env python3
"""
MCP Server for Email Intelligence System

Exposes email search and retrieval tools via Model Context Protocol.
"""

import sys
import json
from pathlib import Path
import sqlite3

from .models import (
    EmailRecord, EmailSearchResult, ConversationThread,
    SearchParams, GetEmailParams, GetConversationParams, FindRecipientParams,
    QueryEmailParams, GetProjectContextParams, ListProjectsParams,
    MentionTimelineParams, ContactProfileParams, ThreadArcParams
)
from .database import (
    search_emails, get_email, get_conversation, find_recipient_emails,
    query_email_database, get_project_context, list_projects,
    get_mention_timeline, get_contact_profile, get_thread_arc, list_threads,
    initialize_embedding_model_async
)
from .config import Config


class MCPServer:
    def __init__(self):
        Config.ensure_directories()
        self._verify_schema()
        initialize_embedding_model_async()
        self.tools = {
            "query_email_database": self.tool_query_email_database,
            "get_project_context": self.tool_get_project_context,
            "get_email_by_id": self.tool_get_email_by_id,
            "get_thread_by_id": self.tool_get_thread_by_id,
            "list_projects": self.tool_list_projects,
            "get_mention_timeline": self.tool_get_mention_timeline,
            "get_contact_profile": self.tool_get_contact_profile,
            "get_thread_arc": self.tool_get_thread_arc,
            "list_threads": self.tool_list_threads,
        }
    
    def _verify_schema(self):
        """Verify database has required schema. Auto-migrate if needed, or fail fast."""
        conn = sqlite3.connect(str(Config.DB_PATH))
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA table_info(emails)")
        columns = {row[1] for row in cursor.fetchall()}
        
        required_v2_cols = {'sender', 'recipients', 'body_text', 'body_main_text', 'category_tags', 'project_tags', 'is_outbound'}
        missing = required_v2_cols - columns
        
        if missing:
            conn.close()
            print(f"ERROR: Missing required v2 columns: {missing}", file=sys.stderr)
            if missing == {'body_main_text'}:
                print("Run: python migrate_body_text.py", file=sys.stderr)
            else:
                print("Run: python migrate_v2.py", file=sys.stderr)
            sys.exit(1)
        
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='project_registry'")
        if not cursor.fetchone():
            print("INFO: project_registry table not found, creating empty", file=sys.stderr)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS project_registry (
                    name TEXT PRIMARY KEY,
                    aliases TEXT,
                    summary TEXT,
                    created_at TEXT
                )
            """)
            conn.commit()
        
        conn.close()
    
    def tool_query_email_database(self, params: dict) -> dict:
        try:
            query_params = QueryEmailParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        results = query_email_database(
            semantic_query=query_params.semantic_query,
            exact_keywords=query_params.exact_keywords,
            category_filter=query_params.category_filter,
            project_filter=query_params.project_filter,
            date_from=query_params.date_from,
            date_to=query_params.date_to,
            from_address=query_params.from_address,
            from_name=query_params.from_name,
            to_address=query_params.to_address,
            is_outbound=query_params.is_outbound,
            has_attachments=query_params.has_attachments,
            limit=query_params.limit,
            include_full_thread=query_params.include_full_thread,
            sort_by=query_params.sort_by,
            sort_order=query_params.sort_order,
            count_only=query_params.count_only,
            fields=query_params.fields,
            snippet_only=query_params.snippet_only,
            snippet_length=query_params.snippet_length,
            cursor=query_params.cursor if hasattr(query_params, 'cursor') else None
        )
        
        return results
    
    def tool_get_project_context(self, params: dict) -> dict | None:
        try:
            project_params = GetProjectContextParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_project_context(
            project_params.project_name,
            project_params.limit
        )
        
        if result is None:
            return None
        
        return result
    
    def tool_get_email_by_id(self, params: dict) -> dict | None:
        try:
            email_params = GetEmailParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_email(email_params.email_id)
        
        if result is None:
            return None
        
        return result.model_dump(mode='json')
    
    def tool_get_thread_by_id(self, params: dict) -> dict | None:
        try:
            thread_params = GetConversationParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_conversation(thread_params.thread_id)
        
        if result is None:
            return None
        
        return result.model_dump(mode='json')
    
    def tool_list_projects(self, params: dict) -> dict:
        try:
            projects_params = ListProjectsParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        results = list_projects(projects_params.limit)
        
        return {
            "projects": results,
            "count": len(results)
        }
    
    def tool_get_mention_timeline(self, params: dict) -> dict:
        try:
            timeline_params = MentionTimelineParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_mention_timeline(
            keyword=timeline_params.keyword,
            semantic_query=timeline_params.semantic_query,
            granularity=timeline_params.granularity,
            date_from=timeline_params.date_from,
            date_to=timeline_params.date_to,
            from_address=timeline_params.from_address,
            is_outbound=timeline_params.is_outbound,
        )
        
        return result
    
    def tool_get_contact_profile(self, params: dict) -> dict | None:
        try:
            contact_params = ContactProfileParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_contact_profile(
            name=contact_params.name,
            email_address=contact_params.email_address,
            limit=contact_params.limit,
            include_timeline=contact_params.include_timeline,
        )
        
        return result
    
    def tool_get_thread_arc(self, params: dict) -> dict | None:
        try:
            arc_params = ThreadArcParams(**params)
        except Exception as e:
            return {"error": str(e)}
        
        result = get_thread_arc(
            thread_id=arc_params.thread_id,
            mode=arc_params.mode,
            max_messages=arc_params.max_messages,
        )
        
        return result
    
    def tool_list_threads(self, params: dict) -> dict:
        sort_by = params.get("sort_by", "message_count")
        sort_order = params.get("sort_order", "desc")
        limit = params.get("limit", 10)
        
        result = list_threads(
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
        )
        
        return result
    
    def handle_request(self, request: dict) -> dict | None:
        method = request.get("method")
        params = request.get("params", {})
        request_id = request.get("id")
        
        # Handle notifications (no response needed)
        if method == "notifications/initialized":
            return None
        
        if method == "notifications/cancelled":
            return None
        
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
        
        if method == "prompts/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"prompts": []}
            }
        
        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "resources": [
                        {
                            "uri": "email://{email_id}/body",
                            "name": "Email Body",
                            "description": "Full body text of an email. Use email_id from any query result.",
                            "mimeType": "text/markdown"
                        }
                    ]
                }
            }
        
        if method == "resources/read":
            uri = params.get("uri", "")
            try:
                if "/body" in uri:
                    if not uri.startswith("email://"):
                        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Invalid email URI: {uri}"}}
                    path_part = uri.replace("email://", "")
                    if not path_part.endswith("/body"):
                        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Invalid email body URI: {uri}"}}
                    email_id = path_part.replace("/body", "")
                    email = get_email(email_id)
                    if email is None:
                        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": f"Email not found: {email_id}"}}
                    body_text = email.body_text or email.body_markdown or ""
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": {
                            "contents": [
                                {
                                    "uri": uri,
                                    "mimeType": "text/markdown",
                                    "text": body_text
                                }
                            ]
                        }
                    }
                else:
                    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unknown resource: {uri}"}}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": str(e)}}
        
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "tools": [
                        {
                            "name": "query_email_database",
                            "description": "Search emails by semantic meaning, literal keywords (FTS5), tags, dates, or participants. Pass both semantic_query and exact_keywords for recommended hybrid RRF search. Returns up to 50 ranked records with IDs, metadata, optional fields, snippets, or full threads. Use dedicated tools for timelines or profiles.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "semantic_query": {"type": "string", "description": "Meaning-based query. Combine with exact_keywords for recommended hybrid RRF search."},
                                    "exact_keywords": {"type": "string", "description": "Literal FTS5 query. Combine with semantic_query for recommended hybrid RRF search."},
                                    "category_filter": {"type": "string", "description": "Comma-separated categories"},
                                    "project_filter": {"type": "string", "description": "Comma-separated projects"},
                                    "date_from": {"type": "string", "description": "Start date ISO 8601"},
                                    "date_to": {"type": "string", "description": "End date ISO 8601"},
                                    "from_address": {"type": "string", "description": "Filter by sender"},
                                    "to_address": {"type": "string", "description": "Filter by recipient"},
                                    "is_outbound": {"type": "boolean", "description": "Filter by direction"},
                                    "has_attachments": {"type": "boolean", "description": "Filter by attachments"},
                                    "limit": {"type": "integer", "description": "Max results (1-50)", "default": 10},
                                    "include_full_thread": {"type": "boolean", "description": "Return full thread", "default": False},
                                    "from_name": {"type": "string", "description": "Filter by sender display name (LIKE match)"},
                                    "sort_by": {"type": "string", "enum": ["timestamp", "relevance"], "description": "Sort field"},
                                    "sort_order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order"},
                                    "count_only": {"type": "boolean", "description": "Return only count, no results", "default": False},
                                    "fields": {"type": "array", "items": {"type": "string"}, "description": "Specific fields to return"},
                                    "snippet_only": {"type": "boolean", "description": "Return FTS5 snippet instead of full body", "default": False},
                                    "snippet_length": {"type": "integer", "description": "FTS5 snippet token window size", "default": 32},
                                    "cursor": {"type": "string", "description": "Opaque pagination cursor from previous response"}
                                }
                            }
                        },
                        {
                            "name": "get_project_context",
                            "description": "Retrieve a registered project's metadata and its latest tagged emails. Returns a project object plus up to 50 email summaries with snippets. Use after list_projects or when a project name is known; use query_email_database for ad hoc searches or filters not tied to one registered project.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "project_name": {"type": "string", "description": "Project name or alias"},
                                    "limit": {"type": "integer", "description": "Max emails to return (1-50)", "default": 10}
                                },
                                "required": ["project_name"]
                            }
                        },
                        {
                            "name": "get_email_by_id",
                            "description": "Retrieve one complete email by its UUID. Returns one full email record, including body and metadata, or no result if absent. Use after any search result when you need one message's full content; use get_thread_by_id or get_thread_arc when conversation context is needed.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "email_id": {"type": "string", "description": "UUIDv4 of the email"}
                                },
                                "required": ["email_id"]
                            }
                        },
                        {
                            "name": "get_thread_by_id",
                            "description": "Retrieve every email in a conversation thread by thread ID. Returns one full conversation object with all message records and metadata. Use when complete thread content is needed; use get_thread_arc for a bounded summary or get_email_by_id for a single message.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "thread_id": {"type": "string", "description": "Thread ID (format: thread-*)"}
                                },
                                "required": ["thread_id"]
                            }
                        },
                        {
                            "name": "list_projects",
                            "description": "List registered projects and their aliases. Returns up to 50 project summaries, including name, aliases, summary, and creation date. Use to discover project names before project_filter or get_project_context; use query_email_database when searching email content directly.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "limit": {"type": "integer", "description": "Max projects to return (1-50)", "default": 20}
                                }
                            }
                        },
                        {
                            "name": "get_mention_timeline",
                            "description": "Count literal keyword mentions over time with optional sender, date, and direction filters. Returns total matches, first and last occurrence, and counts grouped by year, month, or quarter; it does not return emails. Use for trend analysis; use query_email_database to retrieve matching messages.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "keyword": {"type": "string", "description": "Exact keyword or name to search"},
                                    "granularity": {"type": "string", "enum": ["year", "month", "quarter"], "description": "Grouping granularity", "default": "year"},
                                    "date_from": {"type": "string", "description": "Start date ISO 8601"},
                                    "date_to": {"type": "string", "description": "End date ISO 8601"},
                                    "from_address": {"type": "string", "description": "Filter by sender"},
                                    "is_outbound": {"type": "boolean", "description": "Filter by direction"}
                                },
                                "required": ["keyword"]
                            }
                        },
                        {
                            "name": "get_contact_profile",
                            "description": "Summarize correspondence with a person matched by name or email address. Returns one contact profile with known addresses, interaction dates, optional yearly timeline, and up to 50 recent sample emails. Use for person-centric context; use query_email_database for topic searches across people.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string", "description": "Fuzzy match on from_name"},
                                    "email_address": {"type": "string", "description": "Exact or partial match on from_address"},
                                    "limit": {"type": "integer", "description": "Representative emails to return (1-50)", "default": 10},
                                    "include_timeline": {"type": "boolean", "description": "Include mention timeline", "default": True}
                                }
                            }
                        },
                        {
                            "name": "get_thread_arc",
                            "description": "Summarize or inspect a conversation thread with participants and message order. Returns one thread arc with up to 50 messages, using short snippets in summary mode or bodies in full mode. Use for a bounded thread overview; use get_thread_by_id for every message in the thread.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "thread_id": {"type": "string", "description": "Thread ID from query result"},
                                    "mode": {"type": "string", "enum": ["summary", "full"], "description": "summary or full", "default": "summary"},
                                    "max_messages": {"type": "integer", "description": "Max messages to return (1-50)", "default": 20}
                                },
                                "required": ["thread_id"]
                            }
                        },
                        {
                            "name": "list_threads",
                            "description": "Browse conversation threads ranked by size, participant count, or activity dates. Returns up to 50 thread summaries with counts, activity range, and latest-email metadata. Use to discover notable threads without a content query; use query_email_database for email-level search and get_thread_arc or get_thread_by_id after choosing a thread.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "sort_by": {"type": "string", "enum": ["message_count", "participant_count", "last_activity", "first_activity"], "description": "Sort field", "default": "message_count"},
                                    "sort_order": {"type": "string", "enum": ["asc", "desc"], "description": "Sort order", "default": "desc"},
                                    "limit": {"type": "integer", "description": "Max threads to return (1-50)", "default": 10}
                                }
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
                
                # Wrap result in MCP CallToolResult format
                mcp_result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(result, indent=2)
                        }
                    ]
                }
                
                return {"jsonrpc": "2.0", "id": request_id, "result": mcp_result}
            except Exception as e:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"Error: {str(e)}"}], "isError": True}}
        
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
                if response is not None:
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
                if response is not None:
                    print(json.dumps(response), flush=True)
            except json.JSONDecodeError:
                print(json.dumps({"error": "Invalid JSON"}), flush=True)
            except Exception as e:
                print(json.dumps({"error": str(e)}), flush=True)


if __name__ == "__main__":
    main()

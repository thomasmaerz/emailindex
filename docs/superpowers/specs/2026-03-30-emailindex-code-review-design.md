# Emailindex Code Review - Design Document

**Version:** 1.0.0  
**Created:** 2026-03-30  
**Status:** Approved for Execution

---

## 1. Overview

This document outlines the structure for a parallel code review of the `emailindex` project using four specialized subagents, each focusing on a specific review dimension (Functionality, Security, Quality, Architecture).

---

## 2. Project Context

**Location:** `/Users/thomasmaerz/emailindex/`

**Key Files:**
- `ingest.py` (776 lines) - Main ingestion pipeline for parsing Maildir emails, generating embeddings, and storing in SQLite
- `mcp_server/server.py` (220 lines) - MCP server exposing email search and retrieval tools
- `mcp_server/database.py` (242 lines) - Database layer with FTS and vector search
- `mcp_server/models.py` (153 lines) - Pydantic models for validation
- `mcp_server/config.py` (30 lines) - Configuration constants

---

## 3. Review Structure: Vertical Slices (By Dimension)

Each subagent will review the entire codebase from their specialized perspective:

### 3.1 Functionality Reviewer
- Verify RFC 822 email parsing correctness
- Validate resumable batching logic
- Check MCP tool accuracy (hybrid FTS + vector search)
- Test edge cases in encoding handling

### 3.2 Security Reviewer
- Identify SQL injection vulnerabilities
- Check for path traversal in attachment storage
- Verify sensitive data handling
- Audit logging for data leaks

### 3.3 Quality Reviewer
- Evaluate code style consistency
- Check type-safety (Pydantic strict mode)
- Identify documentation gaps
- Assess test coverage

### 3.4 Architecture Reviewer
- Critique SQLite + sqlite-vec storage strategy
- Evaluate attachment deduplication design
- Assess scalability of the ingestion pipeline
- Review component boundaries and separation of concerns

---

## 4. Execution Plan

1. Dispatch 4 subagents in parallel, each with their specific review scope
2. Each subagent returns findings with severity levels (Critical, Important, Minor)
3. Synthesize results into a unified report
4. Present findings to user with recommendations

---

## 5. Expected Output Format

Each subagent will return:
- **Strengths**: What works well
- **Issues**: Categorized by severity with file:line references
- **Recommendations**: Actionable fixes
- **Assessment**: Overall readiness (Ready / Needs Work / Requires Overhaul)

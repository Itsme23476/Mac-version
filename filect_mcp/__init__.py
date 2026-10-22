"""Filect MCP server (EXPERIMENTAL) — isolated from the app.

This package ONLY imports from `app.core.*` and calls the same functions the app uses. It
never edits app code and is not part of the shipped build. Runs as its own process.
"""

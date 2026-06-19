"""Ling-Ling TUI — a read-only/file-drop companion cockpit.

This package is a SEPARATE process from the daemon. It must never import
RAGManager / LLMClient or open ChromaDB (single-writer). It only:
  • composes @ling-* command files and drops them into toLingLing/
  • reads status read-only (llm_trace.sqlite mode=ro, .kb_lock, *_state.json)
"""

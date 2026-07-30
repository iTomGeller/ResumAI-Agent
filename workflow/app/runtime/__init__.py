"""Conversational agent runtime: coordinator-driven multi-agent execution.

Java owns conversations, queueing, permits and authoritative run status.
This package executes exactly one run at a time per request: dynamic agent
selection, budgeted LLM/tool loops, shared blackboard state, layered memory
access, context compaction and full event trajectories.
"""

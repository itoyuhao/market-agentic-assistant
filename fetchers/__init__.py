"""Data fetchers for the market digest agent.

Each module is independent and returns structured Python objects (TypedDict /
dataclass / dict). Keeps I/O separate from the agent layer so modules can be
tested in isolation.
"""

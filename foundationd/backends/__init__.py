"""Backends: code that reads or changes the system. Workers call these; the broker never does.

Each takes what it needs as arguments (paths, a command runner) so tests can use fakes.
"""

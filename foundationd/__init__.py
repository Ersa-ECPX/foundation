"""foundationd: the Ersa Compute Platform management service (D-Bus name org.ersa.Foundation1).

The design is in docs/foundationd.md. This package holds the broker; the modules that do not need
D-Bus (schema, config, state) are importable and testable on their own.
"""

__version__ = "0.4.0"

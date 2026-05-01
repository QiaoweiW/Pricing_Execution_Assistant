"""External data-source connectors.

Each module in this package exposes a single fetch_*() function that returns
an in-memory pandas DataFrame plus a SnapshotMeta dataclass.  Page modules
under pages/ consume these connectors instead of reading files directly so
that the I/O boundary is testable, swappable, and cacheable in isolation.
"""

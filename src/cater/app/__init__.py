"""Local application layer for CATER.

The business conversion engine lives in :mod:`cater.core` and is imported
lazily through the ``process_job`` contract. This package owns persistence,
mail transport, polling, delivery state and the local administration UI.
"""

__version__ = "0.1.0"
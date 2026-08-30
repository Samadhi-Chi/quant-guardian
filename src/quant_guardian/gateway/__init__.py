"""Isolated messaging gateway for Quant Guardian.

The gateway intentionally exposes only a fixed command vocabulary.  It has no
shell, strategy, order, cancellation, or arbitrary process-control capability.
"""

from quant_guardian.gateway.config import MessagingConfig

__all__ = ["MessagingConfig"]

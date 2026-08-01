"""Logging bootstrap for AliOS processes."""

import logging


def configure_logging(level: str) -> None:
    """Configure safe process logging once at startup."""
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

"""Consistent console/file logging with optional simulation context."""

import logging
from pathlib import Path


def configure_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    """Configure only this package's logger; safe to call repeatedly."""
    logger = logging.getLogger("thermal_home_modeling")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(simulation_id)s] %(message)s", defaults={"simulation_id": "-"})
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)

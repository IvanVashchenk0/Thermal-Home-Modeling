"""Small validation helpers shared by configuration and dataclasses."""

import math
import re


def finite_number(name: str, value: object) -> float:
    """Return a finite number, rejecting booleans and numeric strings."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def nonempty_text(name: str, value: object) -> str:
    """Validate and trim a nonempty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def validate_simulation_id(value: str) -> None:
    """Keep identifiers portable and safe to use as directory names."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}", value):
        raise ValueError("simulation_id must contain 1–120 letters, digits, underscores, or hyphens")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise ValueError("simulation_id is a reserved filename")

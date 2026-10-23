"""Synthetic subprocess fixture, not an EnergyPlus replacement or emulator."""

import argparse
from pathlib import Path

from thermal_home_modeling.smoke.fixtures import write_synthetic_sql


def main() -> int:
    """Accept the runner's CLI arguments and write clearly artificial outputs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annual", action="store_true")
    parser.add_argument("--weather", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("model", type=Path)
    args = parser.parse_args()
    output = args.output_directory
    output.mkdir(parents=True, exist_ok=True)
    write_synthetic_sql(output / "eplusout.sql")
    (output / "eplusout.end").write_text("EnergyPlus Completed Successfully (SYNTHETIC FIXTURE)\n", encoding="utf-8")
    (output / "eplusout.err").write_text("SYNTHETIC SMOKE FIXTURE - NO PHYSICS WAS SIMULATED\n", encoding="utf-8")
    print("SYNTHETIC SMOKE FIXTURE: emitting fixed test data; no EnergyPlus was executed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

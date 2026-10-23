# Real batch verification — October 9, 2026

Historical commercial demonstration validation record. The current Streamlit
interface shows residential results only; commercial execution remains available
through the CLI.

Engine: installed EnergyPlus `26.2.0-4bd7a1f26f`.
Baseline: installed `5ZoneAirCooled.idf`, converted using the existing pipeline.
Machine reported 8 logical CPUs; the parallel batches used 4 workers.
All 12 requested validation cases were new executions (`--rerun`); none used cache.

| Test | Configuration set | Workers | New successes | Wall time | Sum of process runtimes | Mean / median process runtime | New successful cases/min |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A — Sequential | Chicago × 0°, 90°, 180°, 270° | 1 | 4 | 14.46 s | 13.92 s | 3.48 / 3.49 s | 16.60 |
| B — Parallel | Chicago × 0°, 90°, 180°, 270° | 4 | 4 | 4.23 s | 15.77 s | 3.94 / 3.93 s | 56.77 |
| C — Multi-city parallel | Chicago, San Francisco × 0°, 90° | 4 | 4 | 5.14 s | 19.48 s | 4.87 / 4.88 s | 46.66 |

Observed speedup for the equivalent A/B pair: **3.42×**, calculated from full
precision wall times `14.458752750011627 / 4.227279459009878`. This is one measured
pair on this machine. It is not a general scaling claim. Per-process times can
increase under contention even while the batch finishes sooner.

Raw execution timestamps show a maximum of one overlapping process in A and
four overlapping processes in each of B and C. The repeated Chicago configurations
returned identical full-precision energy metrics in A and B.

## Batch artifacts

Directories are relative to the repository; per-case input/output locations are
referenced in each batch's manifest, CSV, and Parquet.

| Test | Directory |
| --- | --- |
| A | `../output/batches/batch_1d392a6c440246cbb4b754b17b35ada1/` |
| B | `../output/batches/batch_823f7fbaf3cd426d86f48444f983b172/` |
| C | `../output/batches/batch_2acf6c4c868444868decbbef438502f3/` |
| Cache verification | `../output/batches/batch_d294f6ddf7694533a1eb73eb22fa6f48/` |

The extra cache verification selected Chicago's four orientations and reused all
four results: **zero new processes**, zero new process-runtime sum, and no claimed
speedup. It created only a new batch aggregation, preserving existing raw runs.

## Energy results

These are measured annual purchased-energy values, not delivered thermal loads.
Gas is expressed in kWh-equivalent of fuel. Values below are rounded for display;
the stored datasets retain full precision. Heating electricity is reported as
zero; whole-building natural gas equals heating natural gas in these cases.

| City | Orientation | Heating natural gas (kWh-eq/year) | Cooling electricity (kWh/year) | Whole-building electricity (kWh/year) |
| --- | ---: | ---: | ---: | ---: |
| Chicago | 0° | 19,131.4 | 4,567.3 | 43,552.8 |
| Chicago | 90° | 19,001.5 | 4,923.1 | 44,326.7 |
| Chicago | 180° | 18,791.7 | 4,945.1 | 44,188.3 |
| Chicago | 270° | 19,059.1 | 4,951.8 | 44,245.5 |
| San Francisco | 0° | 8,106.5 | 3,850.2 | 42,727.7 |
| San Francisco | 90° | 7,991.2 | 4,273.9 | 43,547.8 |

San Francisco 90° is compared to San Francisco 0°: cooling +11.0%, heating gas
−1.4%, and whole-building electricity +1.9%. Chicago 90° relative to its own
0° baseline shows +7.8%, −0.7%, and +1.8%, respectively.

## Diagnostics and provenance

Every `.end` confirms successful completion, and every `.err` was inspected.
There were **no Severe/Fatal errors** and **one warning per case**: the example's
central heating airflow ratio of 0.30 is below the calculated ratio, so the
autosized heating coil may be undersized. Complete warning text is preserved.
The original model and its HVAC sizing assumptions were not altered.

Checks verify unique per-case paths, correct EPW command arguments, matching
generated `Site:Location`, correct north axes, source/weather hashes, successful
annual SQLite extraction, and agreement among SQLite, JSON, CSV, and Parquet.
Streamlit renders the saved real outcomes and calculates within-city comparisons
and the measured A/B speedup from those records.

## Repeat artifact checks without launching EnergyPlus

The complete suite passed **130 tests** with both the earlier single-run and new
batch artifact environment variables supplied. The synthetic smoke workflow also
passed. Without the opt-in artifact paths, four real-output checks are skipped;
normal tests do not launch the installed EnergyPlus engine.

```sh
THM_REAL_BATCH_SEQUENTIAL="../output/batches/batch_1d392a6c440246cbb4b754b17b35ada1" \
THM_REAL_BATCH_PARALLEL="../output/batches/batch_823f7fbaf3cd426d86f48444f983b172" \
THM_REAL_BATCH_MULTICITY="../output/batches/batch_2acf6c4c868444868decbbef438502f3" \
.venv/bin/python -m pytest -q tests/test_real_mvp_batches.py
```

The normal test suite also covers deduplication, sequential execution, simultaneous
synthetic subprocesses, CPU caps, baseline preparation before workers, single-thread
aggregation, isolated failures, cache accounting, exports, and a 3-city ×
4-orientation Streamlit submission with rerun protection. Synthetic test outputs
are confined to test fixtures and are not used for the real timings above.

## Files changed for this feature

Created:

- `thermal_home_modeling/experiments/mvp_batch.py`
- `thermal_home_modeling/presentation/batch_results.py`
- `thermal_home_modeling/presentation/streamlit_batch.py`
- `tests/test_mvp_batch.py`
- `tests/test_real_mvp_batches.py`
- `docs/real_batch_validation.md`

Modified:

- `streamlit_app.py` — add batch UI while preserving single-run controls.
- `thermal_home_modeling/cli.py` — add `mvp-batch` and benchmark comparison options.
- `thermal_home_modeling/experiments/mvp_real.py` — accept a preassigned case ID for tracing failures.
- `thermal_home_modeling/storage/result_store.py` — reuse atomic dataframe export with a selectable filename stem.
- `tests/test_real_orientation_results.py` — allow a newer validated cache record with identical inputs and metrics after forced reruns.
- `README.md` — usage, architecture, storage, timing, and limitations.

The annual example's original Chicago HVAC design days are retained for every
city. These outputs establish software correctness and local execution capability;
a validated residential baseline and climate-specific sizing remain future work.

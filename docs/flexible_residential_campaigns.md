# Flexible residential campaigns — October 16, 2026

Historical validation record. The current Streamlit application is a read-only
residential results viewer. Configure, submit, stop, and resume campaigns through
the CLI commands below.

The residential workflow now supports seeded fixed-count and full-factorial experiments from one shared generator, with persistent local workers and raw-results browsing. All six workbook cities are executable. **The supported space is 1,728 unique, symmetry-distinct models.** A 1,000-case request is valid; a 5,000-case request is rejected. No 1,000-case EnergyPlus campaign was launched during development.

## Supported physical dimensions

| Input | Implemented selections |
|---|---|
| Cities / matching weather | Phoenix, Memphis, Washington DC, Boston, Minneapolis, Grand Forks |
| Houses | 1S: 1,500 ft²; 1M: 2,000 ft²; 1L: 2,500 ft²; all one story |
| Walls | W3, W5, W6, W8, W9, W10 |
| Attic insulation, Phoenix/Memphis | R-38, R-49, R-60, R-80 |
| Attic insulation, remaining cities | R-49, R-60, R-70, R-80 |
| Orientation | 0°, 45°, 90°, 135°; opposing rotations deduplicated after a physical symmetry check |

Count: 6 cities × 3 houses × 6 walls × 4 city-eligible attic levels × 4 distinct orientations = **1,728**. The global cross product contains 2,160 entries because there are five attic levels across cities; 432 ineligible city/attic combinations are excluded before sampling. All six active walls are eligible in all six cities under workbook study rules. W5 eligibility retains the workbook tolerance; this is not a new local-code certification.

Additional houses use the workbook floor areas with the existing provisional 5:3 rectangular aspect ratio and 8 ft height. Length = √(area × 5/3), width = √(area ÷ (5/3)). Walls, roof, floor, zone volume, windows and floor-area-based gains change physically. These are **provisional V0 templates**, not finalized workbook footprints. Added wall mappings use the full-precision workbook material definitions, existing construction mapping checks, and reviewed hashes. R-38/R-60/R-70 use the same nominal insulation SI conversion as R-49/R-80. Materials and operating assumptions remain provisional.

Inactive: two-story 2S/2M/2L, remaining wall assemblies, independently variable WWR/glazing/Low-E/frame/divider choices, city-specific foundations/basements, HVAC equipment/fuels/efficiencies, pricing/carbon and analysis. No arbitrary material or operating-parameter ranges were invented to inflate the count.

## All six city weather bindings

| Workbook city | Station | Source |
|---|---|---|
| Phoenix | Phoenix-Sky Harbor International, 722780 | Downloaded TMY3 |
| Memphis | Memphis International, 723340 | Downloaded TMY3 |
| Washington DC | Dulles, 724030, Virginia | Existing installed TMY3 |
| Boston | Boston-Logan International, 725090 | Downloaded TMY3 |
| Minneapolis | Minneapolis–St Paul International, 726580 | Downloaded TMY3 |
| Grand Forks | Grand Forks International, 727570 | Downloaded TMY3 |

The five additional files came from the [Climate.OneBuilding US weather catalog](https://climate.onebuilding.org/WMO_Region_4_North_and_Central_America/USA_United_States_of_America/index.html). Exact archive URLs, retrieval date, EPW/archive SHA256, station names, state, country and coordinates are retained in `catalogs/residential_mappings.json`. These are airport/metro-area weather stations; DC Dulles is in Virginia. No city uses another city's file.

Archives and extracted files are in `data/weather/research/`. Files are validated by station binding when models are generated, and their actual content hashes enter simulation identity. `weather-fetch` restores missing downloads and verifies pinned hashes; it will not silently overwrite an existing altered file. DC still requires the installed Dulles EPW.

## Run 50, 100 or 1,000 cases

From the repository:

```sh
.venv/bin/python main.py weather-fetch

# Validate exact counts without launching any simulations.
.venv/bin/python main.py residential-batch --count 1000 --seed 42 --fresh --dry-run

# Detached workers: safe to close the browser or terminal after submission.
.venv/bin/python main.py residential-batch --count 50 --workers 4 --seed 42 --fresh --background
.venv/bin/python main.py residential-batch --count 100 --workers 4 --seed 42 --fresh --background
.venv/bin/python main.py residential-batch --count 1000 --workers 8 --seed 42 --fresh --background

# Balanced sampling and a saved configuration.
.venv/bin/python main.py residential-batch --config examples/residential_all_cities.json --count 1000 --sampling balanced --seed 42 --workers 8 --fresh --background

# Full supported factorial, only when intentionally requested.
.venv/bin/python main.py residential-batch --config examples/residential_all_cities.json --sampling full_factorial --workers 8 --background
```

`--fresh` and `--rerun` disable cache reuse. Omit them to reuse individually verified successful results. Without `--background`, the CLI runs in the foreground and preserves checkpoints on interruption. Streamlit always submits detached jobs. Workers are CPU-capped; supported requests are 1, 2, 4 or 8. `--workers 1` defaults to sequential; the others default to parallel. `--mode sequential` explicitly selects serial execution.

The old `residential` single-case command still defaults to DC/1S/W3/R49/0°. The old commercial commands and controls are preserved. `residential-batch` now defaults to the full supported selection, so use `--count` or `--config` to bound a campaign. Merely opening Streamlit, importing a workbook or generating a preview never starts simulations.

## JSON experiment specification

`examples/residential_all_cities.json` is the full supported space. `examples/residential_validation_12.json` is the small balanced validation experiment. Example custom selection:

```json
{
  "schema_version": 1,
  "parameters": {
    "city_id": ["washington_dc", "boston", "phoenix"],
    "house_id": ["1S", "1M"],
    "wall_id": ["W3", "W5", "W9"],
    "attic_r_ip": [49, 60, 80]
  },
  "fixed": {"orientation_deg": 45},
  "sampling": "balanced",
  "count": 40,
  "seed": 42,
  "constraints": {
    "exclude": [{"city_id": "phoenix", "attic_r_ip": 80}]
  }
}
```

Only implemented dimensions are accepted. A dimension cannot be both fixed and varying. An omitted dimension uses DC/1S/W3/R49/0° baseline values. Every exclusion is an AND of field/value matches; the exclusion list is an OR. Eligibility rules always apply in addition to user exclusions. No Python expressions are evaluated. Optional `weather_files` maps city IDs to explicit EPW paths; station validation still applies. Relative paths are interpreted from the command's working directory.

`--cities`, `--house`, `--walls`, `--attic-levels`, `--orientations` accept comma-separated values and override the corresponding JSON dimension. `--count`, `--sampling`, `--seed` override sampling. `--save-config experiment.json` saves the resolved specification. A count supplied against a full-factorial config selects random sampling unless balanced sampling is explicitly requested. Full factorial and an explicit count are mutually exclusive.

Random sampling is without replacement with a local seeded RNG. Balanced sampling greedily minimizes marginal imbalance, with seeded tie-breaking; constraints may prevent exact balance. The achieved counts are stored, not merely advertised as balanced. Generation order is stable. Selection is made after validity filtering and physical deduplication. Material/construction labels do not make identical physics unique. 180° equivalence is used only after checking geometry, windows, envelope assignments, zoning and shading; asymmetric models retain opposing orientations. There is no blind 90° reduction.

## Same cases with 1, 4 or 8 workers

Generate the experiment once, then use the printed batch directory to reuse its **exact saved cases and catalog/assumption snapshots**. `--fresh` makes these new executions, suitable for timing comparisons:

```sh
.venv/bin/python main.py residential-batch --config examples/residential_validation_12.json --workers 1 --fresh

# Replace FIRST_BATCH with the directory printed by that command.
.venv/bin/python main.py residential-batch --resume-batch FIRST_BATCH --workers 4 --fresh --compare-batch FIRST_BATCH/batch_summary.json
.venv/bin/python main.py residential-batch --resume-batch FIRST_BATCH --workers 8 --fresh --compare-batch FIRST_BATCH/batch_summary.json
```

Timing comparison requires identical effective configuration sets, zero cache hits, all successful runs and actual process runtimes. It rejects non-equivalent work. Summaries distinguish batch wall time, summed process runtimes, mean/median process runtime, newly successful simulations/minute and all completed configurations/minute. Cached runs contribute no new process runtime or simulated throughput; their original runtime remains separately indexed.

## Persistent execution, interruption and resume

The CLI prints `../output/jobs/job_<UUID>/`. Each job contains the exact submitted cases, expected signatures, immutable catalog/mapping/assumption snapshots, `request.json`, `status.json`, `worker.log` and a kernel-protected worker lock. The worker uses `start_new_session=True`, closed inherited descriptors and file logs. It does not depend on a Streamlit session or browser connection. This is a local macOS/Linux process mechanism, not a distributed queue; it does not survive a machine reboot, but its saved job can be resumed afterward.

```sh
.venv/bin/python main.py residential-batch --job-status JOB_DIRECTORY
.venv/bin/python main.py residential-batch --stop-job JOB_DIRECTORY
.venv/bin/python main.py residential-batch --resume-job JOB_DIRECTORY --workers 4
.venv/bin/python main.py residential-batch --resume-batch BATCH_DIRECTORY --workers 4 --background
```

A stop ceases scheduling and lets active workflows preserve artifacts. Resume creates a new attempt, preserving the original. Completed successful cases are revalidated and reused; failed/missing cases rerun. A job interrupted before its batch manifest exists can be recovered from its submission snapshots. A second worker cannot acquire the same job lock. Engine/weather/model changes after submission are detected before execution rather than silently replacing requested inputs.

Execution still uses the existing shared coordinator, `EnergyPlusRunner`, SQLite parser, isolated raw directories and CSV/Parquet exports. The coordinator writes a small durable case record after each completion; larger shared exports are checkpointed every two seconds or 25 completions and at completion/interruption. Interrupted active cases also retain their individual result records for cache recovery. Cache metadata is indexed once per planning pass; only matching candidate SQL files are opened, one at a time. Plans and compact indexes are kept in memory; complete raw SQL datasets are not loaded together.

## Streamlit results viewer

Run `.venv/bin/streamlit run streamlit_app.py`, then select **Residential simulation — V0**.

The current viewer reads saved residential batches without launching EnergyPlus.
It offers campaign and configuration filters, a compact results table, annual load
chart, selected run provenance, warnings, batch summaries, and downloads. Use the
CLI commands above for all execution and worker management.

## Actual verification

Baseline before these changes: **149 passed, 5 skipped**. The regression suite retains the existing commercial and residential checks; the former attic-unsupported test now uses R38 in DC because R60 is implemented. The UI launch-once test now verifies a single persistent submission. New tests exercise 1,728-case enumeration, 50/100/1,000 seeded selection, balanced marginals, exclusions, unsupported inputs, label-independent model identity, numeric and symmetry deduplication, asymmetric geometry, real house/window scaling, CLI/UI consistency, dry runs, 1,000-case submission snapshots without executing them, locks, stop/resume, changed-input rejection, timing-comparison guards and weather hashes.

**36 new real EnergyPlus simulations were executed in this task**, in three 12-case batches; every batch succeeded 12/12. Each set covers all six cities, all three house sizes, all six active walls, all five globally available attic levels and all four distinct orientations. The first detached batch was an initial validation before numeric case representation was normalized. The subsequent sequential and parallel batches have identical effective signatures and exactly matching raw heating/cooling outputs. A detached resume reused 12/12 with zero new runs. No large real campaign was executed.

| Batch | Workers | New/cached | Wall s | Process runtime sum s | Mean s | Median s | New successes/min |
|---|---:|---|---:|---:|---:|---:|---:|
| Initial detached validation | 4 | 12/0 | 4.5475 | 16.1019 | 1.3418 | 1.1460 | 158.33 |
| Saved-case sequential benchmark | 1 | 12/0 | 17.2408 | 16.1170 | 1.3431 | .8363 | 41.76 |
| Same-case parallel benchmark | 4 | 12/0 | 8.9362 | 33.7052 | 2.8088 | 3.0408 | 80.57 |
| Detached cached resume | 4 | 0/12 | .5413 | 0 | null | null | 0 |

These are observed local timings under concurrent development/test load, not controlled hardware benchmarks or forecasts for 1,000 runs. The saved-case comparison reports a wall-time ratio of 1.9293; increased per-process runtime under concurrency is retained honestly. Execution timestamps verified actual peak concurrency1 and4.

Artifact directories, relative to the repository:

- Initial detached job: `../output/jobs/job_52fdf351263e4a0dbb9543cd55c0f464/`.
- Initial validation batch: `../output/batches/residential_9faf7e2c4253409dae238d32babd7cd1/`.
- Sequential benchmark: `../output/batches/residential_3c5070efa554443a9cdf8a7f5d37d1d6/`.
- Parallel benchmark: `../output/batches/residential_8f637ff4880b4160aa128fe2fa728121/`, including `timing_comparison.json`.
- Cached resume job: `../output/jobs/job_af8f0d98713f4ab89801d8d5f14efcce/`.
- Cached resume batch: `../output/batches/residential_9b532c6766d64175a455db9704fec43c/`.
- Generated inputs: `../input/generated/res_<UUID>/`.
- Raw engine files: `../output/res_<UUID>/energyplus/`; exact per-run paths are in each batch index.

All campaign folders contain the compact index, manifest, timing summary and snapshots. Original artifacts from the earlier eight-case pilot and all commercial campaigns remain intact.

## Remaining issues and engineering limits

No Severe/Fatal errors or failed process exits occurred in the 36 new runs. Phoenix and Grand Forks validation cases had no warning blocks. DC, Memphis, Boston and Minneapolis report the existing psychrometric out-of-range warning; Boston's recurring summary reports four occurrences. The index preserves two text blocks per affected case (initial warning plus recurring summary), totaling16 blocks per 12-case batch. A block count is not an occurrence count. Underlying warning causes remain unresolved.

The fixed **18°C ground-contact slab** remains a strong modeling limitation in every climate. In this validation set, cooling outputs are zero or numerical residuals; the pipeline preserves the actual values. These results are not calibrated residential performance predictions. No city-specific basement/foundation model was added. Workbook material values, OSB/foam order, glazing, gains and schedules still need engineering validation. The extension verifies physical input application and executable software coverage, not final research validity. See the earlier [V0 engineering report](residential_v0_engineering_report.md) for unchanged physical assumptions.

No finance, carbon, regression, optimization, savings or derived building-performance analysis was added.

Final validation: **184 passed, 1 skipped in 70.63 seconds**, with saved commercial integration artifacts enabled. The skipped test is the separate opt-in original eight-case residential simulator test; this task instead executed and directly audited the 36 real multi-city runs described above. Without optional artifact environment variables, the default suite has 180 passing checks and five opt-in skips. A live Streamlit AppTest using the installed engine, actual six-city EPWs and saved results produced no errors/exceptions and correctly displayed 1,728 available / 100 requested / 87 new / 13 cached. Local server health returned `ok`.

Machine-readable evidence: `catalogs/campaign_validation.json`. Shared generator: `experiments/residential_design.py`; persistent workers: `experiments/campaign_jobs.py`; weather restoration: `weather/research.py` (all under `thermal_home_modeling/`).

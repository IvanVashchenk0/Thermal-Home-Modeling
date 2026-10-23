# Residential V0 engineering report — October 16, 2026

Historical engineering and validation record. For current setup and the read-only
results viewer, see the repository README and docs/ARCHITECTURE.md.

Phases A–D are implemented for the restricted pilot. This is an executable, traceable residential **software baseline**, not a validated Marvin research model. The commercial example workflow remains separate. All residential outputs are directly reported EnergyPlus values; no performance comparisons, fuel conversions, economics, carbon, optimization or product recommendations were added.

## 1. Existing architecture preserved

Inspected CLI, Streamlit, configuration/input/result types, model generation, caches, subprocess execution, SQLite extraction, exports and batch tests before changing code. Baseline: **126 passed, 4 skipped**. Existing single/small/smoke/export, `mvp-real`, `mvp-batch`, commercial single-case comparisons and batch UI remain available.

Both commercial and residential batches now call `experiments/coordinator.py`: bounded local worker threads launch independent EnergyPlus subprocesses through the existing `EnergyPlusRunner`. Callbacks, manifests and CSV/Parquet exports have one coordinator writer. No unrelated simulator runner was introduced. Original weather discovery, completion gates, execution logs and SQL environment filtering are reused.

## 2. Files and modules

New modules:

- `thermal_home_modeling/catalogs/workbook.py`, `catalogs/registry.py`, `catalogs/__init__.py`: complete workbook import and separate implementation annotations.
- `thermal_home_modeling/models/residential.py`: supported immutable case inputs.
- `thermal_home_modeling/energyplus/residential_model.py`: complete residential epJSON generation, eligibility and symmetry checks.
- `thermal_home_modeling/experiments/coordinator.py`: shared bounded execution coordinator.
- `thermal_home_modeling/experiments/residential.py`: planning, physical signatures, cache validation, execution records, campaign checkpoints and resume.
- `thermal_home_modeling/presentation/streamlit_residential.py`: supported controls, preflight, progress and raw-results explorer.
- `tests/test_residential.py`, `tests/test_real_residential.py`: synthetic orchestration/physical contracts and opt-in actual EnergyPlus execution.

Updated `cli.py`, `energyplus/result_parser.py`, `experiments/mvp_batch.py`, `streamlit_app.py`, `pyproject.toml` and README. Added `openpyxl>=3.1,<4` for import; existing editable requirements install it.

Catalog artifacts: `catalogs/current.json`; immutable `catalogs/revisions/<workbook SHA256>/{source.xlsx,catalog.json,walls.idf}`; cell-difference `import_report.json`; separately reviewed `residential_mappings.json`; `residential_v0_assumptions.json`; `residential_validation.json` evidence.

## 3. Workbook import

Source: `Thermal_Home_Simulation_Inputs_and_Walls.xlsx`.

SHA256: `2b5c855486f4ff29307203d7fcb5205a391b070f5d85d135e60f45c152f3f3c6`.

All **16 sheets**, including README, are retained as **6,036 cell records**. Formula expressions, cached results, blank/null cells, TBD strings, number formats, comments and merged ranges remain inspectable. **74 registry entries** cover locations, houses, orientations, attic insulation, walls, foundations, windows, HVAC, environmental/planning/reference/pricing data. All six cities, six houses, thirteen wall definitions and six glazing/coating rows are imported. Window sizes, frame/divider/spacer choices and other pending fields remain in the overview records and complete sheet snapshots.

Parameter records separate workbook data status from implementation status, with source cells/hash, values, units where known, allowed values, defaults where genuinely defined, dependencies, constraints and pending decisions. The effective registry overlays software support without rewriting source data. Verification is scoped to the pilot and never means “Validated for research.”

Import compares every cell to the previous revision, reports pending/error markers and retains prior revisions. It never edits reviewed mapping files. Changed active wall physics must pass the reviewed mapping hash; changed house properties are rejected. Cached Excel formulas are **not recalculated by Python**: recalculate and save the workbook after formula changes. Missing required numeric/formula caches or inconsistent IDF/Layers definitions reject import. The 115,200 count remains a planning value only.

## 4. Active and inactive inputs

| Category | Active V0 support | Stored but inactive |
|---|---|---|
| Location | Washington DC, zone 4; exact Dulles EPW binding | Phoenix, Memphis, Boston, Minneapolis, Grand Forks; matching weather/mappings pending |
| House | 1S, one story, 1,500 ft² | 1M, 1L, 2S, 2M, 2L; final footprints pending |
| Orientation | 45° increments 0–315; pilot verified 0/90 | No automatic reduction for asymmetric future models |
| Wall | W3 and W9, eligible for DC in workbook | W1–W2, W4–W8, W10–W13 pending activation/review |
| Attic | DC nominal insulation R-49/R-80 | Other city/level combinations, including DC R-60/R-70 |
| Foundation | Fixed provisional V0 slab only | Workbook DC basement and all city foundation mappings |
| Windows | Fixed explicitly provisional V0 equivalent windows | Workbook WWR/glazing/coatings/products/dividers/frames/sizes/spacers as dimensions |
| HVAC | Unlimited ideal thermal loads | Equipment/fuel/efficiency/sizing conversions |
| Other | Provenance/reference metadata | Pricing, finance, environmental scoring and analyses |

Unsupported case fields and inactive selections raise errors. UI batch controls are restricted to supported and city-eligible values. Imported TBD values are never replaced by V0 assumptions.

## 5. Residential model

Python creates the entire model; no commercial model is copied or used as fallback. One 15.24 × 9.144 × 2.4384 m zone (50 × 30 × 8 ft), four external walls, roof, ground-contact floor and four windows. External vertex winding and explicit area/volume are consistent. Building north axis applies orientation. Selected construction objects physically change walls and roof insulation.

Weather is the installed `USA_VA_Sterling-Washington.Dulles.Intl.AP.724030_TMY3.epw`, station 724030, header `Washington Dc Dulles IntL Ar`, VA, USA, 38.98° N / −77.47° E, UTC−5, elevation 82 m. This is a Washington-area station in **Virginia**, not downtown DC. City aliases alone cannot authorize a different EPW: station, region, country and coordinates are checked, then the full file hash enters identity.

## 6. Provisional assumptions

The complete machine-readable record is `catalogs/residential_v0_assumptions.json`, copied into every campaign; effective model objects and run provenance are also retained.

| Component | Fixed provisional choice |
|---|---|
| Geometry | Rectangular single zone, flat roof, no partitions, basement, doors or exterior shading |
| Ground floor | 0.1 m concrete, k=1.4 W/(m K), density 2200 kg/m³, cp=900 J/(kg K); Ground boundary, 18°C all months; no soil dynamics/Kiva or slab-edge insulation |
| Roof | Compact roof, no attic zone/ventilation/framing bridges; 0.018 m deck, k=.13, density600, cp1600; selected nominal insulation; 0.0127 m gypsum |
| Windows | One centered window per façade, 19% gross wall area; U=2 W/(m² K), SHGC=.4, visible transmittance=.6; no named manufacturer, frames or dividers |
| Occupancy | Three people, 120 W/person; fraction1 from midnight–07:00 and 18:00–24:00; fraction.5 otherwise; same every day |
| Gains | Lights3 W/m², appliances4 W/m², both follow occupancy; people radiant.3; lights radiant.4/visible.2/return air0; appliances radiant.3/latent0/lost0 |
| Infiltration | Constant .35 ACH, coefficient1; temperature/wind coefficients0 |
| Controls | Constant 20°C heating / 26°C cooling; always available; no setback |
| HVAC | IdealLoads NoLimit; max heating supply50°C/min cooling13°C; no humidification, dehumidification, mechanical outdoor air, economizer or recovery; no equipment sizing/fuel conclusions |
| Calculation | Annual2001, Monday start, no daylight saving or holiday modifications; six timesteps/hour; no design days or sizing; Suburbs, FullExterior, CTF, TARP inside, DOE-2 outside; warmup6–50 days |
| Absorptance | Vinyl thermal.9/solar.6/visible.6; other unspecified opaque absorptances retain engine defaults thermal.9/solar.7/visible.7 |

The uninsulated slab with constant 18°C ground is a particularly strong limitation. All pilot zones stayed below the 26°C cooling setpoint: sensible cooling was zero; directly reported total cooling contains approximately 10⁻⁶ J numerical residuals. These values are retained exactly, not rounded into invented zeroes or presented as residential cooling predictions. The basement, ground heat transfer and operating assumptions need engineering review before performance research.

## 7. Materials and constructions

Mappings use the full precision cached `EnergyPlus Layers` values, independently checked against rounded `IDF Text` objects. W3 is wall row8 / layers row38; W9 is wall row14 / layers row44. Both have Washington DC eligibility Y. Outside-to-inside order is preserved exactly:

`Vinyl Siding → OSB 7/16 → Wn Exterior Foam → Wn Cavity Equivalent → Gypsum 1/2`.

| Layer | Thickness m | Conductivity W/(m K) | Density kg/m³ | Specific heat J/(kg K) |
|---|---:|---:|---:|---:|
| OSB 7/16 | .0111 | .12359287837792698 | 650 | 1880 |
| W3/W9 XPS | .0508 | .028847245883021008 | 29 | 1210 |
| W3 equivalent cavity | .0889 | .050257393242687264 | 120.75 | 1571.1180124223602 |
| W9 equivalent cavity | .1397 | .036397280091671029 | 135.75 | 1622.9281767955802 |
| Gypsum | .0127 | .16026247712789449 | 800 | 1090 |

Vinyl is `Material:NoMass`, R=.109182 m² K/W, retaining the workbook’s R=.62 IP × .1761 conversion. Equivalent cavities include the workbook’s 25% framing representation. No hand-calculation film resistances are added as materials. Nominal attic insulation uses `Material:NoMass`, R(IP) × .1761101838 m² K/W, with deck and gypsum separate.

Engineering status remains provisional: typical density/heat-capacity values and closed-cell foam R/in require confirmation; OSB-outside-foam ordering follows the workbook and warrants review. W13’s known −.51 IP layer/hand-calculation difference is flagged; it is inactive. W12 and other unverified assemblies are not automatically activated. This is mapping fidelity, not independent certification of the source physics or local code compliance.

The installed 26.2 epJSON schema was inspected. EnergyPlus documents Material:NoMass resistance in SI and the ideal-load variables as thermal energy. References: [construction objects](https://bigladdersoftware.com/epx/docs/9-4/input-output-reference/group-surface-construction-elements.html), [ideal-load model](https://bigladdersoftware.com/epx/docs/26-1/engineering-reference/ideal-loads-air-system.html). Actual variable existence/units were confirmed from this installation’s generated RDD and SQL, not inferred from the commercial meters.

## 8. Proof of physical changes and identity

Eight input combinations produce eight distinct effective signatures. Tests inspect actual north axis, façade construction assignments, layer order, cavity thickness/conductivity and roof resistance. Geometry/window/construction symmetry checks reject removed windows, shading, additional zones and asymmetric wall assignments. No requested orientations are silently discarded; 0/45/90/135 can be selected when the check passes.

Cache identity includes complete generated model, relevant physical catalog subset, actual weather file hash/path, engine version/executable hash, generator revision and parser configuration. Description-only changes do not change physical identity. Cache reuse verifies generated model, manifest, exact input record, SQL hash, completion gates and parsed values. Corrupt SQL/model/result/end markers force a fresh attempt. Campaign snapshots retain the full workbook catalog, mappings and assumptions even when a prior result is reused; reused results retain their original provenance.

## 9–10. Actual execution and timings

**26 new real annual residential EnergyPlus executions**, all successful: one initial baseline, one diagnostic baseline with added hourly temperatures, eight sequential pilot cases, eight four-worker pilot cases, and eight more in the opt-in integration test. No fabricated runner was used for these 26. The subsequent resume check launched zero subprocesses and reused all eight matching pilot cases.

| Campaign | New / cached | Success / failure | Wall time |
|---|---|---|---:|
| Sequential pilot | 8 / 0 | 8 / 0 | 7.1060 s |
| Parallel pilot, four workers | 8 / 0 | 8 / 0 | 2.2446 s |
| Resume of parallel pilot | 0 / 8 | 8 / 0 | .5006 s |

Execution timestamps independently confirm peak concurrency1 and4. Sequential and parallel raw load values and effective signatures match exactly. This is reproducibility validation; no building-performance savings or comparisons are computed.

## 11. Actual artifact locations

Paths below are relative to this repository:

- Initial baseline: `../output/batches/residential_57673cffa5ce4db8a2bee9e20c51638c/`.
- Sequential pilot: `../output/batches/residential_6bb58f0245aa46538b3367008d3136c1/`.
- Parallel pilot: `../output/batches/residential_c3635b3a37314bd1a542b678dbc19568/`.
- Cached resume: `../output/batches/residential_0037b936b1c848c4b706f56bd3f3fd1a/`.
- Integration pilot: `data/residential_integration/bc9f1f57fe1a49ddad447502df4c8690/`.
- Normal generated inputs: `../input/generated/res_<UUID>/model.epJSON`, `simulation_input.json`, `run_manifest.json`.
- Normal raw outputs: `../output/res_<UUID>/energyplus/`; case record `../output/res_<UUID>/result.json`.

Each campaign contains `batch_manifest.json`, `batch_summary.json`, `batch_results.csv`, `batch_results.parquet` and three configuration snapshots. The indexes point to every individual generated model, result and raw directory. Per-run SQL, ERR, END, RDD, ESO and other engine outputs plus stdout/stderr/execution metadata are retained. Hourly data stays in per-run raw files; no giant hourly campaign JSON exists.

Both total heating and total cooling are extracted from `Zone Ideal Loads Zone Total … Energy`, key `IDEAL LOADS`, units **J**, frequency **Run Period**, 365 weather days. Hourly versions and zone temperature/humidity/setpoint/sensible-cooling diagnostics are retained too. Failed/unavailable metrics remain null.

## 12. Streamlit

Workflow switch separates **Commercial demonstration** from **Residential simulation — V0**. Residential controls support DC/1S, W3/W9, R49/R80 and orientations; default selection creates exactly eight cases. Preflight displays dimensions, total/new/cached counts and CPU-capped workers. More than20 configurations requires confirmation. Complete catalog and pending decisions remain inspectable without fake active controls.

A submitted request is consumed once. Progress shows per-case state, active workers, completed/failed/cached counts and elapsed operation. Normal rerenders never launch work. Resume uses immutable prior campaign snapshots and valid case checkpoints, preserving the previous attempt.

Results enter by city, climate zone and house, then campaign and filterable 20-row pages. A selected run exposes exact inputs, original-unit outputs, engine/version/runtime/provenance, warnings/errors, file paths and selectable downloads. Only compact indexes are loaded across campaigns; raw databases are not bulk-loaded. Download bytes are loaded only after selecting and preparing one artifact.

## 13. Tests

Baseline:126 passed/4 skipped. New default residential suite:23 passed, including full import/diffs, physical mappings, inactive rejection, wrong-weather rejection, symmetry, description-independent identity, bounded sequential/parallel operation, isolated exports, corruption handling, partial and interrupted resume, original-unit parser and Streamlit launch-once/browse behavior.

Actual simulator integration:1 passed, launching eight isolated cases, checking RDD/SQL variables, all 8,760 hourly zone temperatures, correct orientation/construction and preserved output files. Full regression including saved commercial integration artifacts is recorded in the final validation update below.

## 14. Errors and warnings

No Severe/Fatal errors or nonzero simulator exits in the 26 residential runs. Each run reports **one psychrometric temperature-range warning** at March15 19:50–20:00 (`PsyPsatFnTemp`, called by `PsyTwbFnTdbWPb`, input −124.32°C), repeated in the recurring-error summary. The existing warning reader preserves both text blocks; index count2 is **two blocks, not two independent occurrences**. Its underlying cause is not established. Hourly zone temperatures remained within the configured thermostat bounds in the real integration checks. Preserve the warning for investigation before research validation; it is not silently removed or claimed resolved.

The sandbox’s Arrow CPU-feature probes emitted permission notices during some local checks; tests and exports completed. No data was lost.

## 15. Remaining engineering decisions

Final footprints and additional house geometry; actual conditioned-basement/foundation and soil modeling; attic geometry/ventilation/framing bridges; operating schedules/gains/infiltration/ventilation and thermostat assumptions; actual window operator/picture performance, provisional U/SHGC, frames/dividers/spacers; material property confirmation and assembly order; ICF/SIP discrepancies; other cities’ matching EPWs and validated location mappings; local-code interpretation versus workbook study assumptions; numerical psychrometric warning. These are not marked complete by software execution.

## 16. Reproduce

From this repository (quote the workbook path):

```sh
.venv/bin/python main.py catalog-import --workbook /Users/ivanvashchenko/Downloads/Thermal_Home_Simulation_Inputs_and_Walls.xlsx
.venv/bin/python main.py residential
.venv/bin/python main.py residential-batch --mode sequential --rerun
.venv/bin/python main.py residential-batch --mode parallel --workers 4 --rerun
.venv/bin/python main.py residential-batch --mode parallel --workers 4
.venv/bin/python main.py residential-batch --resume-batch ../output/batches/residential_c3635b3a37314bd1a542b678dbc19568 --mode parallel --workers 4
.venv/bin/streamlit run streamlit_app.py
.venv/bin/python -m pytest -q
THM_RUN_REAL_RESIDENTIAL=1 .venv/bin/python -m pytest -q -s tests/test_real_residential.py
```

Use `--walls W3,W9 --attic-levels 49,80 --orientations 0,90` to state the default pilot explicitly. `--rerun` creates fresh attempts; omit it for verified cache reuse. The old `mvp-real` and `mvp-batch` commands retain their commercial behavior. The workbook planning factorial is never launched automatically.

Final regression validation: **153 passed, 1 skipped in 20.46 seconds**, with the saved real commercial single/orientation/batch integration fixtures enabled. The one skipped test is the opt-in residential simulator test, which was separately run and passed (eight actual simulations, artifacts preserved at the integration path above). The default suite without saved-artifact environment variables has 149 tests enabled and five opt-in checks skipped.

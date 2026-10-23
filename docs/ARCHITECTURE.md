# Residential V0 architecture

The application separates computation from presentation. The CLI starts and manages
campaigns; Streamlit reads saved results only.

```text
CLI → experiment design → residential model generator → batch coordinator
    → EnergyPlus runner → SQLite parser → persistent results → Streamlit viewer
```

1. `main.py` calls `cli.main`. For `residential-batch`, the CLI loads the active
   normalized workbook catalog, reviewed mappings, assumptions, and installed
   EnergyPlus metadata. The experiment JSON or CLI flags select physical dimensions.
2. `experiments.residential_design.design_from_spec` validates values, resolves
   matching EPW files, applies city eligibility and exclusions, verifies physical
   180° symmetry, removes equivalent models, and samples the requested case set.
3. `energyplus.residential_model.generate_residential_model` creates one complete
   epJSON model per case. `experiments.residential.prepare_residential_cases`
   hashes effective model, weather, engine, catalog physics, and parser settings
   to identify valid reusable results.
4. Foreground batches call `run_residential_batch` directly. Background batches
   use `campaign_jobs.submit_campaign` to save immutable request snapshots and
   launch a detached local worker. `coordinator.coordinate_cases` bounds concurrent
   workers and routes completion events to one checkpoint writer.
5. Each uncached case writes `input/generated/res_ID/model.epJSON`,
   `simulation_input.json`, and `run_manifest.json`. `EnergyPlusRunner.run`
   launches an annual EnergyPlus subprocess with that model and EPW in an isolated
   `output/res_ID/energyplus/` directory. Return code, completion marker, and
   Severe/Fatal diagnostics determine process success.
6. `SQLiteResultParser.parse_raw_annual_variables` reads `eplusout.sql`
   read-only. It requires one complete weather year and the requested Run Period
   heating/cooling ideal-load variables in joules. `execute_residential_case`
   writes `output/res_ID/result.json` with values, status, warnings, file hashes,
   runtime, and provenance.
7. The batch writes individual `records/*.json` checkpoints, a running
   `batch_manifest.json`, and consolidated `batch_results.csv` and
   `batch_results.parquet` indexes. `batch_summary.json` stores counts and
   measured timing. The per-run JSON and raw SQLite files are the detailed
   evidence; CSV/Parquet are compact searchable views and can be rebuilt from
   checkpoints.
8. `presentation.streamlit_residential.render_residential` reads existing
   residential Parquet indexes and selected run JSON files. It has no execution
   dependency and needs no local EnergyPlus installation to open saved results.

The active model currently has one zone and ideal heating/cooling loads. Geometry,
slab ground temperature, windows, schedules, and gains are provisional. The
commercial example and generic experiment framework have separate CLI paths and
schemas; their shared runner/parser infrastructure is retained.

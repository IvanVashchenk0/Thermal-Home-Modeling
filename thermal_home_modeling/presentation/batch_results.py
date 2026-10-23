"""Per-weather batch comparisons and charts with explicit fuel/annual units."""

import pandas as pd
import streamlit as st

from thermal_home_modeling.experiments.mvp_batch import BatchOutcome, measured_speedup
from thermal_home_modeling.presentation.comparison import energy_change

COMPARISON_METRICS = {
    "cooling_electricity_kwh": ("Cooling electricity", "kWh/year"),
    "heating_natural_gas_kwh_equivalent": ("Heating natural gas", "kWh-eq/year"),
    "facility_electricity_kwh": ("Whole-building electricity", "kWh/year"),
}


def batch_comparisons(frame: pd.DataFrame) -> pd.DataFrame:
    """Use only a successful 0° row with the same EPW and comparison signature."""
    rows = []
    successful = frame.loc[frame.success.fillna(False)]
    for _, group in successful.groupby(["weather_file", "comparison_signature"], dropna=True):
        baseline = group.loc[group.orientation_deg == 0]
        if len(baseline) != 1 or not str(baseline.iloc[0].comparison_signature):
            continue
        reference = baseline.iloc[0]
        for _, selected in group.iterrows():
            for field, (label, unit) in COMPARISON_METRICS.items():
                if pd.isna(reference[field]) or pd.isna(selected[field]):
                    continue
                change = energy_change(label, float(selected[field]), float(reference[field]), unit)
                rows.append({"City / weather": selected.weather_label, "Orientation (°)": selected.orientation_deg,
                             "Metric": label, "Change (%)": change.percent,
                             "Absolute change": change.difference, "Unit": unit,
                             "Baseline simulation": reference.simulation_id,
                             "Simulation": selected.simulation_id})
    return pd.DataFrame(rows, columns=["City / weather", "Orientation (°)", "Metric", "Change (%)",
                                       "Absolute change", "Unit", "Baseline simulation", "Simulation"])


def render_batch_results(outcome: BatchOutcome, previous_summaries: list[dict]) -> None:
    summary, frame = outcome.summary, outcome.frame
    if summary["failed"]:
        st.warning(f"Batch finished: {summary['succeeded']} succeeded, {summary['failed']} failed. Errors are retained in the results table and case records.")
    else:
        st.success(f"Batch complete: {summary['total_cases']} configurations; {summary['cached_cases']} reused.")
    st.caption("The example model retains its original Chicago HVAC design-day assumptions. Cross-city outputs demonstrate the software pipeline and are not validated residential research conclusions.")
    a, b, c = st.columns(3)
    a.metric("Batch wall time", f"{summary['wall_clock_seconds']:.2f} s")
    b.metric("Newly executed cases", summary["newly_executed_cases"])
    c.metric("Maximum workers", summary["max_workers"])
    st.caption(
        f"New successful simulations per minute: {summary['new_successful_simulations_per_minute']:.2f} · "
        f"Sum of new EnergyPlus runtimes: {summary['sum_energyplus_runtime_seconds']:.2f} s"
    )
    if summary["mean_energyplus_runtime_seconds"] is not None:
        st.caption(f"Individual runtime — mean: {summary['mean_energyplus_runtime_seconds']:.2f} s; median: {summary['median_energyplus_runtime_seconds']:.2f} s. Cached runs are excluded.")
    for previous in reversed(previous_summaries):
        speedup = measured_speedup(previous, summary)
        if speedup is not None:
            pair = {s["mode"]: s for s in (previous, summary)}
            st.info(f"Equivalent new runs — Sequential: {pair['sequential']['wall_clock_seconds']:.2f} s · "
                    f"Parallel: {pair['parallel']['wall_clock_seconds']:.2f} s · Observed speedup: {speedup:.2f}×")
            st.caption(f"Compared batches: {previous['batch_id']} and {summary['batch_id']}. One measured pair, not a general performance guarantee.")
            break
    successful = frame.loc[frame.success.fillna(False)]
    for field, title, unit in (
        ("cooling_electricity_kwh", "Cooling electricity by orientation", "Annual electricity (kWh)"),
        ("heating_natural_gas_kwh_equivalent", "Heating natural-gas consumption by orientation", "Annual natural gas (kWh-equivalent)"),
    ):
        st.markdown(f"**{title}**")
        data = successful.dropna(subset=[field])
        if data.empty:
            st.caption("No verified values are available for this meter.")
        else:
            st.vega_lite_chart(data[["orientation_deg", "weather_label", field]], {
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": {"field": "orientation_deg", "type": "ordinal", "title": "Orientation (°)", "sort": "ascending"},
                    "y": {"field": field, "type": "quantitative", "title": unit, "scale": {"zero": True}},
                    "color": {"field": "weather_label", "type": "nominal", "title": "City / weather"},
                    "tooltip": [{"field": "weather_label"}, {"field": "orientation_deg"}, {"field": field, "format": ",.1f"}],
                },
            }, use_container_width=True)
    st.markdown("**Difference relative to each city’s 0° baseline**")
    changes = batch_comparisons(frame)
    st.caption("Only matching weather/configuration pairs are compared. Select 0° to include each city's baseline; no extra simulations are added. A zero baseline has no defined percentage. End uses are already included in whole-building totals.")
    if changes.empty:
        st.info("No compatible successful 0° baseline and reported metrics are available in this batch.")
    else:
        chart_data = changes.dropna(subset=["Change (%)"])
        if not chart_data.empty:
            st.vega_lite_chart(chart_data, {
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "x": {"field": "Orientation (°)", "type": "ordinal", "sort": "ascending"},
                    "y": {"field": "Change (%)", "type": "quantitative"},
                    "color": {"field": "City / weather", "type": "nominal"},
                    "row": {"field": "Metric", "type": "nominal"},
                    "tooltip": [{"field": "City / weather"}, {"field": "Metric"},
                                {"field": "Orientation (°)"}, {"field": "Change (%)", "format": ".1f"}],
                },
            }, use_container_width=True)
        st.dataframe(changes, hide_index=True)
    st.markdown("**Batch results**")
    st.caption("Energy columns are annual. Gas uses kWh-equivalent of fuel; electricity uses kWh. Missing meters remain empty. Cached rows have no new execution runtime; their original runtime is retained separately.")
    st.dataframe(frame, hide_index=True)
    st.download_button("Download batch CSV", frame.to_csv(index=False),
                       file_name=f"{outcome.batch_id}.csv", mime="text/csv", key="batch_download")
    with st.expander("Batch warnings and errors", expanded=False):
        st.dataframe(frame[["weather_label", "orientation_deg", "status", "warning_count", "warnings_json", "error_type", "error_message"]], hide_index=True)
    with st.expander("Batch files and timing details", expanded=False):
        st.code(str(outcome.directory), language=None)
        st.json(summary)

"""Read-only presentation of saved Residential V0 batch and run artifacts."""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from thermal_home_modeling.config.mvp_config import MVPPaths

FIELDS = {
    "city": "City", "house_id": "House", "wall_id": "Wall",
    "attic_r_ip": "Attic R-value", "orientation_deg": "Orientation (°)",
    "status": "Status",
}


def batch_indexes(paths: MVPPaths) -> list[Path]:
    """Find compact residential indexes without opening raw SQL databases."""
    return sorted(paths.output.glob("batches/residential_*/batch_results.parquet"),
                  key=lambda path: path.stat().st_mtime_ns, reverse=True)


def render_residential(paths: MVPPaths | None = None) -> None:
    """Render existing results; this module never imports execution code."""
    paths = paths or MVPPaths.default()
    indexes = batch_indexes(paths)
    if not indexes:
        st.info(f"No residential batch results found under {paths.output / 'batches'}.")
        st.caption("Run residential-batch from the command line, then refresh this page.")
        return

    campaign = st.selectbox("Campaign", ["All campaigns", *[str(p.parent) for p in indexes]],
                            index=1, format_func=lambda value: Path(value).name if value != "All campaigns" else value)
    chosen = indexes if campaign == "All campaigns" else [Path(campaign) / "batch_results.parquet"]
    try:
        frame = pd.concat((pd.read_parquet(path) for path in chosen), ignore_index=True)
    except (OSError, ValueError, ImportError) as exc:
        st.error(f"Could not read the saved batch index: {exc}")
        return
    if frame.empty:
        st.info("The selected campaign has no completed case records yet.")
        return

    for field, label in FIELDS.items():
        if field not in frame:
            continue
        options = sorted(frame[field].dropna().unique().tolist())
        selected = st.multiselect(label, options, key=f"filter_{field}")
        if selected:
            frame = frame[frame[field].isin(selected)]
    query = st.text_input("Search simulation ID or configuration")
    if query:
        searchable = [field for field in ("simulation_id", "city", "house_id", "wall_id") if field in frame]
        frame = frame[frame[searchable].astype(str).apply(
            lambda column: column.str.contains(query, case=False, regex=False)).any(axis=1)]
    if frame.empty:
        st.info("No cases match these filters.")
        return

    success = frame[frame["success"].fillna(False)]
    a, b, c = st.columns(3)
    a.metric("Cases shown", len(frame))
    b.metric("Successful", len(success))
    c.metric("Failed", len(frame) - len(success))
    st.caption("Annual ideal zone loads are direct EnergyPlus Run Period outputs in joules (J). They are not purchased energy.")
    if not success.empty:
        means = success.groupby("city", dropna=False)[["heating_load_j", "cooling_load_j"]].mean()
        st.bar_chart(means, x_label="City", y_label="Mean annual ideal load (J)")

    page_size = 25
    max_page = max(1, (len(frame) + page_size - 1) // page_size)
    page = st.number_input("Results page", min_value=1, max_value=max_page, value=1, step=1)
    displayed = frame.iloc[(page - 1) * page_size:page * page_size].reset_index(drop=True)
    columns = [name for name in ("simulation_id", "city", "house_id", "wall_id", "attic_r_ip",
               "orientation_deg", "status", "heating_load_j", "cooling_load_j", "warning_count") if name in displayed]
    event = st.dataframe(displayed[columns], hide_index=True, on_select="rerun",
                         selection_mode="single-row", key="res_results_table")
    selection = event.selection.rows
    row = displayed.iloc[selection[0] if selection and selection[0] < len(displayed) else 0]
    result_path = Path(row.result_path)
    if not result_path.is_file():
        st.warning(f"Saved run record is missing: {result_path}")
        return
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError) as exc:
        st.error(f"Could not read the saved run record: {exc}")
        return
    st.subheader("Selected simulation")
    st.json({key: result.get(key) for key in ("simulation_id", "inputs", "outputs", "success",
            "energyplus_version", "energyplus_runtime_seconds", "catalog_revision", "generated_input",
            "output_directory", "warnings", "error_type", "error_message")})

    if campaign != "All campaigns":
        summary_path = Path(campaign) / "batch_summary.json"
        if summary_path.is_file():
            with st.expander("Batch summary"):
                st.json(json.loads(summary_path.read_text()))
                st.download_button("Download batch summary", summary_path.read_bytes(), file_name="batch_summary.json")
        csv_path = Path(campaign) / "batch_results.csv"
        if csv_path.is_file():
            st.download_button("Download batch CSV", csv_path.read_bytes(), file_name="batch_results.csv")

    raw = Path(result.get("output_directory", ""))
    generated = Path(result.get("generated_input", ""))
    artifacts = [result_path, *sorted(generated.parent.glob("*.json")),
                 *sorted(raw.glob("*.err")), *sorted(raw.glob("*.end")),
                 *sorted(raw.glob("execution.json"))]
    artifacts = list(dict.fromkeys(path for path in artifacts if path.is_file()))
    with st.expander("Saved files and provenance"):
        artifact = st.selectbox("Artifact", artifacts, format_func=lambda path: path.name)
        st.code(str(artifact))
        st.download_button("Download selected artifact", artifact.read_bytes(), file_name=artifact.name)

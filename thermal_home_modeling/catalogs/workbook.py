"""Repeatable workbook import. Cached Excel formulas are preserved, never executed."""

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil

from openpyxl import load_workbook

from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.utilities.files import file_digest, fingerprint, write_json

CATALOG_ROOT = Path(__file__).resolve().parents[2] / "catalogs"
REQUIRED_SHEETS = ("Inputs Overview", "Run Impact", "Run Count", "Cities", "Houses", "Foundations", "Windows",
                   "Wall Configurations", "Assumptions", "EnergyPlus Layers", "IDF Text", "Open Decisions",
                   "IECC Reference", "Sources", "Pricing Data")


def slug(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def read_workbook(path: Path) -> dict:
    """Retain all sheets, blank/TBD cells, formula expressions, units, comments and cached values."""
    values = load_workbook(path, data_only=True)
    formulas = load_workbook(path, data_only=False)
    missing = set(REQUIRED_SHEETS) - set(values.sheetnames)
    if missing:
        raise ConfigurationError(f"Workbook is missing sheets: {sorted(missing)}")
    sheets = {}
    for sheet in formulas:
        cells = {}
        for row in sheet:
            for cell in row:
                value = values[sheet.title][cell.coordinate].value
                if hasattr(value, "isoformat"):
                    value = value.isoformat()
                cells[cell.coordinate] = {"value": value, "formula": cell.value if cell.data_type == "f" else None,
                    "number_format": cell.number_format, "comment": cell.comment.text if cell.comment else None,
                    "excel_type": cell.data_type}
        sheets[sheet.title] = {"cells": cells, "rows": sheet.max_row, "columns": sheet.max_column,
                               "merged_ranges": [str(r) for r in sheet.merged_cells.ranges]}
    values.close()
    formulas.close()
    return sheets


def value(sheets, sheet, cell):
    return sheets[sheet]["cells"].get(cell, {}).get("value")


def number(sheets, sheet, cell):
    v = value(sheets, sheet, cell)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ConfigurationError(f"Missing/malformed numeric value: {sheet}!{cell} = {v!r}; recalculate and save the workbook")
    return v


def row_values(sheets, sheet, row, columns):
    return {column: value(sheets, sheet, f"{column}{row}") for column in columns}


def wall_mapping(sheets, row):
    """Use full-precision EnergyPlus Layers values in their specified outside-to-inside order."""
    wall_id = value(sheets, "EnergyPlus Layers", f"A{row}")
    layers = [value(sheets, "EnergyPlus Layers", f"{c}{row}") for c in "QRSTU"]
    layers = [name for name in layers if name]
    mats = {}
    for r in range(16, 21):
        name = value(sheets, "EnergyPlus Layers", f"A{r}")
        if name not in layers:
            continue
        props = {"roughness": value(sheets, "EnergyPlus Layers", f"B{r}")}
        if r == 16:
            props.update(thermal_resistance=number(sheets, "EnergyPlus Layers", "G16") * number(sheets, "EnergyPlus Layers", "B5"),
                         thermal_absorptance=0.9, solar_absorptance=number(sheets, "EnergyPlus Layers", "B12"),
                         visible_absorptance=number(sheets, "EnergyPlus Layers", "B12"))
            kind = "Material:NoMass"
        else:
            props.update({key: number(sheets, "EnergyPlus Layers", f"{col}{r}") for col, key in
                          zip("CDEF", ("thickness", "conductivity", "density", "specific_heat"))})
            kind = "Material"
        mats[name] = {"object_type": kind, "properties": props}
    for name, columns, roughness in ((f"{wall_id} Exterior Foam", "GHIJ", "MediumSmooth"),
                                    (value(sheets, "EnergyPlus Layers", f"K{row}"), "MNOP", "MediumRough")):
        if name in layers:
            props = {"roughness": roughness, **{key: number(sheets, "EnergyPlus Layers", f"{col}{row}")
                     for col, key in zip(columns, ("thickness", "conductivity", "density", "specific_heat"))}}
            mats[name] = {"object_type": "Material", "properties": props}
    if set(layers) != set(mats):
        raise ConfigurationError(f"Incomplete material definitions for {wall_id}")
    for m in mats.values():
        if any(v <= 0 for k, v in m["properties"].items() if k in ("thermal_resistance", "thickness", "conductivity", "density", "specific_heat")):
            raise ConfigurationError(f"Nonpositive physical property in {wall_id}")
    return {"construction_name": f"Wall {wall_id}", "layers": layers, "materials": mats}


def parse_wall_idf(text: str) -> dict:
    """Only the three expected construction/material types are accepted as data."""
    clean = re.sub(r"!.*", "", text)
    objects = {}
    for block in clean.split(";"):
        parts = [v.strip() for v in block.strip().split(",")]
        if len(parts) < 2:
            continue
        if parts[0] not in ("Material", "Material:NoMass", "Construction"):
            raise ConfigurationError(f"Unexpected IDF Text object {parts[0]}")
        objects[parts[1]] = {"type": parts[0], "values": parts[2:]}
    return objects


def normalize(path: Path) -> dict:
    sheets = read_workbook(path)
    revision = file_digest(path)
    issues = [{"severity": "notice", "message": "Excel cached values are imported without recalculation; recalculate/save in Excel after editing formulas."}]
    records = []

    def record(id, category, name, data, sheet, cells, *, status="Provisional", dtype="object", units=None,
               allowed=None, default=None, notes="", dependencies=None):
        item = dict(parameter_id=id, category=category, name=name, data_type=dtype, units=units,
                    allowed_values=allowed, default=default, value=data, data_status=status,
                    implementation_status="Catalog only", changes_model=False, batch_eligible=False,
                    source={"sheet": sheet, "range": cells, "workbook_sha256": revision},
                    dependencies=dependencies or [], constraints=[], notes=notes, unresolved_decisions=[])
        records.append(item)
        return item

    for r in range(5, 27):
        name = value(sheets, "Inputs Overview", f"B{r}")
        source_status = value(sheets, "Inputs Overview", f"F{r}")
        record("overview_" + slug(name), slug(value(sheets, "Inputs Overview", f"A{r}")), name,
               row_values(sheets, "Inputs Overview", r, "CDEFGH"), "Inputs Overview", f"A{r}:H{r}",
               status={"Decided": "Confirmed", "TBD": "TBD"}.get(source_status, "Provisional"),
               notes=value(sheets, "Inputs Overview", f"G{r}") or "")
    locations, houses, walls, foundations, windows = {}, {}, {}, {}, {}
    for r in range(6, 12):
        city = value(sheets, "Cities", f"B{r}")
        id = slug(city)
        data = {"city": city, "state": value(sheets, "Cities", f"C{r}"), "climate_zone": number(sheets, "Cities", f"A{r}"),
                "doe_ac_region": value(sheets, "Cities", f"D{r}"), "code_basis": value(sheets, "Cities", f"E{r}"),
                "max_wall_u_ip": number(sheets, "Cities", f"F{r}"), "triple_pane": value(sheets, "Cities", f"H{r}") == "Y",
                "attic_levels_r_ip": [number(sheets, "Cities", f"{col}{r}") for col in "KLMN"],
                "attic_minimum_r_ip": number(sheets, "Cities", f"J{r}"), "weather_file": None,
                "notes": value(sheets, "Cities", f"I{r}"), "ceiling_source": value(sheets, "Cities", f"P{r}")}
        locations[id] = record(id, "locations", city, data, "Cities", f"A{r}:P{r}", status="Confirmed",
                              dependencies=["Matching EPW required; city-specific attic and wall eligibility"])
        record(f"attic_{id}", "attic_insulation", f"{city} attic levels", data["attic_levels_r_ip"], "Cities", f"J{r}:P{r}",
               status="Confirmed", units="h-ft2-F/Btu", allowed=data["attic_levels_r_ip"], dependencies=[id])
        h = value(sheets, "Houses", f"A{r}")
        house = {"stories": number(sheets, "Houses", f"B{r}"), "floor_area_ft2": number(sheets, "Houses", f"C{r}"),
                 "footprint": value(sheets, "Houses", f"D{r}"), "foundation": value(sheets, "Houses", f"E{r}")}
        houses[h] = record(h, "houses", h, house, "Houses", f"A{r}:F{r}", notes="Sizes proposed; footprint TBD; implementation is separate.")
        fr = r - 1
        foundations[id] = record(f"foundation_{id}", "foundations", f"{city} foundation",
            row_values(sheets, "Foundations", fr, "CDEFGHIJ"), "Foundations", f"A{fr}:J{fr}",
            dependencies=[id], notes="Fixed by city; baseline conditioning/type remains subject to advisor confirmation.")
    orientations = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(value(sheets, "Inputs Overview", "C8")).split("deg")[0])]
    wwrs = [float(x)/100 for x in re.findall(r"(\d+(?:\.\d+)?)%", str(value(sheets, "Inputs Overview", "C14")))]
    if not orientations or not wwrs:
        raise ConfigurationError("Missing/malformed orientation or WWR catalog list")
    record("orientation", "orientations", "Selected orientations", orientations, "Inputs Overview", "C8:G8",
           status="Confirmed", units="degrees clockwise from north", allowed=orientations,
           dependencies=["180-degree physical symmetry must be checked before reducing a full orientation set"])
    record("wwr", "windows", "Window-to-wall ratio", wwrs, "Inputs Overview", "C14:G14",
           status="Confirmed", units="fraction of gross above-grade wall area", allowed=wwrs)
    for r in range(7, 13):
        id = slug(f"{value(sheets, 'Windows', f'A{r}')}_{value(sheets, 'Windows', f'B{r}')}")
        windows[id] = record(id, "windows", id, row_values(sheets, "Windows", r, "ABCDEFGHIJKL"), "Windows", f"A{r}:L{r}",
                             units={"C": "Btu/(h ft2 F)", "D": "SHGC fraction", "E": "SHGC fraction"},
                             notes="Provisional manufacturer values; operator/picture split pending. No V0 product mapping.")
    idf = "\n".join(str(value(sheets, "IDF Text", f"A{r}") or "") for r in range(4, sheets["IDF Text"]["rows"] + 1))
    idf_objects = parse_wall_idf(idf)
    for r in range(6, 19):
        id = value(sheets, "Wall Configurations", f"A{r}")
        mapping = wall_mapping(sheets, r + 30)
        if idf_objects[mapping["construction_name"]]["values"] != mapping["layers"]:
            raise ConfigurationError(f"IDF Text and EnergyPlus Layers disagree for {id}")
        for name, material in mapping["materials"].items():
            obj = idf_objects[name]
            fields = ("thermal_resistance",) if material["object_type"] == "Material:NoMass" else ("thickness", "conductivity", "density", "specific_heat")
            if obj["type"] != material["object_type"] or obj["values"][0] != material["properties"]["roughness"]:
                raise ConfigurationError(f"Material type/roughness mismatch: {name}")
            for index, field in enumerate(fields, 1):
                if not math.isclose(float(obj["values"][index]), material["properties"][field], rel_tol=0.002, abs_tol=1e-5):
                    raise ConfigurationError(f"Rounded IDF Text conflicts with Layers: {name}.{field}")
        data = {"description": value(sheets, "Wall Configurations", f"B{r}"),
                "definition": row_values(sheets, "Wall Configurations", r, "CDEFGHOPQRSTUV"),
                "eligible_cities": [idc for col, idc in zip("IJKLMN", locations) if value(sheets, "Wall Configurations", f"{col}{r}") == "Y"],
                "mapping": mapping, "mapping_hash": fingerprint(mapping),
                "layer_check_difference_r_ip": number(sheets, "EnergyPlus Layers", f"W{r+30}")}
        walls[id] = record(id, "wall_assemblies", data["description"], data, "EnergyPlus Layers", f"A{r+30}:W{r+30}",
                           notes="Equivalent cavity includes framing effects. No film layers. Source properties include unverified typical values.",
                           dependencies=[f"Wall Configurations!A{r}:AI{r}", "EnergyPlus Layers!A5:H32", "IDF Text!A4:A418", "Sources!A5:F39"])
        if abs(data["layer_check_difference_r_ip"]) > 0.01:
            issues.append({"severity": "warning", "parameter_id": id, "message": "Layer/hand-calculation R mismatch; inactive pending review (ICF omits OSB)."})
    for name in ("Assumptions", "Pricing Data", "Open Decisions", "IECC Reference", "Sources", "Run Impact", "Run Count"):
        record(slug(name), "other_parameters", name, sheets[name], name, f"A1:{sheets[name]['columns']} columns/{sheets[name]['rows']} rows",
               notes="Reference/catalog only. Pricing, finance, environmental and derived performance calculations inactive.")
    for name, sheet in sheets.items():
        for coord, cell in sheet["cells"].items():
            if cell["excel_type"] == "e":
                issues.append({"severity": "warning", "source": f"{name}!{coord}", "message": str(cell["value"])})
            if isinstance(cell["value"], str) and re.search(r"\b(TBD|to verify|confirm|pending|placeholder)\b", cell["value"], re.I):
                issues.append({"severity": "pending", "source": f"{name}!{coord}", "message": cell["value"]})
    return {"schema_version": 1, "workbook_sha256": revision, "workbook_name": path.name,
            "sheets": sheets, "parameters": records, "locations": locations, "houses": houses,
            "wall_assemblies": walls, "foundations": foundations, "windows": windows,
            "planning_count": number(sheets, "Run Count", "B19"), "issues": issues, "wall_idf_text": idf}


def import_catalog(path: Path, root: Path = CATALOG_ROOT) -> dict:
    catalog = normalize(path)
    current = root / "current.json"
    previous = load_catalog(root) if current.exists() else None
    changes = []
    old_sheets = previous["sheets"] if previous else {}
    for sheet in sorted(set(old_sheets) | set(catalog["sheets"])):
        old = old_sheets.get(sheet, {}).get("cells", {})
        new = catalog["sheets"].get(sheet, {}).get("cells", {})
        for coord in sorted(set(old) | set(new)):
            if old.get(coord) != new.get(coord):
                changes.append({"source": f"{sheet}!{coord}", "before": old.get(coord), "after": new.get(coord)})
    revision = catalog["workbook_sha256"]
    directory = root / "revisions" / revision
    directory.mkdir(parents=True, exist_ok=True)
    if not (directory / "catalog.json").exists():
        write_json(directory / "catalog.json", catalog)
        shutil.copy2(path, directory / "source.xlsx")
        (directory / "walls.idf").write_text(catalog["wall_idf_text"])
    report = {"revision": revision, "previous_revision": previous["workbook_sha256"] if previous else None,
              "changed_cells": changes, "issues": catalog["issues"], "imported_at": datetime.now(timezone.utc).isoformat(),
              "mapping_policy": "Reviewed mappings are separate and never overwritten. Changed active physical mappings are rejected until reviewed."}
    write_json(directory / "import_report.json", report)
    write_json(current, {"revision": revision})
    return report


def load_catalog(root: Path = CATALOG_ROOT) -> dict:
    pointer = json.loads((root / "current.json").read_text())
    return json.loads((root / "revisions" / pointer["revision"] / "catalog.json").read_text())

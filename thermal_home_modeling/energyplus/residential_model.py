"""Complete, explicit one-zone residential V0 epJSON; no commercial template fallback."""
import json
import math
from pathlib import Path

from thermal_home_modeling.catalogs.workbook import CATALOG_ROOT
from thermal_home_modeling.errors import ConfigurationError
from thermal_home_modeling.models.residential import ResidentialInput
from thermal_home_modeling.utilities.files import fingerprint
from thermal_home_modeling.weather.discovery import read_epw_location

LOAD_VARIABLES = {
    "heating_load_j": "Zone Ideal Loads Zone Total Heating Energy",
    "cooling_load_j": "Zone Ideal Loads Zone Total Cooling Energy",
}
PARSER_CONFIG = {"variables": LOAD_VARIABLES, "units": "J", "frequency": "Run Period", "key": "IDEAL LOADS", "revision": 1}


def load_v0_configuration():
    return (json.loads((CATALOG_ROOT / "residential_mappings.json").read_text()),
            json.loads((CATALOG_ROOT / "residential_v0_assumptions.json").read_text()))


def validate_input(case, catalog, mappings):
    bindings = mappings.get("weather_bindings", {mappings["active_city"]: mappings["weather_binding"]})
    if case.city_id not in bindings:
        raise ConfigurationError(f"{case.city_id}: catalog only; no reviewed residential weather/location mapping")
    houses = mappings.get("house_properties", {mappings["active_house"]: mappings["supported_house_properties"]})
    if case.house_id not in houses:
        raise ConfigurationError(f"{case.house_id}: catalog only; geometry mapping pending")
    house = catalog["houses"][case.house_id]["value"]
    if any(house[key] != val for key, val in houses[case.house_id].items()):
        raise ConfigurationError("House catalog properties changed; review the V0 geometry mapping")
    if case.wall_id not in mappings["reviewed_wall_hashes"]:
        raise ConfigurationError(f"{case.wall_id}: catalog only; wall mapping not activated")
    wall = catalog["wall_assemblies"][case.wall_id]["value"]
    if fingerprint(wall["mapping"]) != mappings["reviewed_wall_hashes"][case.wall_id]:
        raise ConfigurationError("Wall physical definition changed in catalog; reviewed mappings were not overwritten. Engineering review required.")
    if case.city_id not in wall["eligible_cities"]:
        raise ConfigurationError(f"{case.wall_id} is not eligible for {case.city_id} under workbook rules")
    if (case.attic_r_ip not in catalog["locations"][case.city_id]["value"]["attic_levels_r_ip"]
            or case.attic_r_ip not in mappings["active_attic_levels"]):
        raise ConfigurationError("Attic selection is not both city-eligible and implemented")
    if not math.isfinite(case.orientation_deg) or case.orientation_deg not in range(0, 360, 45):
        raise ConfigurationError("Residential orientation must be a 45-degree increment in [0, 360)")
    location = read_epw_location(case.weather_file)
    binding = bindings[case.city_id]
    for key in ("station_id", "station_name", "region", "country"):
        if getattr(location, key) != binding[key]:
            raise ConfigurationError(f"Weather does not match the reviewed {case.city_id} station: {key}")
    for key in ("latitude", "longitude"):
        if abs(getattr(location, key) - binding[key]) > 0.01:
            raise ConfigurationError("Weather coordinates differ from the reviewed station")
    return location


def generate_residential_model(case: ResidentialInput, catalog: dict, mappings: dict, assumptions: dict, version: str) -> dict:
    weather = validate_input(case, catalog, mappings)
    g, o = assumptions["geometry"], assumptions["operation"]
    length, width, height = (g[key] for key in ("length_m", "width_m", "height_m"))
    if case.house_id != mappings["active_house"]:
        # Additional one-story sizes retain the explicitly provisional V0 aspect
        # ratio and height; floor area comes from the reviewed workbook mapping.
        area = catalog["houses"][case.house_id]["value"]["floor_area_ft2"] * 0.09290304
        length, width = math.sqrt(area * length / width), math.sqrt(area * width / length)
    if not math.isclose(length * width, catalog["houses"][case.house_id]["value"]["floor_area_ft2"] * 0.09290304):
        raise ConfigurationError("Provisional footprint area disagrees with the selected house")
    zone = "Residential Zone"
    model = {}
    def add(kind, name, **fields):
        model.setdefault(kind, {})[name] = fields
    add("Version", "Version", version_identifier=".".join(version.split("-")[0].split(".")[:2]))
    add("Building", "Residential V0", north_axis=float(case.orientation_deg), terrain="Suburbs",
        loads_convergence_tolerance_value=0.04, temperature_convergence_tolerance_value=0.4,
        solar_distribution="FullExterior", maximum_number_of_warmup_days=50, minimum_number_of_warmup_days=6)
    add("SimulationControl", "Control", do_zone_sizing_calculation="No", do_system_sizing_calculation="No",
        do_plant_sizing_calculation="No", run_simulation_for_sizing_periods="No", run_simulation_for_weather_file_run_periods="Yes")
    add("Timestep", "Timestep", number_of_timesteps_per_hour=assumptions["simulation"]["timestep_per_hour"])
    add("HeatBalanceAlgorithm", "Algorithm", algorithm="ConductionTransferFunction")
    add("SurfaceConvectionAlgorithm:Inside", "Inside", algorithm="TARP")
    add("SurfaceConvectionAlgorithm:Outside", "Outside", algorithm="DOE-2")
    add("Site:Location", weather.station_name, **weather.site_location())
    add("RunPeriod", "Annual", begin_month=1, begin_day_of_month=1, begin_year=2001,
        end_month=12, end_day_of_month=31, end_year=2001, day_of_week_for_start_day="Monday",
        use_weather_file_holidays_and_special_days="No", use_weather_file_daylight_saving_period="No")
    add("GlobalGeometryRules", "Geometry", starting_vertex_position="UpperLeftCorner", vertex_entry_direction="Counterclockwise", coordinate_system="Relative")
    add("Zone", zone, direction_of_relative_north=0, x_origin=0, y_origin=0, z_origin=0,
        type=1, multiplier=1, ceiling_height=height, volume=length*width*height, floor_area=length*width)
    wall = catalog["wall_assemblies"][case.wall_id]["value"]["mapping"]
    for name, material in wall["materials"].items():
        add(material["object_type"], name, **material["properties"])
    def construction(name, layers):
        add("Construction", name, **{"outside_layer" if i == 0 else f"layer_{i+1}": layer for i, layer in enumerate(layers)})
    construction(wall["construction_name"], wall["layers"])
    roof = assumptions["roof"]
    add("Material", "Roof deck", roughness="MediumRough", thickness=roof["deck_thickness_m"],
        conductivity=roof["deck_conductivity_w_mk"], density=roof["deck_density_kg_m3"], specific_heat=roof["deck_specific_heat_j_kgk"])
    add("Material:NoMass", "Attic insulation", roughness="MediumRough", thermal_resistance=case.attic_r_ip * roof["r_ip_to_si"])
    construction("Roof ceiling", ["Roof deck", "Attic insulation", "Gypsum 1/2"])
    foundation = assumptions["foundation"]
    add("Material", "Slab concrete", roughness="MediumRough", thickness=foundation["slab_thickness_m"],
        conductivity=foundation["slab_conductivity_w_mk"], density=foundation["slab_density_kg_m3"], specific_heat=foundation["slab_specific_heat_j_kgk"])
    construction("Provisional slab", ["Slab concrete"])
    months = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
    add("Site:GroundTemperature:BuildingSurface", "Provisional ground", **{f"{m}_ground_temperature": foundation["ground_temperature_c"] for m in months})
    faces = {
        "South": [(0,0,height),(0,0,0),(length,0,0),(length,0,height)],
        "East": [(length,0,height),(length,0,0),(length,width,0),(length,width,height)],
        "North": [(length,width,height),(length,width,0),(0,width,0),(0,width,height)],
        "West": [(0,width,height),(0,width,0),(0,0,0),(0,0,height)],
        "Roof": [(0,width,height),(0,0,height),(length,0,height),(length,width,height)],
        "Floor": [(0,0,0),(0,width,0),(length,width,0),(length,0,0)],
    }
    for name, vertices in faces.items():
        floor, roof_face = name == "Floor", name == "Roof"
        add("BuildingSurface:Detailed", name, surface_type="Floor" if floor else "Roof" if roof_face else "Wall",
            construction_name="Provisional slab" if floor else "Roof ceiling" if roof_face else wall["construction_name"], zone_name=zone,
            outside_boundary_condition="Ground" if floor else "Outdoors", sun_exposure="NoSun" if floor else "SunExposed",
            wind_exposure="NoWind" if floor else "WindExposed", number_of_vertices=4,
            vertices=[dict(zip(("vertex_x_coordinate", "vertex_y_coordinate", "vertex_z_coordinate"), point)) for point in vertices])
    window = assumptions["windows"]
    add("WindowMaterial:SimpleGlazingSystem", "Provisional glazing", u_factor=window["u_factor_w_m2k"],
        solar_heat_gain_coefficient=window["shgc"], visible_transmittance=window["visible_transmittance"])
    construction("Provisional window", ["Provisional glazing"])
    for name in ("South", "East", "North", "West"):
        points = faces[name]
        center = [sum(p[i] for p in points)/4 for i in range(3)]
        points = [[center[i] + (p[i]-center[i])*math.sqrt(window["wwr"]) for i in range(3)] for p in points]
        add("FenestrationSurface:Detailed", f"{name} window", surface_type="Window", construction_name="Provisional window",
            building_surface_name=name, number_of_vertices=4,
            **{f"vertex_{n}_{axis}_coordinate": p[i] for n, p in enumerate(points, 1) for i, axis in enumerate("xyz")})
    add("ScheduleTypeLimits", "Fraction", lower_limit_value=0, upper_limit_value=1, numeric_type="Continuous")
    add("ScheduleTypeLimits", "Temperature", numeric_type="Continuous", unit_type="Temperature")
    add("ScheduleTypeLimits", "Control", lower_limit_value=0, upper_limit_value=4, numeric_type="Discrete")
    add("ScheduleTypeLimits", "Activity", lower_limit_value=0, numeric_type="Continuous", unit_type="ActivityLevel")
    for name, kind, val in (("Always on", "Fraction", 1), ("Heating setpoint", "Temperature", o["heating_setpoint_c"]),
                            ("Cooling setpoint", "Temperature", o["cooling_setpoint_c"]), ("Dual control", "Control", 4),
                            ("Activity", "Activity", o["activity_w_per_person"])):
        add("Schedule:Constant", name, schedule_type_limits_name=kind, hourly_value=val)
    add("Schedule:Compact", "Occupancy", schedule_type_limits_name="Fraction", data=[{"field": field} for field in
        ["Through: 12/31", "For: AllDays", "Until: 07:00", o["occupancy_night_fraction"], "Until: 18:00", o["occupancy_day_fraction"], "Until: 24:00", o["occupancy_night_fraction"]]])
    add("People", "Residents", zone_or_zonelist_or_space_or_spacelist_name=zone, number_of_people_schedule_name="Occupancy",
        number_of_people_calculation_method="People", number_of_people=o["people"], fraction_radiant=0.3,
        activity_level_schedule_name="Activity")
    add("Lights", "Residential lights", zone_or_zonelist_or_space_or_spacelist_name=zone, schedule_name="Occupancy",
        design_level_calculation_method="Watts/Area", watts_per_floor_area=o["lights_w_m2"], fraction_radiant=0.4, fraction_visible=0.2)
    add("ElectricEquipment", "Residential appliances", zone_or_zonelist_or_space_or_spacelist_name=zone, schedule_name="Occupancy",
        design_level_calculation_method="Watts/Area", watts_per_floor_area=o["equipment_w_m2"], fraction_latent=0.0, fraction_radiant=0.3, fraction_lost=0)
    add("ZoneInfiltration:DesignFlowRate", "Residential infiltration", zone_or_zonelist_or_space_or_spacelist_name=zone,
        schedule_name="Always on", design_flow_rate_calculation_method="AirChanges/Hour", air_changes_per_hour=o["infiltration_ach"],
        constant_term_coefficient=1, temperature_term_coefficient=0, velocity_term_coefficient=0, velocity_squared_term_coefficient=0)
    add("ThermostatSetpoint:DualSetpoint", "Residential setpoints", heating_setpoint_temperature_schedule_name="Heating setpoint", cooling_setpoint_temperature_schedule_name="Cooling setpoint")
    add("ZoneControl:Thermostat", "Residential thermostat", zone_or_zonelist_name=zone, control_type_schedule_name="Dual control",
        control_1_object_type="ThermostatSetpoint:DualSetpoint", control_1_name="Residential setpoints")
    hvac = assumptions["hvac"]
    add("ZoneHVAC:IdealLoadsAirSystem", "Ideal Loads", availability_schedule_name="Always on", zone_supply_air_node_name="Supply node",
        maximum_heating_supply_air_temperature=hvac["max_heating_supply_c"], minimum_cooling_supply_air_temperature=hvac["min_cooling_supply_c"],
        heating_limit="NoLimit", cooling_limit="NoLimit", dehumidification_control_type="None", humidification_control_type="None",
        outdoor_air_economizer_type="NoEconomizer", heat_recovery_type="None")
    add("ZoneHVAC:EquipmentList", "Equipment", load_distribution_scheme="SequentialLoad", equipment=[{
        "zone_equipment_object_type": "ZoneHVAC:IdealLoadsAirSystem", "zone_equipment_name": "Ideal Loads",
        "zone_equipment_cooling_sequence": 1, "zone_equipment_heating_or_no_load_sequence": 1}])
    add("ZoneHVAC:EquipmentConnections", "Connections", zone_name=zone, zone_conditioning_equipment_list_name="Equipment",
        zone_air_inlet_node_or_nodelist_name="Supply node", zone_air_node_name="Zone air node", zone_return_air_node_or_nodelist_name="Return node")
    add("Output:SQLite", "SQL", option_type="SimpleAndTabular")
    add("Output:VariableDictionary", "Dictionary", key_field="IDF", sort_option="Name")
    for key, variable in LOAD_VARIABLES.items():
        for freq in ("RunPeriod", "Hourly"):
            add("Output:Variable", key+freq, key_value="*", variable_name=variable, reporting_frequency=freq)
    for variable in ("Zone Mean Air Temperature", "Zone Air Humidity Ratio", "Zone Thermostat Heating Setpoint Temperature",
                     "Zone Thermostat Cooling Setpoint Temperature", "Zone Ideal Loads Zone Sensible Cooling Energy",
                     "Zone Ideal Loads Zone Latent Cooling Energy", "Zone Ideal Loads Supply Air Temperature"):
        add("Output:Variable", variable, key_value="*", variable_name=variable, reporting_frequency="Hourly")
    return model


def symmetric_180(model: dict) -> bool:
    """Check geometry, constructions, windows and lack of exterior shading for this one-zone template."""
    if len(model.get("Zone", {})) != 1 or any(k.startswith("Shading:") for k in model):
        return False
    surfaces = model["BuildingSurface:Detailed"]
    points = [v for s in surfaces.values() for v in s["vertices"]]
    cx = (min(p["vertex_x_coordinate"] for p in points)+max(p["vertex_x_coordinate"] for p in points))/2
    cy = (min(p["vertex_y_coordinate"] for p in points)+max(p["vertex_y_coordinate"] for p in points))/2
    def signature(fields, vertices, rotate=False):
        pts = sorted((round(float(2*cx-x if rotate else x), 6), round(float(2*cy-y if rotate else y), 6), round(float(z), 6)) for x,y,z in vertices)
        return fingerprint([fields, pts])
    original, rotated = [], []
    for s in surfaces.values():
        verts = [tuple(v[k] for k in ("vertex_x_coordinate", "vertex_y_coordinate", "vertex_z_coordinate")) for v in s["vertices"]]
        props = {k:v for k,v in s.items() if k != "vertices"}
        original.append(signature(props, verts)); rotated.append(signature(props, verts, True))
    if sorted(original) != sorted(rotated):
        return False
    original, rotated = [], []
    for s in model.get("FenestrationSurface:Detailed", {}).values():
        verts = [tuple(s[f"vertex_{n}_{axis}_coordinate"] for axis in "xyz") for n in range(1,5)]
        props = {k:v for k,v in s.items() if not k.startswith("vertex_") and k != "building_surface_name"}
        original.append(signature(props, verts)); rotated.append(signature(props, verts, True))
    return sorted(original) == sorted(rotated)

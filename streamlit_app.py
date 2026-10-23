"""Read-only browser for saved Residential V0 simulations."""

import streamlit as st

from thermal_home_modeling.config.mvp_config import MVPPaths
from thermal_home_modeling.presentation.streamlit_residential import render_residential

st.set_page_config(page_title="Residential simulation results", page_icon="🏠", layout="wide")
st.title("Residential simulation results")
st.caption("Saved EnergyPlus runs · Residential V0 · annual ideal heating and cooling loads")
render_residential(MVPPaths.default())

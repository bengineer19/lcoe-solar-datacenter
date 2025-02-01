"""
Power flow model for simulating hybrid solar + storage + generator system performance.

It uses PVGIS data for solar resource assessment and models hour-by-hour battery flows & degradation 
over the lifetime of the project.
"""

import polars as pl
import pandas as pd
from pvlib import pvsystem, modelchain, location, iotools
import logging
import time
import streamlit as st
from typing import Dict
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# System constants
SYSTEM_LIFETIME_YEARS = 20
BATTERY_ROUND_TRIP_EFFICIENCY = 0.92
BATTERY_DURATION_HOURS = 4
BATTERY_DEGRADATION_PCT_PER_YEAR = 0.35 / 20  # 0.35% total over 20 years
SOLAR_DEGRADATION_PCT_PER_YEAR = 0.005  # 0.5% per year
GENERATOR_HEAT_RATE_BTU_PER_KWH = 8989.3
DC_AC_RATIO = 1.2

# PVLib configuration parameters
PVLIB_CONFIG = {
    "module_parameters": {
        "pdc0": 1,  # Normalized to 1 kW for scaling
        "gamma_pdc": -0.004,  # Temperature coefficient (%/°C)
    },
    "temperature_model_parameters": {
        "a": -3.56,  # Wind speed coefficient (°C/(W/m2))
        "b": -0.075,  # Wind speed coefficient (°C/(W/m2)/(m/s))
        "deltaT": 3,  # Temperature difference between cell and module back (°C)
    },
}

@st.cache_data
def get_solar_ac_dataframe(
    latitude: float,
    longitude: float,
    system_type: str = "single-axis",
    surface_tilt: float = 20,
    surface_azimuth: float = 180,
) -> pd.DataFrame:
    """
    Calculate the AC output profile of a PV system based on location and configuration.

    Uses PVGIS typical meteorological year (TMY) data to simulate solar PV performance,
    accounting for temperature effects, solar angle, and system losses. The output is
    normalized to 1 MW-DC of installed capacity.

    Args:
        latitude: Site latitude in decimal degrees (positive for northern hemisphere)
        longitude: Site longitude in decimal degrees (positive for eastern hemisphere)
        system_type: Mounting system type ('fixed-tilt' or 'single-axis')
        surface_tilt: Panel tilt angle in degrees from horizontal (fixed-tilt only)
        surface_azimuth: Panel azimuth angle in degrees clockwise from north (fixed-tilt only)

    Returns:
        DataFrame containing hourly AC output values normalized to 1 MW-DC capacity

    Raises:
        ValueError: If system_type is not 'fixed-tilt' or 'single-axis'
    """
    logger.info(
        f"Starting solar AC calculation for {latitude}, {longitude} with {system_type} system"
    )

    # Create mount based on system type
    if system_type.lower() == "fixed-tilt":
        mount = pvsystem.FixedMount(
            surface_tilt=surface_tilt, surface_azimuth=surface_azimuth
        )
    elif system_type.lower() == "single-axis":
        mount = pvsystem.SingleAxisTrackerMount()
    else:
        raise ValueError("system_type must be either 'fixed-tilt' or 'single-axis'")

    # Create array and location objects
    array = pvsystem.Array(mount, **PVLIB_CONFIG)
    site = location.Location(latitude, longitude)

    # Create PV system with normalized 1 MW rating
    pv_system = pvsystem.PVSystem(
        arrays=[array],
        inverter_parameters={"pdc0": PVLIB_CONFIG["module_parameters"]["pdc0"]},
    )

    # Configure model chain with physical AOI model
    model = modelchain.ModelChain(
        pv_system, site, aoi_model="physical", spectral_model="no_loss"
    )

    # Fetch and process weather data
    weather_start = time.time()
    weather_data = iotools.get_pvgis_tmy(latitude, longitude)[0]
    logger.info(f"Weather data fetch took {(time.time() - weather_start)*1000:.1f} ms")

    # Run performance model
    model_start = time.time()
    model.run_model(weather_data)
    logger.info(f"Model run took {(time.time() - model_start)*1000:.1f} ms")

    return model.results.ac


def simulate_battery_operation(
    df: pd.DataFrame,
    battery_capacity_mwh: float,
    initial_battery_charge: float,
    generator_capacity: float,
    load_mw: float,
    operating_year: int,
) -> pd.DataFrame:
    """
    Simulate battery, solar and generator operation for one year of system lifetime.

    Models the charging and discharging of the battery storage system and generator
    operation to meet the datacenter load, accounting for battery efficiency,
    degradation, and power limits.

    Args:
        df: DataFrame containing solar generation profile
        battery_capacity_mwh: Nameplate battery energy capacity in MWh
        initial_battery_charge: Initial battery state of charge in MWh
        generator_capacity: Generator power capacity in MW
        load: Constant load power in MW
        operating_year: Current year of operation (for degradation)

    Returns:
        DataFrame with added columns for battery state, energy flows, and generator output
    """
    # Calculate battery parameters with degradation
    battery_power_mw = battery_capacity_mwh / BATTERY_DURATION_HOURS
    degraded_capacity_mwh = battery_capacity_mwh * (
        1 - BATTERY_DEGRADATION_PCT_PER_YEAR * (operating_year - 1)
    )
    battery_state_mwh = initial_battery_charge

    # Initialize result lists
    curtailed_solar_mwh = []
    unmet_load_mwh = []
    battery_state_history = []
    battery_charge_mwh = []
    battery_discharge_mwh = []
    generator_output_mwh = []

    # Simulate each timestep
    for _, timestep in df.iterrows():
        solar_generation_mw = timestep["scaled_solar_generation_mw"]

        if solar_generation_mw > load_mw:
            # Excess solar case
            excess_power_mw = solar_generation_mw - load_mw
            available_storage_mwh = degraded_capacity_mwh - battery_state_mwh
            stored_energy_mwh = min(
                min(excess_power_mw, battery_power_mw), available_storage_mwh
            )
            curtailed_power_mwh = excess_power_mw - stored_energy_mwh
            battery_state_mwh += stored_energy_mwh * BATTERY_ROUND_TRIP_EFFICIENCY**0.5

            # Record results for excess case
            curtailed_solar_mwh.append(curtailed_power_mwh)
            unmet_load_mwh.append(0.0)
            battery_state_history.append(battery_state_mwh)
            battery_charge_mwh.append(stored_energy_mwh)
            battery_discharge_mwh.append(0.0)
            generator_output_mwh.append(0.0)
        else:
            # Power deficit case
            deficit_mw = load_mw - solar_generation_mw
            max_discharge_mwh = min(
                battery_power_mw,
                min(deficit_mw / BATTERY_ROUND_TRIP_EFFICIENCY**0.5, battery_state_mwh),
            )
            battery_state_mwh -= max_discharge_mwh
            discharge_power_mw = max_discharge_mwh * BATTERY_ROUND_TRIP_EFFICIENCY**0.5
            remaining_deficit_mw = deficit_mw - discharge_power_mw
            generator_power_mw = min(remaining_deficit_mw, generator_capacity)
            unmet_power_mw = remaining_deficit_mw - generator_power_mw

            # Record results for deficit case
            curtailed_solar_mwh.append(0.0)
            unmet_load_mwh.append(unmet_power_mw)
            battery_state_history.append(battery_state_mwh)
            battery_charge_mwh.append(0.0)
            battery_discharge_mwh.append(discharge_power_mw)
            generator_output_mwh.append(generator_power_mw)

    # Add results to DataFrame
    df["battery_state_mwh"] = battery_state_history
    df["battery_charge_mwh"] = battery_charge_mwh
    df["battery_discharge_mwh"] = battery_discharge_mwh
    df["curtailed_solar_mwh"] = curtailed_solar_mwh
    df["generator_output_mwh"] = generator_output_mwh
    df["unmet_load_mwh"] = unmet_load_mwh
    df['load_served_mwh'] = load_mw - df['unmet_load_mwh']

    return df


def scale_solar_generation(
    df: pd.DataFrame, installed_capacity_mw: float, operating_year: int
) -> pd.DataFrame:
    """
    Scale the normalized solar generation profile by installed capacity and degradation.

    Args:
        df: DataFrame containing normalized solar generation
        installed_capacity_mw: Installed solar capacity in MW-DC
        operating_year: Current year of operation (for degradation)

    Returns:
        DataFrame with scaled generation values
    """
    degradation_factor = 1 - SOLAR_DEGRADATION_PCT_PER_YEAR * (operating_year - 1)
    ac_capacity_mw = installed_capacity_mw / DC_AC_RATIO
    df["scaled_solar_generation_mw"] = df["p_mp"] * ac_capacity_mw * degradation_factor
    return df

@st.cache_data
def simulate_system(
    latitude: float,
    longitude: float,
    solar_ac_dataframe: pd.DataFrame,
    solar_capacity_mw: float,
    battery_power_mw: float,
    generator_capacity_mw: float,
    data_center_demand_mw: float = 100,
) -> pl.DataFrame:
    """
    Simulate complete system performance over its lifetime.

    Performs a year-by-year simulation of the hybrid power system, accounting for
    solar resource variation, battery operation, and system degradation.

    Args:
        latitude: Site latitude in decimal degrees
        longitude: Site longitude in decimal degrees
        solar_capacity_mw: Solar PV capacity in MW-DC
        battery_power_mw: Battery power capacity in MW
        generator_capacity_mw: Generator capacity in MW

    Returns:
        Polars DataFrame containing annual performance metrics for the system
    """
    logger.info(
        f"Starting simulation for lat={latitude}, lon={longitude}, "
        f"solar={solar_capacity_mw} MW, battery={battery_power_mw} MW/"
        f"{battery_power_mw * BATTERY_DURATION_HOURS} MWh, generator={generator_capacity_mw} MW"
    )

    # Calculate battery energy capacity
    battery_capacity_mwh = battery_power_mw * BATTERY_DURATION_HOURS

    # Get normalized solar generation profile
    solar_generation_df = (
        solar_ac_dataframe
        .reset_index()
        .rename(columns={"index": "time(UTC)", "value": "p_mp"})
    )
    solar_generation_df["time(UTC)"] = pd.to_datetime(solar_generation_df["time(UTC)"])

    annual_results = []
    for operating_year in range(1, SYSTEM_LIFETIME_YEARS + 1):
        logger.info(f"Simulating year {operating_year} of {SYSTEM_LIFETIME_YEARS}")

        # Scale solar generation for current year
        scaled_df = scale_solar_generation(
            solar_generation_df.copy(), solar_capacity_mw, operating_year
        )

        # Set initial battery charge (empty in final year)
        initial_charge = 0 if operating_year == 0 else battery_capacity_mwh

        # Simulate battery and generator operation
        result_df = simulate_battery_operation(
            scaled_df,
            battery_capacity_mwh,
            initial_charge,
            generator_capacity_mw,
            data_center_demand_mw,
            operating_year,
        )
        if operating_year == 1:
            # Slice 24h * 7 days of data from the middle of the year
            sample_week_df = result_df[result_df['time(UTC)'].dt.dayofyear.isin(range(182, 189))]
            sample_week_df = sample_week_df.reset_index(drop=True)
        solar_mwh_raw_tot = result_df["scaled_solar_generation_mw"].sum()
        solar_mwh_curtailed_tot = result_df["curtailed_solar_mwh"].sum()
        # Append results for the current year
        annual_results.append(
            {
                "system_spec": f"{int(solar_capacity_mw)}MW | {int(battery_power_mw)}MW | {int(generator_capacity_mw)}MW",
                "Operating Year": operating_year,
                "Solar Output - Raw (MWh)": round(solar_mwh_raw_tot),
                "Solar Output - Curtailed (MWh)": round(solar_mwh_curtailed_tot),
                "Solar Output - Net (MWh)": round(
                    solar_mwh_raw_tot - solar_mwh_curtailed_tot
                ),
                "BESS charged (MWh)": round(result_df["battery_charge_mwh"].sum()),
                "BESS discharged (MWh)": round(
                    result_df["battery_discharge_mwh"].sum()
                ),
                "Generator Output (MWh)": round(
                    result_df["generator_output_mwh"].sum()
                ),
                "Generator Fuel Input (MMBtu)": round(
                    result_df["generator_output_mwh"].sum()
                    * GENERATOR_HEAT_RATE_BTU_PER_KWH
                    / 1000
                ),
                # This method of calculating load served produces sliiightly different results to the original,
                # but I think this may be more correct.
                "Load Served (MWh)": round(
                    data_center_demand_mw * 8760 - result_df["unmet_load_mwh"].sum()
                ),
            }
        )

    logger.info("Simulation completed successfully")
    return {
        "annual_results": pd.DataFrame(annual_results),
        "daily_sample": sample_week_df
    }

def calculate_energy_mix(simulation_data: pd.DataFrame) -> Dict[str, float]:
    """Calculate lifetime energy mix from simulation data."""
    solar_gen_net_twh = simulation_data['Solar Output - Net (MWh)'].sum() / 1_000_000
    solar_to_bess_twh = simulation_data['BESS charged (MWh)'].sum() / 1_000_000
    bess_to_load_twh = simulation_data['BESS discharged (MWh)'].sum() / 1_000_000
    generator_twh = simulation_data['Generator Output (MWh)'].sum() / 1_000_000
    total_load_twh = simulation_data['Load Served (MWh)'].sum() / 1_000_000
    
    renewable_percentage = 100 * (1 - generator_twh / total_load_twh)
    
    return {
        'solar_gen_net_twh': solar_gen_net_twh,
        'solar_to_load_twh': solar_gen_net_twh - solar_to_bess_twh,
        'bess_to_load_twh': bess_to_load_twh,
        'generator_twh': generator_twh,
        'total_generation_twh': solar_gen_net_twh + bess_to_load_twh + generator_twh,
        'total_load_twh': total_load_twh,
        'renewable_percentage': renewable_percentage
    }


if __name__ == "__main__":
    # Example simulation for El Paso, TX
    EXAMPLE_CONFIG = {
        "latitude": 31.9,
        "longitude": -106.2,
        "solar_capacity_mw": 500,
        "battery_power_mw": 100,
        "generator_capacity_mw": 100,
        "data_center_demand_mw": 100
    }

    solar_ac_dataframe = get_solar_ac_dataframe(EXAMPLE_CONFIG["latitude"], EXAMPLE_CONFIG["longitude"])
    results = simulate_system(**EXAMPLE_CONFIG, solar_ac_dataframe=solar_ac_dataframe)
    results.write_csv("output_20_yrs.csv")
    print(results)

"""
creates load profiles for a number of building types with oemof demandlib and saves them to an Excel file
weather data of Berlin-Dahlem is used for 2021/22.
For other years new weather data must be placed in data folder and the holidays need to be defined for the year.
"""

import os
import warnings
import pandas as pd
from logic.utilities import create_folder
import matplotlib.pyplot as plt
import datetime
import demandlib.bdew as bdew

warnings.simplefilter(action='ignore', category=FutureWarning)  # ignore pandas future warnings caused by demandlib


def generate_building_loadprofiles(project_name, bui_dict, year, p_results=None):
    """
    :param bui_dict: dict contains building type and demand data
    :param year: int specify year for which the load profiles should be calculated
    :param p_results: str path where result Excel file will be saved
    :param project_name: str project name will be used for result path
    :return:
    """
    dict_holidays = {}
    # create folder where load profiles are saved
    if p_results is not None:
        p_results = os.path.abspath(p_results)
    else:
        p_results = os.path.abspath(os.path.join('results', project_name))

    dict_holidays[2021] = {
        datetime.date(2021, 1, 1): "Neujahr",
        datetime.date(2021, 3, 8): "Frauentag",
        datetime.date(2021, 4, 2): "Karfreitag",
        datetime.date(2021, 4, 5): "Ostermontag",
        datetime.date(2021, 5, 1): "Tag der Arbeit",
        datetime.date(2021, 5, 13): "Himmelfahrt",
        datetime.date(2021, 5, 23): "Pfingsten",
        datetime.date(2021, 10, 3): "Tag der deutschen Einheit",
        datetime.date(2021, 12, 25): "erster Weihnachtsfeiertag",
        datetime.date(2021, 12, 26): "zweiter Weihnachtsfeiertag",
    }
    dict_holidays[2022] = {
        datetime.date(2021, 1, 1): "Neujahr",
        datetime.date(2021, 3, 8): "Frauentag",
        datetime.date(2021, 4, 7): "Karfreitag",
        datetime.date(2021, 4, 10): "Ostermontag",
        datetime.date(2021, 5, 1): "Tag der Arbeit",
        datetime.date(2021, 5, 18): "Himmelfahrt",
        datetime.date(2021, 5, 29): "Pfingsten",
        datetime.date(2021, 10, 3): "Tag der deutschen Einheit",
        datetime.date(2021, 12, 25): "erster Weihnachtsfeiertag",
        datetime.date(2021, 12, 26): "zweiter Weihnachtsfeiertag",
    }

    # --------- HEAT LOAD PROFILES ----------- #
    # read weatherdata for specified year
    try:
        df_temp = pd.read_csv(os.path.abspath(os.path.join('profile_generation', 'data', 'weatherdata_'+str(year)+'.csv')))
    except ValueError:
        raise ValueError("weather data for year {} not available".format(str(year)))

    temperature = df_temp["TT_TU"]

    # Create DataFrame for year
    df_demand = pd.DataFrame(
        index=pd.date_range(
            datetime.datetime(year, 1, 1, 0), periods=8760, freq="H"))

    for key in bui_dict:
        df_demand[key+'_h.fix'] = bdew.HeatBuilding(
            df_demand.index,
            holidays=dict_holidays[year],
            temperature=temperature,
            shlp_type=bui_dict[key]['bui_type'],
            building_class=bui_dict[key]['bui_class'],
            wind_class=bui_dict[key]['wind_class'],
            annual_heat_demand=1,
            ww_incl=bui_dict[key]['ww_incl'],
            name=key,
        ).get_bdew_profile()

    # --------- ELECTRIC LOAD PROFILES ----------- #
    for key in bui_dict:

        # I am not quite sure if h0_dyn should be False for other profiles than h0
        if bui_dict[key]['el_profile'] == 'h0':
            ann_el_demand_per_sector = {
                bui_dict[key]['el_profile']: 1,
                "h0_dyn": True,
            }
        else:
            ann_el_demand_per_sector = {
                bui_dict[key]['el_profile']: 1,
                "h0_dyn": False,
            }

        # read standard load profiles
        e_slp = bdew.ElecSlp(year, holidays=dict_holidays[year])

        # multiply given annual demand with timeseries (1 = normalized)
        elec_demand = e_slp.get_profile(ann_el_demand_per_sector)

        # Resample 15-minute values to hourly values.
        df_demand[key+'_el.fix'] = elec_demand.resample("H").mean()

    # reorder df columns alphabetically
    df_demand = df_demand.reindex(sorted(df_demand.columns), axis=1)

    p_results = os.path.join(p_results, 'loadprofiles')

    create_folder(p_results)
    df_demand.to_excel(os.path.join(p_results, 'loadprofiles.xlsx'))

    df_demand.plot(subplots=True)
    plt.savefig(os.path.join(p_results, 'loadprofiles.pdf'))
    plt.close()

    print("Saved load profiles to: {}".format(os.path.join(p_results, 'loadprofiles.xlsx')))

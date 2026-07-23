"""
This script allows you to generate an Excel-File with normalized heating and electricity load profiles for several
buildings to copy into your oemof energy system Excel-Sheet as time series.
The profiles are normalized profiles from oemof demandlib based on standard load profiles by the BDEW.
There are several normalized load profiles for different types of buildings.
To generate a yearly load profile you need a temperature weather time series for the year and have to define
the holidays of that year. Two years 2021 and 2022 are already built in using weather profiles of Berlin-Dahlem
located in ./profile_generation/data. Holidays are set per year in the generate_building_loadprofiles function.
When adding or replacing weather profiles please ensure that it contains hourly data and has exactly 8760 entries.

The building_class parameter was defined by studies of the TU Muenchen (Gutachten, Festlegung von Standardlastprofilen Haushalte
und Gewerbe für BGW und VKU, Dr.-Ing. Bernd Geiger, Veröffentlicht in BGW/VKU Praxisinformation P 2006/8, Berlin, 2006)

                    Altbauanteil            mittlere Anteile von
Baualtersklasse     von      bis            Altbau      Neubau
1                   85,5%    90,5%          88,0%       12,0%
2                   80,5%    85,5%          83,0%       17,0%
3                   75,5%    80,5%          78,0%       22,0%
4                   70,5%    75,5%          73,0%       27,0%
5                   65,5%    70,5%          68,0%       32,0%
6                   60,5%    65,5%          63,0%       37,0%
7                   55,5%    60,5%          58,0%       42,0%
8                   50,5%    55,5%          53,0%       47,0%
9                   45,5%    50,5%          48,0%       52,0%
10                  40,5%    45,5%          43,0%       57,0%
11                                          75,0%       25,0%
Source: https://www.stadtwerke-bramsche.de/ceasy/resource/?id=1283&download=1

possible building types are:
Residential: EFH, MFH (single family home, multifamily home)
Nonresidential:
GMF - haushaltsähnliche Gewerbebetriebe
GPD - Papier und Druck
GHD - Summenlastprofil: Gewerbe, Handel, Dienstleistung
GWA - Waeschereien
GGB - Gartenbau
GKO - Gebietskörperschaften, Kreditinstitute und Versicherungen, Organisationen ohne Erwerbszw. & öffentl. Einr.
GBD - sonstige betriebliche Dienstleistungen
GBA - Baeckereien
GMK - Metall, Kfz.
GBH - Beherbergung
GGA - Gaststätten
GHA - Einzelhandel, Großhandel

For the electric profiles there are different BDEW categories:
h0 - Haushalt
g0 - Gewerbe allgemein
g1 - Gewerbe werktags 8-18 Uhr
g2 - Gewerbe mit starkem bis überwiegendem Verbrauch in den Abendstunden
g3 - Gewerbe durchlaufend
g4 - Laden/ Friseur
g5 - Baeckerei mit Backstube
g6 - Wochenendbetrieb
l0 - Landwirtschaftsbetriebe
l1 - Landwirtschaftsbetriebe mit Milchwirtschaft/Nebenerwerbs-Tierzucht
l2 - Uebrige Landwirtschaftsbetriebe
"""

from profile_generation.profile_generation import generate_building_loadprofiles

# define for what buildings you want to generate load profiles
# bui_type: the building type needed to choose the correct bdew profile
# bui_class: choose between 1-11 ONLY for type EFH and MFH! All other types get bui_class = 0!
# el_profile: choose BDEW electric profile (see above)
# wind_class: wind classification for building location (0=not windy or 1=windy)
# ww_incl: True if the hot water demand should be included in the heat load profile

bui_dict = {
    'building1': {
        'bui_type': 'efh',
        'bui_class': 10,
        'el_profile': 'h0',
        'wind_class': 1,
        'ww_incl': True
    },
    'building2': {
        'bui_type': 'gmk',
        'bui_class': 0,
        'el_profile': 'g3',
        'wind_class': 1,
        'ww_incl': True
    },
    'building3': {
        'bui_type': 'gha',
        'bui_class': 0,
        'el_profile': 'g4',
        'wind_class': 1,
        'ww_incl': True
    }
}

generate_building_loadprofiles(project_name='example', bui_dict=bui_dict,
                               year=2022)

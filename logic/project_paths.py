import os
from pathlib import Path

# logic/project_paths.py
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]   # folder containing data/, logic/, results/, oemof-hri.py

def get_data_dir() -> Path:
    return BASE_DIR / "data"

def get_templates_dir() -> Path:
    # Put templates in data/ (or change to BASE_DIR / "templates" if that’s where you store them)
    return get_data_dir()

TEMPLATE_CEN_FILE = str(get_templates_dir() / "system/system_template_cen.xlsx")
TEMPLATE_DEC_FILE = str(get_templates_dir() / "system/system_template_dec.xlsx")
PV_COP_FILE = str(get_templates_dir() / "system/PV_COP.xlsx")

def input_done_flag_path() -> str:
    return str(get_data_dir() / "input_done.json")

def get_projects_root() -> str:
    return str(get_data_dir() / "projects")

def get_project_dir(project_name: str) -> str:
    return str(Path(get_projects_root()) / project_name)

def get_scenario_dir_for_project(project_name: str) -> str:
    return str(Path(get_project_dir(project_name)) / "scenarios")

def ensure_project_dirs(project_name: str):
    root = get_projects_root()
    proj_dir = get_project_dir(project_name)
    scen_dir = get_scenario_dir_for_project(project_name)
    os.makedirs(root, exist_ok=True)
    os.makedirs(proj_dir, exist_ok=True)
    os.makedirs(scen_dir, exist_ok=True)
    return proj_dir, scen_dir

def project_meta_path(project_name: str) -> str:
    return os.path.join(get_project_dir(project_name), "project_meta.json")

def get_results_root() -> str:
    return str(BASE_DIR / "results")

def get_project_results_dir(project_name: str) -> str:
    return str(Path(get_results_root()) / project_name)

def get_scenario_results_dir(project_name: str, scenario_name: str) -> str:
    return str(Path(get_project_results_dir(project_name)) / scenario_name)
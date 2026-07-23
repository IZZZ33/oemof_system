from logic import API
import pandas as pd
import subprocess
import sys
import os
import time
from pathlib import Path
from datetime import datetime
import shutil
import glob
import json
from logic.utilities import create_folder

from logic.project_paths import (
    get_scenario_dir_for_project,
    input_done_flag_path,
    get_results_root,
    get_project_results_dir,
    get_scenario_results_dir,
)
from logic.project_run_state import project_input_fingerprint, write_run_status

def list_scenarios_for_project(project_name: str):
    scen_dir = get_scenario_dir_for_project(project_name)
    if not os.path.exists(scen_dir):
        return []
    return [f for f in os.listdir(scen_dir) if f.endswith(".xlsx")]

def archive_project_results(project_name: str) -> None:
    """Move existing results/<project_name> to results/old/<project_name>_delete_<timestamp>/."""
    project_res = Path(get_project_results_dir(project_name))
    if not project_res.exists():
        return
    # skip if empty folder
    if not any(project_res.iterdir()):
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    old_root = Path(get_results_root()) / "old"
    old_root.mkdir(parents=True, exist_ok=True)

    target = old_root / f"{project_name}_delete_{ts}"
    shutil.move(str(project_res), str(target))
    print(f"Archived old results: {project_res} -> {target}")



if __name__ == '__main__':

    p_input = subprocess.Popen([
        sys.executable, "-m", "streamlit", "run", "logic/data_input.py"
    ], shell=False)

    flag_file_input = input_done_flag_path()
    print("Waiting for project submissions. The input webpage remains available during simulations.")

    while True:
        if not os.path.exists(flag_file_input):
            if p_input.poll() is not None:
                raise RuntimeError("The Streamlit input webpage stopped unexpectedly.")
            time.sleep(1)
            continue

        try:
            with open(flag_file_input, "r", encoding="utf-8") as f:
                flag_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # The Streamlit process may still be finishing its write.
            time.sleep(0.2)
            continue

        try:
            os.remove(flag_file_input)
        except OSError as exc:
            print("Warning: could not consume input flag:", exc)
            time.sleep(1)
            continue

        submitted_project = flag_data.get("project_name")
        if not submitted_project:
            print(f"Ignoring invalid submit flag: {flag_data}")
            continue

        input_fingerprint = flag_data.get("input_fingerprint") or project_input_fingerprint(submitted_project)
        print(f"Starting submitted project: {submitted_project!r}")

        try:
            archive_project_results(submitted_project)
            scenarios = list_scenarios_for_project(submitted_project)
            if not scenarios:
                raise RuntimeError("No scenario files were found for this project.")

            scenario_dir = get_scenario_dir_for_project(submitted_project)
            for index, file in enumerate(scenarios, start=1):
                scenario_name = file[:-5]
                scenario_path = os.path.join(scenario_dir, file)
                write_run_status(
                    submitted_project,
                    "running",
                    input_fingerprint=input_fingerprint,
                    scenario=scenario_name,
                    scenario_index=index,
                    scenario_count=len(scenarios),
                )

                p_results = get_scenario_results_dir(submitted_project, scenario_name)
                create_folder(p_results)
                print(f"Running project '{submitted_project}', scenario: {scenario_name}")
                API.OEMOFSim(
                    project_name=submitted_project,
                    p_results=p_results,
                    start_date="2010-01-01 00:00:00",
                    time_steps=8760,
                    time_step_freq="h",
                    oemof_input_file=scenario_path,
                    emission_limit=1e12,
                    save_sim=True,
                    skip_sim=False,
                    exclude_draw_nodes=["_excess", "_shortage"]
                )

            write_run_status(
                submitted_project,
                "completed",
                input_fingerprint=input_fingerprint,
                scenario_count=len(scenarios),
            )
            print(f"Completed project: {submitted_project!r}")
        except Exception as exc:
            write_run_status(
                submitted_project,
                "failed",
                input_fingerprint=input_fingerprint,
                error=str(exc),
            )
            print(f"Simulation failed for project {submitted_project!r}: {exc}")

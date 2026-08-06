# District Planning Tool — User Guide

This tool helps you plan and compare heating solutions for a neighbourhood, village, or district. It can:

- estimate the heat needed by buildings in a selected area;
- compare a shared district heating network with individual building solutions;
- test heat sources such as heat pumps, biomass, gas, biogas, electricity, and existing district heating;
- show the expected cost, carbon emissions, required equipment size, and heat production.

You do not need heating-planning or programming knowledge to use the pages in the tool.

## Before you start

The tool works best for locations in Germany and needs an internet connection to load maps, building information, and weather data.

For a complete beginner-friendly Windows setup—including Anaconda, Gurobi,
licence activation, downloading the code, creating the Conda environment, and
starting the application—see [INSTALLATION_GUIDE.md](INSTALLATION_GUIDE.md).

A technical administrator must install the required software once. Using Conda
with `environment.yml` is recommended because it installs the tested Python
version and all required Python packages together.

### Create the Conda environment

Open Anaconda Prompt or a terminal in the downloaded repository and run:

```text
conda env create --file environment.yml
conda activate oemof-system
python -m pip check
```

The default environment name is `oemof-system`. To use a different name:

```text
conda env create --name YOUR_ENVIRONMENT_NAME --file environment.yml
conda activate YOUR_ENVIRONMENT_NAME
```

To synchronize an existing environment after `environment.yml` changes:

```text
conda env update --name YOUR_ENVIRONMENT_NAME --file environment.yml --prune
conda activate YOUR_ENVIRONMENT_NAME
python -m pip check
```

As an alternative to Conda, install Python 3.10, create and activate a virtual
environment, and install the pinned packages with:

```text
python -m pip install -r requirements.txt
```

### Install Gurobi and start the tool

1. Install Gurobi and activate a valid Gurobi licence. Gurobi is not installed by `environment.yml` because its installation and licence are managed separately. The environment and installation folders may have any names. The application first uses the Python-native `gurobi_direct` interface when `gurobipy` is installed. Otherwise, it detects the external Gurobi launcher through `PATH`, `GUROBI_HOME`, standard Windows installation folders, and Conda roots. For an unusual installation, set `OEMOF_GUROBI_PATH` to the Gurobi `bin` directory or directly to its `gurobi`/`gurobi.bat` launcher.
2. Start the tool from the repository:

   ```text
   python oemof-hri.py
   ```

Your web browser should open the tool automatically. Keep the command window and browser page open while a calculation is running.

The environment may have any name. Activate it before starting the tool; the
application and its Streamlit subprocess both reuse that environment's Python:

```text
conda activate YOUR_ENVIRONMENT_NAME
python -c "import sys; print(sys.executable)"
python oemof-hri.py
```

The repository itself may also be stored under any folder name or location.
Application data, templates, and results are resolved relative to the downloaded
repository rather than the terminal's current working directory.

When using PyCharm, select that environment as the project interpreter and set
the run configuration to use the project interpreter. Do not enter an absolute
Python path in a shared run configuration. Files under `.idea/` are intentionally
not shared because interpreter registrations are specific to each computer.

## Plan and compare heating solutions

Use the **Workflow** menu on the left and follow these pages in order.

### 1. Project Setup

1. Select **Create a new project**.
2. Enter a clear project name, such as `Example Village 2035`.
3. Select the location by searching for a place or clicking on the map.
4. Check that the project name and location are correct.

You can also open, copy, or rename an existing project.

### 2. Building Heat Demand

Use this page when you do not already know the annual heat demand.

1. Draw the area to be studied on the map.
2. Check the buildings found by the tool.
3. Exclude garages, sheds, empty buildings, or other buildings that should not receive heat.
4. Correct building information if needed. Add a missing building manually when necessary.
5. Select **Estimate Space Heating Demand**.
6. Select **Estimate DHW Demand with OpenDHW** to estimate domestic hot water demand.
7. Check the annual demand totals.
8. Select **Save areas, buildings & results**.

If you already know the annual demand, you may skip this page and enter the values directly in each scenario.

### 3. Heat Supply Scenarios

A scenario is one possible heating solution. Create at least two scenarios to make a useful comparison. For example:

- `District heating with large heat pump`
- `Individual air-source heat pumps`
- `District heating with biomass`

For each scenario:

1. Select **Create New Scenario**.
2. Choose **District heating (centralized)** for one shared heat network, or **Decentralized solution** for separate systems at the buildings.
3. Give the scenario a clear name.
4. Complete the scenario sections:

   - **Demand:** Import the latest building-demand estimate, or enter a known annual demand. Use **HT** for normal high-temperature heating, **LT** for low-temperature heating, and **el** for electricity.
   - **Network Length:** For district heating, use the estimated route length or enter a known length.
   - **Sources:** Check the prices, carbon emissions, and availability of fuels or electricity. Replace default values with local values when available.
   - **Generation Techs:** Select the heating equipment connected to each source.
   - **Storage:** Add a heat storage tank if the scenario should use one. Storage is optional.

5. Select **Save** in every section that you change.

For a fair comparison, use the same heat demand in all scenarios unless demand reduction is part of the option you want to test.

### 4. Project Review & Simulation

1. Check the location, demand, and scenario summaries.
2. Confirm that every scenario contains at least one heat source and one matching heating technology.
3. Select **Submit this project**.
4. Wait for all scenarios to finish. A scenario may take from a few seconds to several minutes.

Do not close the tool while the simulation is running. If an error appears, check for missing demand, sources, technologies, or unrealistic input values, then submit again.

### 5. Simulation Results

Choose the project and compare the scenarios in the four result tabs:

- **Summary (CO₂ & Cost):** Compare total annual cost and carbon emissions.
- **Investment Capacity:** See the suggested equipment and storage sizes.
- **Generation Comparison:** See how much heat and electricity each technology produces.
- **Operational Control Overview:** See when the equipment operates during the year.

A useful scenario should have acceptable cost and emissions, use locally available energy sources, and have realistic equipment and network sizes. The option with the lowest cost is not always the best option for the area.

## Good practice

- Use clear project and scenario names.
- Check map data and demand estimates against local records when possible.
- Use current local energy prices and carbon values.
- Change one major choice at a time so the reason for a result is easy to understand.
- Copy a project before making large changes if you want to keep its old results.
- Re-run the project after changing demand or scenario inputs. The tool warns you when displayed results are out of date.

## Important note

The results are intended for early planning and comparison. They are estimates, not a final engineering design or investment decision. Before construction, ask qualified specialists to check building demand, network routes, equipment sizes, ground conditions, permits, energy prices, and available heat sources.

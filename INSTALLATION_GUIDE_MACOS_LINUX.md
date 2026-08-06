# Installation Guide for macOS and Linux

This guide supplements the detailed Windows guide. The application uses the
same Conda environment and Python launcher on all supported desktop operating
systems.

## Supported systems

The application supports:

- Windows 64-bit;
- macOS on Intel or Apple Silicon; and
- Linux on x86-64.

Windows remains the most tested platform. Complete the checks below before
starting a real project on macOS or Linux.

## 1 — Install Conda

Install the correct 64-bit Anaconda, Miniconda, or Miniforge build for the
computer:

- On an Apple Silicon Mac, select the **macOS arm64** installer.
- On an Intel Mac, select the **macOS x86-64** installer.
- On Linux, select the installer matching the machine architecture, normally
  **Linux x86-64**.

Close and reopen Terminal after installation, then check:

```bash
conda --version
```

If `conda` is installed but not available in a new shell, follow the installer
instructions for `conda init`, then restart Terminal.

## 2 — Obtain a Gurobi licence

Create or sign in to a Gurobi account and obtain the appropriate academic or
commercial licence. The software and licence are installed after the project
environment has been created in Section 4.

## 3 — Download and extract the application

Download and extract the repository ZIP from:

[github.com/IZZZ33/oemof_system](https://github.com/IZZZ33/oemof_system)

Alternatively, if Git is installed:

```bash
cd ~/Documents
git clone https://github.com/IZZZ33/oemof_system.git
cd oemof_system
```

For an extracted ZIP, use its actual folder name, for example:

```bash
cd ~/Downloads/oemof_system-main
```

Confirm that the correct folder is open:

```bash
ls
```

The output should include `environment.yml`, `oemof-hri.py`, and `start.sh`.

## 4 — Create the environment

From the repository folder, run:

```bash
conda env create --file environment.yml
conda activate oemof-system
python --version
python -m pip check
```

Python should report version 3.10.

While the environment is active, install the tested Gurobi package. It includes
`gurobipy`, `gurobi_cl`, and the licence tools:

```bash
conda install -c gurobi gurobi=12.0.1
```

Version 12.0.1 is tested with this project. Users of an organization-managed
Gurobi installation should follow their administrator's instructions instead.

For a named-user licence, run the private `grbgetkey` command supplied in the
Gurobi User Portal and accept the usual licence location:

```text
/Users/YOUR_NAME/gurobi.lic       # macOS
/home/YOUR_NAME/gurobi.lic        # Linux
```

Define the standard licence path in the Terminal that will start the program:

```bash
export GRB_LICENSE_FILE="$HOME/gurobi.lic"
```

Do not put `gurobi.lic` inside the Conda environment, and do not share the file
or private key. WLS and licence-server users should use the configuration
provided by their administrator instead.

Test the package and licence:

```bash
python -c "import pyomo, gurobipy as gp; print('Pyomo:', pyomo.__version__); print('Gurobi:', gp.gurobi.version())"
python -c "import gurobipy as gp; env=gp.Env(); print('Gurobi licence is working'); env.dispose()"
```

The tested combination reports Pyomo 6.8.2 and Gurobi 12.0.1. Check the
licence selected by Gurobi:

```bash
gurobi_cl --license
```

It should report an academic or commercial licence. `Restricted license` means
Gurobi is using its size-limited fallback rather than the full licence. If the
licence is intentionally stored outside the home directory, replace the path in
`GRB_LICENSE_FILE` with its actual location.

## 5 — Start the complete application

### Move to the repository before starting

The same Terminal commands work on macOS and Linux:

| Action | Command |
|---|---|
| Enter the repository | `cd ~/Documents/oemof_system` |
| Show the current location | `pwd` |
| List files and folders | `ls` |
| Display a text file | `cat README.md` |
| Move back one folder | `cd ..` |

For example:

```bash
cd ~/Documents/oemof_system
pwd
ls
cat README.md
```

For an extracted GitHub ZIP, use its actual folder name:

```bash
cd ~/Downloads/oemof_system-main
```

Useful path symbols:

- `~` means your home folder.
- `.` means the current folder.
- `..` means the parent folder.
- Paths containing spaces must be enclosed in quotation marks. Use `$HOME`
  instead of `~` inside quotation marks, for example:

  ```bash
  cd "$HOME/Documents/Heating Tool/oemof_system"
  ```

Pressing **Tab** while typing a folder name can complete it automatically.

Start the program with:

```bash
export GRB_LICENSE_FILE="$HOME/gurobi.lic"
python oemof-hri.py
```

This starts the interface and background optimization runner. Keep Terminal
open while using the application. Stop it with `Ctrl+C`.

## Starting the program on later days

The environment and Gurobi do not need to be installed again. Open Terminal and
run:

```bash
cd ~/path/to/oemof_system
conda activate oemof-system
export GRB_LICENSE_FILE="$HOME/gurobi.lic"
python oemof-hri.py
```

Replace the example repository path with its actual location.

## Apple Silicon notes

- Use ARM64 Conda and Gurobi packages consistently. Do not mix Intel/x86-64 and
  ARM64 packages in the same environment.
- The environment uses Python 3.10, which satisfies Gurobi's Python requirement
  for Apple Silicon.
- If a pinned dependency has no compatible ARM64 wheel, Conda or pip will stop
  during environment creation. Save the complete error output and ask the
  project administrator before changing a pinned version.

## Troubleshooting

### `conda: command not found`

Restart Terminal. If necessary, initialize the current shell using the command
recommended by the Conda installer and restart it again.

### `python: command not found`

Activate the environment:

```bash
conda activate oemof-system
which python
```

The path should point into an environment named `oemof-system`.

### Gurobi cannot find a licence

Check the licence using the Gurobi command supplied for your licence type. A
named-user licence is commonly stored at `~/gurobi.lic`. For another location,
follow Gurobi's instructions for the `GRB_LICENSE_FILE` environment variable.
The `export` command in Section 4 shows how to select the usual licence file.

### Gurobi cannot be found

Activate the environment and check both interfaces:

```bash
conda activate oemof-system
command -v gurobi_cl
python -c "import gurobipy; print(gurobipy.__file__)"
```

If neither command succeeds, reinstall Gurobi with the Conda command from
Section 4. If `gurobi_cl` works, restart the application from that same Terminal.

If Gurobi reports `Model too large for size-limited license`, it selected a
size-limited fallback instead of the intended licence. Run:

```bash
export GRB_LICENSE_FILE="$HOME/gurobi.lic"
gurobi_cl --license
```

The result must show the academic or commercial licence, not `Restricted
license`. Then restart the application from the same Terminal.

If a simulation reports `addConstr() got an unexpected keyword argument
'sense'`, Gurobi 12 is being used with an old Pyomo release. Update the active
environment and restart the application:

```bash
conda activate oemof-system
python -m pip install Pyomo==6.8.2
```

### The browser does not open

For a desktop installation, manually open the local URL printed in Terminal,
normally:

```text
http://localhost:8501
```

### Updating an existing environment

```bash
cd ~/path/to/oemof_system
conda activate oemof-system
conda env update --name oemof-system --file environment.yml --prune
python -m pip check
```

Then refresh Gurobi:

```bash
conda install -c gurobi gurobi=12.0.1
```

## Quick installation checklist

- [ ] A 64-bit Conda distribution matching the computer architecture is installed.
- [ ] The repository ZIP is extracted, or the repository is cloned with Git.
- [ ] Terminal is open in the folder containing `environment.yml`.
- [ ] `conda env create --file environment.yml` completed.
- [ ] `conda activate oemof-system` completed.
- [ ] Gurobi 12.0.1 was installed in the active environment.
- [ ] A suitable Gurobi licence is active and the Python licence test passed.
- [ ] The application was started with `python oemof-hri.py`.

## Official references

- [Installing Conda](https://docs.conda.io/projects/conda/en/stable/user-guide/install/)
- [Installing Conda on macOS](https://docs.conda.io/projects/conda/en/stable/user-guide/install/macos.html)
- [Installing Gurobi for Python](https://support.gurobi.com/hc/en-us/articles/360044290292-How-do-I-install-Gurobi-for-Python)
- [Gurobi on Apple Silicon](https://support.gurobi.com/hc/en-us/articles/4409801941521-How-do-I-use-gurobipy-on-Apple-Silicon-and-Monterey-macOS-12)

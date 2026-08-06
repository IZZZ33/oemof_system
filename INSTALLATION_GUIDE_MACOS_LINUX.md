# Installation Guide for macOS and Linux

This guide supplements the detailed Windows guide. The application uses the
same Conda environment and Python launcher on all supported desktop operating
systems.

## Current portability status

The code is designed for:

- Windows 64-bit;
- macOS on Intel or Apple Silicon; and
- Linux on x86-64, including headless systems.

The application uses platform-independent project paths, a non-interactive
Matplotlib backend, the current Conda environment's Python interpreter, and the
Python-native Gurobi interface when available. Windows remains the most tested
platform, so first installations on macOS and Linux should run the checks below
before starting a real project.

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

## 2 — Install and license Gurobi

Download Gurobi for the same operating system and processor architecture as the
Conda installation. Activate an academic or commercial licence according to
the instructions in your Gurobi account.

For a named-user licence, Gurobi normally supplies a private command similar to:

```bash
grbgetkey YOUR-LICENCE-KEY
```

Run the exact command supplied by Gurobi. Do not share the key or licence file.

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

### macOS and Linux folder and file commands

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

## 4 — Create the environment

From the repository folder, run:

```bash
conda env create --file environment.yml
conda activate oemof-system
python --version
python -m pip check
```

Python should report version 3.10.

Install the tested Gurobi Python package into this environment:

```bash
python -m pip install gurobipy==12.0.1
```

Test the package and licence:

```bash
python -c "import gurobipy as gp; env=gp.Env(); print('Gurobi licence is working'); env.dispose()"
```

## 5 — Start the complete application

The platform-independent command is:

```bash
python oemof-hri.py
```

The repository also includes a shell launcher:

```bash
bash start.sh
```

Both commands start the Streamlit interface and the background optimization
runner. Keep Terminal open while using the application. Stop it with `Ctrl+C`.

The existing `streamlit.sh` script is UI-only and does not start the background
optimization runner.

## Headless Linux servers

The calculation and file-generation code can run without a graphical desktop.
The Matplotlib backend defaults to `Agg`, so Tk is not required.

For remote browser access, server administrators must configure Streamlit's
listening address, firewall, authentication, and any reverse proxy according to
their security requirements. Do not expose an unprotected Streamlit service
directly to the public internet.

For a local SSH tunnel, an administrator can keep Streamlit bound to localhost
and forward its port securely. The exact command depends on the server and
organization.

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
python -m pip install gurobipy==12.0.1
python -m pip check
```

## Official references

- [Installing Conda](https://docs.conda.io/projects/conda/en/stable/user-guide/install/)
- [Installing Conda on macOS](https://docs.conda.io/projects/conda/en/stable/user-guide/install/macos.html)
- [Installing Gurobi for Python](https://support.gurobi.com/hc/en-us/articles/360044290292-How-do-I-install-Gurobi-for-Python)
- [Gurobi on Apple Silicon](https://support.gurobi.com/hc/en-us/articles/4409801941521-How-do-I-use-gurobipy-on-Apple-Silicon-and-Monterey-macOS-12)

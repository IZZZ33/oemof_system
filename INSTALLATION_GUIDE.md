# Installation Guide for Windows

This guide explains how to install and start the District Planning Tool on a
Windows computer. It is written for users without programming experience.

Users of other operating systems should follow
[INSTALLATION_GUIDE_MACOS_LINUX.md](INSTALLATION_GUIDE_MACOS_LINUX.md).

## What will be installed

You need four things:

1. **Anaconda**, which provides Python and the `conda` environment manager.
2. **Gurobi Optimizer** and a valid Gurobi licence.
3. **The District Planning Tool source code** from GitHub.
4. **The project environment**, created automatically from `environment.yml`.

> **You do not need to install Python separately when you install Anaconda.**
> Anaconda includes Python. The project's environment file will create a
> separate environment containing the required Python 3.10 version.

An internet connection is required during installation and for map, building,
and weather-data functions in the application.

## Part 1 — Install Anaconda

1. Open the official [Anaconda download page](https://www.anaconda.com/download).
2. Download **Anaconda Distribution for Windows (64-bit)**.
3. Double-click the downloaded installer.
4. Select **Just Me** unless your IT administrator instructs you otherwise.
5. Accept the default installation options and complete the installation.
6. Open the Windows **Start** menu.
7. Search for and open **Anaconda Prompt**.
8. Enter this command and press **Enter**:

   ```bat
   conda --version
   ```

If a version number appears, Anaconda is installed correctly. If Windows says
that `conda` is not recognized, make sure you opened **Anaconda Prompt**, not a
normal Command Prompt. Official Windows installation instructions are available
in the [Conda documentation](https://docs.conda.io/projects/conda/en/stable/user-guide/install/windows.html).

## Part 2 — Obtain a Gurobi licence

The optimization calculations require Gurobi. The small size-limited licence
included with some Gurobi installations is generally not sufficient for real
projects.

1. Create an account on the [Gurobi website](https://www.gurobi.com/).
2. Sign in to the Gurobi User Portal.
3. Choose the licence that applies to you:

   - Students, faculty, and staff at eligible institutions can request a free
     [academic licence](https://www.gurobi.com/academics).
   - Companies and other commercial users need an appropriate commercial,
     evaluation, or cloud licence from Gurobi.

Academic named-user licences are normally tied to one user and one computer.
Follow Gurobi's instructions and connect through your institution's network or
VPN when the licence request requires it.
The software and licence are installed after the project environment has been
created in Part 6.

## Part 3 — Download the program

### Recommended method: download a ZIP file

1. Open the project page:
   [github.com/IZZZ33/oemof_system](https://github.com/IZZZ33/oemof_system).
2. Select the green **Code** button.
3. Select **Download ZIP**.
4. When the download finishes, right-click the ZIP file and select
   **Extract All**.
5. Move the extracted folder to a permanent location where you have write
   permission, for example:

   ```text
   C:\Users\YOUR_NAME\Documents\oemof_system
   ```

Do not run the program from inside the ZIP file. The folder must be extracted
first. The project needs write access because it stores project inputs and
simulation results inside its folders.

### Alternative method: Git

Users who already have Git installed can instead enter:

```bat
cd /d "%USERPROFILE%\Documents"
git clone https://github.com/IZZZ33/oemof_system.git
cd oemof_system
```

Git is optional. Downloading and extracting the ZIP file is sufficient.

## Part 4 — Open the program folder in Anaconda Prompt

1. Open **Anaconda Prompt**.
2. Use `cd /d` followed by the full folder path. Put quotation marks around a
   path that contains spaces. For example:

   ```bat
   cd /d "C:\Users\YOUR_NAME\Documents\oemof_system"
   ```

   Another example for a folder on drive `D:` is:

   ```bat
   cd /d "D:\Programs\oemof_system"
   ```

3. Enter:

   ```bat
   dir
   ```

You should see at least these files:

```text
environment.yml
oemof-hri.py
requirements.txt
```

If they are not shown, you are in the wrong folder. Locate the extracted folder
in File Explorer, click its address bar, copy the full path, and use that path
with `cd /d`.

## Part 5 — Create the Conda environment

Make sure Anaconda Prompt is still in the folder containing `environment.yml`,
then enter:

```bat
conda env create --file environment.yml
```

Conda will download and install Python 3.10 and all packages listed in the file.
The environment is named `oemof-system`. Installation can take several minutes.
Wait until the command prompt appears again.

Activate the new environment:

```bat
conda activate oemof-system
```

The beginning of the prompt should now contain `(oemof-system)`.

Check the installation:

```bat
python --version
python -m pip check
```

The Python command should report Python 3.10. `pip check` should report that no
requirements are broken.

## Part 6 — Install and license Gurobi

Gurobi is deliberately not included in `environment.yml`, because its software
version and licence must be managed separately. Keep the `oemof-system`
environment activated and install the tested package. It includes the optimizer,
`gurobi_cl`, `gurobipy`, and the licence tools:

```bat
conda install -c gurobi gurobi=12.0.1
```

Version 12.0.1 is tested with this project. Users of an organization-managed
Gurobi installation should follow their administrator's instructions instead.

For a named-user licence, copy the private `grbgetkey` command from the Gurobi
User Portal and run it now. Accept the default licence location:

```text
C:\Users\YOUR_NAME\gurobi.lic
```

Then define that standard licence path in the current Anaconda Prompt:

```bat
set "GRB_LICENSE_FILE=%USERPROFILE%\gurobi.lic"
```

Do not put `gurobi.lic` inside the Conda environment, and do not share the file
or private key. WLS and licence-server users should follow the instructions
provided by their administrator instead.

Test that Python can find Gurobi and its licence:

```bat
python -c "import pyomo, gurobipy as gp; print('Pyomo:', pyomo.__version__); print('Gurobi:', gp.gurobi.version())"
python -c "import gurobipy as gp; env=gp.Env(); print('Gurobi licence is working'); env.dispose()"
gurobi_cl --license
```

You should see Pyomo 6.8.2, Gurobi 12.0.1, and an academic or commercial
licence message. If it says `Restricted license`, the full licence is not being
used and real projects may exceed its size limit.

Only if the licence file is deliberately stored somewhere else, replace the
example with its actual path:

```bat
set "GRB_LICENSE_FILE=C:\full\path\to\gurobi.lic"
```

If the test reports that the model is too large for the licence, a size-limited
licence is active instead of the required academic or commercial licence. If it
reports that no licence can be found, repeat the licence steps above or ask your licence
administrator for help.

## Part 7 — Start the complete program

### Move to the repository before starting

Anaconda Prompt uses the same basic commands as Windows Command Prompt:

| Action | Command |
|---|---|
| Enter the repository | `cd /d "C:\path\to\oemof_system"` |
| Show the current location | `cd` |
| List files and folders | `dir` |
| Display a text file | `type README.md` |
| Move back one folder | `cd ..` |

For example:

```bat
cd /d "C:\Users\YOUR_NAME\Documents\oemof_system"
dir
type README.md
```

If the repository was downloaded as a ZIP, its extracted folder may be named
`oemof_system-main`:

```bat
cd /d "C:\Users\YOUR_NAME\Downloads\oemof_system-main"
```

The `/d` option allows the command to change both the folder and the drive, for
example from drive `C:` to drive `D:`. Paths containing spaces must be enclosed
in quotation marks.

In Windows PowerShell, the corresponding commands are:

```powershell
cd "C:\Users\YOUR_NAME\Documents\oemof_system"
pwd
ls
cat README.md
cd ..
```

PowerShell accepts `cd`, `ls`, and `cat` as aliases. Pressing **Tab** while
typing a folder name can complete it automatically.

Each time you start the application:

1. Open **Anaconda Prompt**.
2. Change to the program folder:

   ```bat
   cd /d "C:\Users\YOUR_NAME\Documents\oemof_system"
   ```

3. Activate the environment:

   ```bat
   conda activate oemof-system
   ```

4. Select the usual Gurobi licence file:

   ```bat
   set "GRB_LICENSE_FILE=%USERPROFILE%\gurobi.lic"
   ```

5. Start the application:

   ```bat
   python oemof-hri.py
   ```

This is the recommended command. It starts both:

- the Streamlit web interface; and
- the background optimization runner that processes submitted projects.

Your default web browser should open automatically. If it does not, look in the
Anaconda Prompt for an address such as `http://localhost:8501` and open that
address manually.

Keep the Anaconda Prompt window open while using the program. Closing it stops
the application and can interrupt a running simulation.

To stop the program safely, click the Anaconda Prompt window and press:

```text
Ctrl+C
```

If Windows asks whether to terminate a batch job, enter `Y` and press **Enter**.

## Starting the program on later days

You do not need to recreate the environment. Run:

```bat
cd /d "C:\Users\YOUR_NAME\Documents\oemof_system"
conda activate oemof-system
set "GRB_LICENSE_FILE=%USERPROFILE%\gurobi.lic"
python oemof-hri.py
```

You do not need to recreate the environment, reinstall Gurobi, or retrieve the
licence again unless the installation or licence has changed.

## Updating the program

If you downloaded a new ZIP version, keep a backup of important project and
result folders before replacing the program directory.

After receiving updated code, synchronize the existing environment from the
program folder:

```bat
conda activate oemof-system
conda env update --name oemof-system --file environment.yml --prune
python -m pip check
```

Because `environment.yml` does not manage Gurobi, refresh it afterward:

```bat
conda install -c gurobi gurobi=12.0.1
```

Do not run `conda env create` again when the environment already exists. Use
`conda env update` instead.

## Common problems

### `conda` is not recognized

- Use **Anaconda Prompt** from the Windows Start menu.
- Close and reopen the prompt after installing Anaconda.
- Confirm that `conda --version` works before continuing.

### The system cannot find the specified path

- Check the folder in File Explorer.
- Copy its full path from the File Explorer address bar.
- Use quotation marks and `cd /d`, for example:

  ```bat
  cd /d "C:\Users\Jane Doe\Documents\oemof_system"
  ```

### The Conda environment already exists

Activate and update it:

```bat
conda activate oemof-system
conda env update --name oemof-system --file environment.yml --prune
```

### `ModuleNotFoundError` appears

The environment is probably not active, or the command is using another Python
installation. Enter:

```bat
conda activate oemof-system
where python
python -m pip check
```

The first path printed by `where python` should contain
`envs\oemof-system`.

### Gurobi cannot be found

First test:

```bat
python -c "import gurobipy; print(gurobipy.__file__)"
```

If this fails, reactivate the environment and reinstall Gurobi:

```bat
conda activate oemof-system
conda install -c gurobi gurobi=12.0.1
```

If importing works but the licence test fails, reactivate the licence with the
private `grbgetkey` command from your Gurobi account or contact your licence
administrator.

If Gurobi reports `Model too large for size-limited license`, it selected a
size-limited fallback instead of the intended licence. Run:

```bat
set "GRB_LICENSE_FILE=%USERPROFILE%\gurobi.lic"
gurobi_cl --license
```

The result must show the academic or commercial licence, not `Restricted
license`. Then restart the application from the same Anaconda Prompt.

If a simulation reports `addConstr() got an unexpected keyword argument
'sense'`, Gurobi 12 is being used with an old Pyomo release. Update the active
environment from the repository:

```bat
conda activate oemof-system
python -m pip install Pyomo==6.8.2
```

Then stop and restart the application.

If the licence is stored in a nonstandard location, set `GRB_LICENSE_FILE` as
shown in Part 6. Start `python oemof-hri.py` in the same Anaconda Prompt and
restart an application that was already running.

### The browser does not open

Keep the Anaconda Prompt open and manually visit:

```text
http://localhost:8501
```

### Port 8501 is already in use

Another copy may already be running. Find its Anaconda Prompt and stop it with
`Ctrl+C`, then start the application again.

### A simulation is slow or runs out of memory

- Close other memory-intensive programs.
- Test fewer scenarios at one time.
- Reduce unnecessary components or the modeled area.
- Keep the computer connected to power and prevent it from sleeping during a
  long calculation.

## Quick installation checklist

- [ ] Anaconda installed and `conda --version` works.
- [ ] Gurobi 12.0.1 installed in the active environment.
- [ ] A valid academic or commercial Gurobi licence activated.
- [ ] Project ZIP downloaded and extracted.
- [ ] Anaconda Prompt opened in the project folder.
- [ ] `conda env create --file environment.yml` completed.
- [ ] `conda activate oemof-system` completed.
- [ ] `gurobi_cl --license` reports the intended full licence.
- [ ] Application started with `python oemof-hri.py`.

## Official reference links

- [Conda installation on Windows](https://docs.conda.io/projects/conda/en/stable/user-guide/install/windows.html)
- [Creating an environment from an environment file](https://docs.conda.io/projects/conda/en/stable/commands/env/create.html)
- [Gurobi academic licences](https://www.gurobi.com/academics)
- [Installing Gurobi for Python](https://support.gurobi.com/hc/en-us/articles/360044290292-How-do-I-install-Gurobi-for-Python)
- [Streamlit run command](https://docs.streamlit.io/develop/api-reference/cli/run)

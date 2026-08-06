# Installation Guide for Windows

This guide explains how to install and start the District Planning Tool on a
Windows computer. It is written for users without programming experience.

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

## Part 2 — Install Gurobi and activate a licence

The optimization calculations require Gurobi. The small size-limited licence
included with some Gurobi installations is generally not sufficient for real
projects.

### 2.1 Create a Gurobi account and choose a licence

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

### 2.2 Install Gurobi Optimizer

1. Download the 64-bit Windows installer from the Gurobi download page available
   through your account.
2. Run the installer and accept the normal Windows installation options.
3. Restart **Anaconda Prompt** after installation so it can see the newly
   installed Gurobi commands.

### 2.3 Activate the licence

The Gurobi licence page provides a private command similar to this:

```bat
grbgetkey YOUR-LICENCE-KEY
```

1. Copy the exact command from your Gurobi licence page.
2. Paste it into **Anaconda Prompt** and press **Enter**.
3. When asked where to save the licence, accept the suggested default location.
4. Do not share your licence key or `gurobi.lic` file with other people.

If `grbgetkey` is not recognized, use the Gurobi Command Prompt installed with
Gurobi, or follow Gurobi's licence-tool instructions. Gurobi's official academic
page contains the current named-user activation procedure.

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

## Part 6 — Install Gurobi in the project environment

Gurobi is deliberately not included in `environment.yml`, because its software
version and licence must be managed separately. Even if the full Gurobi program
is installed on Windows, install its Python package into the activated project
environment:

```bat
python -m pip install gurobipy==12.0.1
```

Version 12.0.1 is the version tested with this project. If your organization
requires another Gurobi version, ask the project administrator before changing
it.

Test that Python can find Gurobi and its licence:

```bat
python -c "import gurobipy as gp; env=gp.Env(); print('Gurobi licence is working'); env.dispose()"
```

You should see `Gurobi licence is working`. Informational Gurobi text may appear
before it; that is normal.

If the test reports that the model is too large for the licence, a size-limited
licence is active instead of the required academic or commercial licence. If it
reports that no licence can be found, repeat Part 2.3 or ask your licence
administrator for help.

## Part 7 — Start the complete program

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

4. Start the application:

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

## UI-only Streamlit command

For interface testing, the Streamlit page can be started directly:

```bat
python -m streamlit run logic\data_input.py
```

On macOS or Linux, `bash streamlit.sh` starts the same interface.

> **Important:** these direct Streamlit commands start only the web interface.
> They do not start the optimization runner that watches for submitted projects.
> Normal users who need simulations and results should use
> `python oemof-hri.py`.

## Starting the program on later days

You do not need to recreate the environment. Use only these three commands:

```bat
cd /d "C:\Users\YOUR_NAME\Documents\oemof_system"
conda activate oemof-system
python oemof-hri.py
```

## Updating the program

If you downloaded a new ZIP version, keep a backup of important project and
result folders before replacing the program directory.

After receiving updated code, synchronize the existing environment from the
program folder:

```bat
conda activate oemof-system
conda env update --name oemof-system --file environment.yml --prune
python -m pip install gurobipy==12.0.1
python -m pip check
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

If this fails, reactivate the environment and reinstall the tested package:

```bat
conda activate oemof-system
python -m pip install gurobipy==12.0.1
```

If importing works but the licence test fails, reactivate the licence with the
private `grbgetkey` command from your Gurobi account or contact your licence
administrator.

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
- [ ] Gurobi Optimizer installed.
- [ ] A valid academic or commercial Gurobi licence activated.
- [ ] Project ZIP downloaded and extracted.
- [ ] Anaconda Prompt opened in the project folder.
- [ ] `conda env create --file environment.yml` completed.
- [ ] `conda activate oemof-system` completed.
- [ ] `gurobipy==12.0.1` installed and licence test passed.
- [ ] Application started with `python oemof-hri.py`.

## Official reference links

- [Conda installation on Windows](https://docs.conda.io/projects/conda/en/stable/user-guide/install/windows.html)
- [Creating an environment from an environment file](https://docs.conda.io/projects/conda/en/stable/commands/env/create.html)
- [Gurobi academic licences](https://www.gurobi.com/academics)
- [Installing Gurobi for Python](https://support.gurobi.com/hc/en-us/articles/360044290292-How-do-I-install-Gurobi-for-Python)
- [Streamlit run command](https://docs.streamlit.io/develop/api-reference/cli/run)


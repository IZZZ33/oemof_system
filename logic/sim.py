import os
os.environ.setdefault("MPLBACKEND", "Agg")
import oemof.solph as solph
import oemof.solph.constraints as constraints
import networkx as nx
import matplotlib.pyplot as plt
import logging
import pickle
import re
from pathlib import Path
import shutil
import sys
import logic.custom_constraints as custom_constraints
from oemof.network.graph import create_nx_graph


def _gurobi_environment_roots() -> list[Path]:
    """Return active Conda/virtual-environment roots without duplicates."""
    roots = []
    for value in (
        os.environ.get("CONDA_PREFIX"),
        os.environ.get("VIRTUAL_ENV"),
        sys.prefix,
    ):
        if not value:
            continue
        root = Path(value).expanduser()
        if root not in roots:
            roots.append(root)

    # Also inspect Conda base. Some installations keep Gurobi there while the
    # application itself runs from a named environment.
    for root in tuple(roots):
        if root.parent.name.lower() == "envs" and root.parent.parent not in roots:
            roots.append(root.parent.parent)
    return roots


def _configure_gurobi_license(executable: str | Path | None = None) -> str | None:
    """Use an explicit licence, or discover a licence near Python/Gurobi."""
    configured = os.environ.get("GRB_LICENSE_FILE")
    if configured:
        # Never override licence-server, WLS, or explicitly selected settings.
        return configured

    candidates = []
    for root in _gurobi_environment_roots():
        candidates.extend(
            [
                root / "gurobi.lic",
                root / "lib" / "gurobi.lic",
                root / "Library" / "gurobi.lic",
                root / "Library" / "bin" / "gurobi.lic",
                root / "share" / "gurobi" / "gurobi.lic",
            ]
        )

    gurobi_home = os.environ.get("GUROBI_HOME")
    if gurobi_home:
        home = Path(os.path.expandvars(gurobi_home)).expanduser()
        candidates.extend([home / "gurobi.lic", home.parent / "gurobi.lic"])

    if executable:
        executable_path = Path(executable).expanduser()
        candidates.extend(
            parent / "gurobi.lic"
            for parent in list(executable_path.parents)[:4]
        )

    candidates.append(Path.home() / "gurobi.lic")
    if os.name == "nt":
        candidates.append(Path("C:/gurobi/gurobi.lic"))
    elif sys.platform == "darwin":
        candidates.append(Path("/Library/gurobi/gurobi.lic"))
    else:
        candidates.append(Path("/opt/gurobi/gurobi.lic"))

    for candidate in candidates:
        if candidate.is_file():
            os.environ["GRB_LICENSE_FILE"] = str(candidate)
            return str(candidate)
    return None


def _ensure_gurobi_available() -> str:
    """Locate gurobi_cl for Pyomo's command-line solver interface."""
    launcher_names = (
        ("gurobi_cl.exe", "gurobi_cl")
        if os.name == "nt"
        else ("gurobi_cl",)
    )
    for launcher_name in launcher_names:
        configured = shutil.which(launcher_name)
        if configured:
            _configure_gurobi_license(configured)
            return configured

    candidates: list[Path] = []
    explicit_path = os.environ.get("OEMOF_GUROBI_PATH")
    if explicit_path:
        explicit = Path(os.path.expandvars(explicit_path)).expanduser()
        candidates.extend(
            [explicit / name for name in launcher_names]
            if explicit.is_dir() else [explicit]
        )

    gurobi_home = os.environ.get("GUROBI_HOME")
    if gurobi_home:
        home = Path(os.path.expandvars(gurobi_home)).expanduser()
        for launcher_name in launcher_names:
            candidates.extend(
                [
                    home / "bin" / launcher_name,
                    home / "win64" / "bin" / launcher_name,
                    home / "macos_universal2" / "bin" / launcher_name,
                    home / "linux64" / "bin" / launcher_name,
                ]
            )

    # Conda packages place gurobi_cl inside the active environment. Usually its
    # bin directory is already on PATH, but checking it explicitly also covers
    # applications started from Finder, a desktop shortcut, or an IDE.
    environment_roots = _gurobi_environment_roots()
    for environment_root in environment_roots:
        for launcher_name in launcher_names:
            candidates.extend(
                [
                    environment_root / "bin" / launcher_name,
                    environment_root / "Library" / "bin" / launcher_name,
                    environment_root / launcher_name,
                ]
            )

    if os.name == "nt":
        for launcher_name in launcher_names:
            candidates.append(Path("C:/gurobi/win64/bin") / launcher_name)
            for root in (
                Path("C:/"),
                Path(os.environ.get("ProgramFiles", "C:/Program Files")),
            ):
                try:
                    candidates.extend(
                        sorted(
                            root.glob(
                                f"gurobi*/win64/bin/{launcher_name}"
                            ),
                            reverse=True,
                        )
                    )
                except OSError:
                    pass

    elif sys.platform == "darwin":
        for launcher_name in launcher_names:
            for root in (Path("/Library"), Path("/opt"), Path.home()):
                try:
                    candidates.extend(
                        sorted(
                            root.glob(
                                f"gurobi*/macos_universal2/bin/{launcher_name}"
                            ),
                            reverse=True,
                        )
                    )
                except OSError:
                    pass
    else:
        for launcher_name in launcher_names:
            for root in (Path("/opt"), Path.home()):
                try:
                    candidates.extend(
                        sorted(
                            root.glob(f"gurobi*/linux64/bin/{launcher_name}"),
                            reverse=True,
                        )
                    )
                except OSError:
                    pass

    seen = set()
    for candidate in candidates:
        candidate = candidate.expanduser()
        candidate_key = os.path.normcase(str(candidate))
        if candidate_key in seen or not candidate.is_file():
            continue
        if os.name != "nt" and not os.access(candidate, os.X_OK):
            continue
        seen.add(candidate_key)
        os.environ["PATH"] = str(candidate.parent) + os.pathsep + os.environ.get(
            "PATH", ""
        )
        _configure_gurobi_license(candidate)
        return str(candidate)

    raise RuntimeError(
        "Gurobi could not be found. Install Gurobi, activate a valid licence, "
        "and add the Gurobi bin directory to PATH, set GUROBI_HOME, set "
        "OEMOF_GUROBI_PATH to gurobi_cl or its bin directory, or install "
        "gurobipy in the active Python environment. "
        f"The application is running with {sys.executable}."
    )


def _gurobi_solver_configuration() -> tuple[str, str | None, str]:
    """Choose a Python-native solver first, then the external launcher."""
    _configure_gurobi_license()
    incompatible_direct_reason = None
    try:
        from pyomo.environ import SolverFactory
        import pyomo
        import gurobipy

        pyomo_numbers = tuple(
            int(part) for part in re.findall(r"\d+", pyomo.__version__)[:3]
        )
        pyomo_version = pyomo_numbers + (0,) * (3 - len(pyomo_numbers))
        gurobi_version = tuple(gurobipy.gurobi.version())

        # Gurobi 12 removed the keyword-based addConstr signature used by old
        # Pyomo releases. Pyomo 6.8.1 replaced that deprecated call. Existing
        # environments with Pyomo 6.5 can still solve safely through gurobi_cl.
        if gurobi_version >= (12, 0, 0) and pyomo_version < (6, 8, 1):
            incompatible_direct_reason = (
                f"Pyomo {pyomo.__version__} is incompatible with the "
                f"gurobi_direct interface of Gurobi {'.'.join(map(str, gurobi_version))}. "
                "Using the gurobi_cl file interface instead. Upgrade to "
                "Pyomo 6.8.2 to restore the direct interface."
            )
            logging.warning(incompatible_direct_reason)
        else:
            direct_solver = SolverFactory("gurobi_direct")
            if direct_solver.available(exception_flag=False):
                return "gurobi_direct", None, str(
                    Path(gurobipy.__file__).resolve()
                )

    except Exception:
        pass

    try:
        launcher = _ensure_gurobi_available()
    except RuntimeError as exc:
        if incompatible_direct_reason:
            raise RuntimeError(
                f"{incompatible_direct_reason} However, gurobi_cl could not be "
                "found. Upgrade the active environment with `python -m pip "
                "install Pyomo==6.8.2`, or install the full Gurobi Conda "
                "package so gurobi_cl is available."
            ) from exc
        raise
    return "gurobi", "mps", launcher


class Sim(object):
    def __init__(self, oemof_pre, emission_limit=None, roof_area_limit=None, chp_funding_limit=None,
                 WNS40_funding_limit=None, OnOff_limit=None, vbH_min_limit=None,
                 # invest_min_limit=None, chp_feed_in_limit=None,
                 save_sim=False, skip_sim=False,
                 exclude_draw_nodes=None):
        """
        :param oemof_pre: obj oemof preprocessing object
        :param int emission_limit: Set CO2 emission limit for oemof solph
        :param dict roof_area_limit: Set roof area limit for oemof solph with needed kwargs from dict
        :param save_sim: bool if true save sim results with pickle.dump to disk
        :param skip_sim: bool if true skip sim and load previous results with pickle.load fromdisk
        :param list exclude_draw_nodes: list of substrings of nodes which are not drawn in the energy system graph
        """

        self.oemof_pre = oemof_pre
        self.save_sim = save_sim
        self.skip_sim = skip_sim
        # path where existing result should be saved to or loaded from
        self.p_result_sim = os.path.join(self.oemof_pre.p_results, self.oemof_pre.project_name + '.dat')

        # constraints or limits if they are given
        self.emission_limit = emission_limit
        self.roof_area_limit = roof_area_limit
        # self.invest_min_limit = invest_min_limit
        self.chp_funding_limit = chp_funding_limit
        # self.chp_feed_in_limit = chp_feed_in_limit
        self.WNS40_funding_limit = WNS40_funding_limit
        self.OnOff_limit = OnOff_limit
        self.vbH_min_limit = vbH_min_limit

        # define globals:
        self.oemof_model = None  # Result of oemof simulation
        self.oemof_solph_results = None  # Results file of oemof solph
        self.oemof_solph_meta_results = None  # Meta Results Data of oemof solph

        self.exclude_draw_nodes = exclude_draw_nodes

        # function calls:
        # if not skip_sim:
        #     self._draw_nodes_graph()
        self._oemof_solve()

    def _draw_nodes_graph(self):
        """Function creates an oemof nodes plot of energy system"""

        # Creating oemof graph from energy system:
        self.graph = create_nx_graph(energy_system=self.oemof_pre.esys,
                                     remove_nodes_with_substrings=self.exclude_draw_nodes)

        # Plotting graph:
        logging.info('Creating nodes plot started for {}.'.format(self.oemof_pre.project_name))
        edge_labels = True
        node_color = '#AFAFAF'
        edge_color = '#CFCFCF'
        node_size = min(2000 / (len(self.graph.nodes) / 2), 500)
        # font_size = int(12 / (len(self.graph.nodes) / 20))
        font_size = min(12, int(12 / (len(self.graph.nodes) / 20)))
        # weight = min(30, int(30 / len(self.graph.nodes)))
        with_labels = True
        arrows = True
        layout = 'neato'

        if type(node_color) is dict:
            node_color = [node_color.get(g, '#AFAFAF') for g in self.graph.nodes()]

        # set drawing options
        options = {
            'with_labels': with_labels,
            'node_color': node_color,
            'edge_color': edge_color,
            'node_size': node_size,
            'font_size': font_size,
            'arrows': arrows
        }

        # use pygraphviz for graph layout
        pos = nx.drawing.nx_agraph.graphviz_layout(G=self.graph, prog=layout)

        # draw graph
        fig_size = (14, 8)
        plt.figure(figsize=fig_size)
        title = 'oemof energy system chart - {}'.format(self.oemof_pre.project_name)
        plt.title(title)
        nx.draw(self.graph, pos=pos, **options)

        # add edge labels for all edges
        if edge_labels is True and plt:
            labels = nx.get_edge_attributes(self.graph, 'weight')
            nx.draw_networkx_edge_labels(self.graph, font_size=font_size, pos=pos, edge_labels=labels)

        path = os.path.join(self.oemof_pre.p_results, 'EnergySystem.pdf')
        plt.savefig(path)
        plt.close()
        logging.info('Nodes_plot at {} finished. \n'.format(path))

    def _oemof_solve(self):
        """Method solves oemof model"""

        # run simulation normally or skip if skip_sim is True and result file exists
        if not self.skip_sim:
            # --- Create oemof model --- #
            self.oemof_model = solph.Model(self.oemof_pre.esys)

            # --- Apply constraints --- #
            if self.emission_limit is not None:
                logging.info("Using constraint: emission limit = "+str(self.emission_limit))
                constraints.emission_limit(om=self.oemof_model, limit=self.emission_limit)
            if self.roof_area_limit is not None:
                logging.info("Using constraint: roof area limit = "+str(self.roof_area_limit['max_roof_area']))
                custom_constraints.roof_area_limit(om=self.oemof_model, esys=self.oemof_pre.esys,
                                                   xls_data=self.oemof_pre.xls_oemof_input_file, **self.roof_area_limit)
            # if self.invest_min_limit is not None:
            #     logging.info("Using constraint: investment limit")
            #     custom_constraints.invest_min_limit(pre=self.oemof_pre,
            #                                        om=self.oemof_model, esys=self.oemof_pre.esys,
            #                                        xls_data=self.oemof_pre.xls_oemof_input_file, **self.invest_min_limit)
            if self.chp_funding_limit is not None:
                logging.info("Using constraint: chp funding limit = "+str(self.chp_funding_limit['chp_funding_flh']))
                custom_constraints.chp_funding_limit(pre=self.oemof_pre,
                                                     om=self.oemof_model, esys=self.oemof_pre.esys,
                                                     xls_data=self.oemof_pre.xls_oemof_input_file,
                                                     **self.chp_funding_limit)
            # if self.chp_feed_in_limit is not None:
            #     logging.info("Using constraint: chp feed in limit for: "+str(self.chp_feed_in_limit['chp_label']))
            #     custom_constraints.chp_feed_in_limit(pre=self.oemof_pre,
            #                                          om=self.oemof_model, esys=self.oemof_pre.esys,
            #                                          xls_data=self.oemof_pre.xls_oemof_input_file,
            #                                          **self.chp_feed_in_limit)
            if self.WNS40_funding_limit is not None:
                logging.info("Using constraint: WNS40 funding limit = " + str(self.WNS40_funding_limit['WNS40_renewable_share_limit']))
                custom_constraints.WNS40_funding_limit(om=self.oemof_model, esys=self.oemof_pre.esys,
                                                     xls_data=self.oemof_pre.xls_oemof_input_file,
                                                     **self.WNS40_funding_limit)
            if self.OnOff_limit is not None:
                keyword = 'keyword'
                for key in self.OnOff_limit:
                    if keyword in key:
                        logging.info(f"Using constraint: OnOff limit for {self.OnOff_limit[key]}")
                        constraints.limit_active_flow_count_by_keyword(
                        model=self.oemof_model, lower_limit=0, upper_limit=1, keyword=self.OnOff_limit[key]
                        )

            if self.vbH_min_limit is not None:
                logging.info("Using constraint: vbH min limit = " + str(self.vbH_min_limit['vbH_min']))
                custom_constraints.vbH_min_limit(om=self.oemof_model, esys=self.oemof_pre.esys,
                                                     xls_data=self.oemof_pre.xls_oemof_input_file,
                                                     **self.vbH_min_limit)

            # --- Start solving model --- #
            logging.info('SupplyOptimizationSolve for Case: {} started.'.format(self.oemof_pre.project_name))
            print("*********************************************************")
            print('Start solving Case: {}'.format(self.oemof_pre.project_name))
            print('The magic of numerical optimization started!')
            print('Grab a coffee, an apple or just relax! Might take a while.')
            print('\t...')

            solver_name, solver_io, solver_location = _gurobi_solver_configuration()
            logging.info(
                "Using Gurobi interface %s from: %s",
                solver_name,
                solver_location,
            )
            solver_log_path = Path(self.oemof_pre.p_results) / "gurobi.log"
            solver_arguments = {
                "solver": solver_name,
                "solve_kwargs": {"tee": False},
                # Gurobi's default concurrent root-LP method keeps several
                # algorithm copies in memory. Large annual models can exhaust
                # RAM before the first branch-and-bound node. Dual simplex uses
                # one algorithm and is substantially less memory intensive.
                "cmdline_options": {
                    "Method": 1,
                    "NumericFocus": 1,
                    "NodefileStart": 0.5,
                    "LogFile": str(solver_log_path),
                },
            }
            if solver_io is not None:
                solver_arguments["solver_io"] = solver_io
            try:
                self.oemof_model.solve(**solver_arguments)
            except Exception as exc:
                try:
                    solver_log = solver_log_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    solver_log = ""
                if "out of memory" in solver_log.lower():
                    raise RuntimeError(
                        "Gurobi ran out of memory while solving this scenario. "
                        "The model is too large for the currently available RAM."
                    ) from exc
                raise
            # self.oemof_model.solve(solver='gurobi', threads=8, solve_kwargs={'tee': False})
            self.oemof_solph_results = solph.processing.results(self.oemof_model)
            self.oemof_solph_meta_results = solph.processing.meta_results(self.oemof_model)
            print("*********************************************************")
            logging.info('Finished solving case : {}\n'.format(self.oemof_pre.project_name))
            logging.info('Total simulation time : {} s\n'.format(self.oemof_solph_meta_results['solver']['Time']))
        else:
            # skipping simulation and use existing result file
            if not os.path.isfile(self.p_result_sim):
                raise FileNotFoundError(
                    "Saved Simulation result file {} not found.\nPlease ensure to use save_sim=True before using "
                    "skip_sim=True".format(
                        self.p_result_sim))
            else:
                with open(self.p_result_sim, "rb") as f:  # "rb" because we want to read in binary mode
                    self.oemof_solph_results = pickle.load(f)
                    logging.info("Simulation results loaded from {}".format(self.p_result_sim))

        # save sim results to file after simulation if save_sim is True and simulation is not skipped
        if self.save_sim and self.skip_sim is False:
            with open(self.p_result_sim, "wb") as f:  # "wb" because we want to write in binary mode
                pickle.dump(self.oemof_solph_results, f)
                logging.info("Simulation results saved to {}".format(self.p_result_sim))

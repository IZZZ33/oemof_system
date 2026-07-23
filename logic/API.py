import os
from logic.utilities import create_folder
from logic import pre
from logic import sim
from logic import post


class OEMOFSim(object):
    def __init__(self, oemof_input_file=None, project_name=None, start_date=None, time_steps=None, time_step_freq=None,
                 emission_limit=None, roof_area_limit=None, chp_funding_limit=None,
                 # chp_feed_in_limit=None, invest_min_limit=None,
                 WNS40_funding_limit=None, OnOff_limit=None, vbH_min_limit=None,
                 p_results=None, save_sim=False, skip_sim=False, exclude_draw_nodes=None):
        """
        :param str project_name: project name
        :param str oemof_input_file: path to oemof .xlsx input file
        :param str start_date: Start date of optimization [year-month-day hours:minutes:seconds]
        :param int time_steps: Number of time steps in time series
        :param str time_step_freq: Time delta for optimization ex.: "60min", "3600s", etc.
        :param int emission_limit: Set CO2 emission limit for oemof solph
        :param dict roof_area_limit: Set roof area limit for oemof solph with needed kwargs from dict
        :param str p_results: path where results should be stored
        :param bool save_sim: save self.oemof_sim object to file (to skip simulation later when working on post)
        :param bool skip_sim: skip simulation if True and previous result file already exists in p_results
        :param list exclude_draw_nodes: list of substrings of nodes which are not drawn in the energy system graph
        """
        # assign parameters to object variables

        if not project_name:
            raise NameError("No project name specified")
        else:
            self.project_name = project_name

        if not oemof_input_file or not os.path.isfile(oemof_input_file):
            raise FileNotFoundError("Excel data file {} not found.".format(oemof_input_file))
        else:
            self.oemof_input_file = oemof_input_file

        if p_results is not None:
            self.p_results = os.path.abspath(p_results)
        else:
            self.p_results = os.path.abspath(os.path.join('results', self.project_name))

        if not start_date:
            raise NameError("No start date for optimization specified")
        else:
            self.start_date = start_date

        # constraints or limits if they are given
        self.emission_limit = emission_limit
        self.roof_area_limit = roof_area_limit
        # self.invest_min_limit = invest_min_limit
        self.chp_funding_limit = chp_funding_limit
        # self.chp_feed_in_limit = chp_feed_in_limit
        self.WNS40_funding_limit = WNS40_funding_limit
        self.OnOff_limit = OnOff_limit
        self.vbH_min_limit = vbH_min_limit

        self.save_sim = save_sim
        self.skip_sim = skip_sim

        if not time_steps:
            raise NameError("No number of time steps in time series specified")
        else:
            self.time_steps = time_steps

        if not time_step_freq:
            raise NameError("No time step frequency for optimization specified")
        else:
            self.time_step_freq = time_step_freq

        if exclude_draw_nodes is None:
            self.exclude_draw_nodes = []
        else:
            self.exclude_draw_nodes = exclude_draw_nodes

        # create new object variables:
        self.oemof_pre = None  # oemof preprocessing object
        self.oemof_sim = None  # oemof simulation object
        self.oemof_post = None  # oemof postprocessing object

        # create result folder for project
        create_folder(self.p_results)

        self._pre_processing()
        self._simulate_model()
        self._post_processing()

    def _pre_processing(self):
        """oemof preprocessing, reads Excel file and creates oemof energy system"""
        self.oemof_pre = pre.Pre(project_name=self.project_name, oemof_input_file=self.oemof_input_file,
                                 start_date=self.start_date, time_steps=self.time_steps,
                                 time_step_freq=self.time_step_freq, p_results=self.p_results)

    def _simulate_model(self):
        """simulate and optimize oemof model"""
        self.oemof_sim = sim.Sim(oemof_pre=self.oemof_pre, emission_limit=self.emission_limit,
                                 roof_area_limit=self.roof_area_limit,
                                 # invest_min_limit=self.invest_min_limit,
                                 chp_funding_limit=self.chp_funding_limit,
                                 # chp_feed_in_limit=self.chp_feed_in_limit,
                                 WNS40_funding_limit=self.WNS40_funding_limit,
                                 OnOff_limit=self.OnOff_limit,
                                 vbH_min_limit=self.vbH_min_limit,
                                 save_sim=self.save_sim, skip_sim=self.skip_sim,
                                 exclude_draw_nodes=self.exclude_draw_nodes)

    def _post_processing(self):
        """postprocessing on simulation results, create result diagrams """
        self.oemof_post = post.Post(oemof_sim=self.oemof_sim, oemof_pre=self.oemof_pre, p_results=self.p_results)

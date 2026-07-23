import os
import oemof.solph as solph
import oemof.solph.constraints as constraints
import networkx as nx
import matplotlib.pyplot as plt
import logging
import pickle
import logic.custom_constraints as custom_constraints
from oemof.network.graph import create_nx_graph


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

            self.oemof_model.solve(solver='gurobi', solver_io="mps",
                                   # threads=8,
                                   solve_kwargs={'tee': False})
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

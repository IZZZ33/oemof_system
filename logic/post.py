import os
import logging

import oemof.solph as solph
import matplotlib as mpl
import matplotlib.pyplot as plt
from logic.utilities import create_folder
import pandas as pd

mpl.use('TkAgg') # workaround for backend error when plotting (maybe Windows-specific (?))


class Post(object):
    def __init__(self, oemof_sim, oemof_pre, p_results, fig_size=None):
        """
        :param oemof_pre: oemof preprocessing object (l)
        :param oemof_sim: oemof simulation object (l)
        :param fig_size: tuple of figsize of plots
        """
        self.oemof_sim = oemof_sim
        self.oemof_pre = oemof_pre
        self.p_results = p_results

        if fig_size is None:
            self.fig_size = (14, 8)
        else:
            self.fig_size = fig_size

        # Globals
        self.oemof_res_raw = oemof_sim.oemof_solph_results  # oemof raw result data base

        self.oemof_res = None  # oemof selected data results
        self.comp_names = None  # name of components in oemof results

        # Function calls:
        self._get_oemof_data()
        # self._plot_oemof_data()
        # self._sum_flows()
        # self._max_flows()
        # self._invest()
        self._result_to_excel()
        self._sum_CO2()
        self._total_cost()

    def _get_oemof_data(self):
        """process and arrange oemof result data"""
        logging.info("Creating result dictionary.")
        # get all esys element types and labels and put them in dictionary
        self.esys_dict = {}
        itr = 0
        for n in self.oemof_pre.esys.nodes:
            obj_type = str(type(n)).replace("<class 'oemof.solph.", "").replace("'>", "")
            self.esys_dict['k'+str(itr)] = [obj_type, n.label]
            itr += 1

        # replace long obj strings with short types
        for key in self.esys_dict:
            # print(self.esys_dict[key][0])
            if "_bus" in self.esys_dict[key][0]:
                self.esys_dict[key][0] = "bus"
            elif "_source" in self.esys_dict[key][0]:
                self.esys_dict[key][0] = "source"
            elif "_sink" in self.esys_dict[key][0]:
                self.esys_dict[key][0] = "sink"
            elif "_transformer" in self.esys_dict[key][0]:
                self.esys_dict[key][0] = "transformer"
            elif "_generic_storage" in self.esys_dict[key][0]:
                self.esys_dict[key][0] = "storage"

    def _plot_oemof_data(self):
        """plots for oemof result data"""
        logging.info("Creating plots for results.")
        # create plots of all energy system nodes of type
        for el in ['bus', 'source', 'sink', 'transformer', 'storage']:
            create_folder(os.path.join(self.p_results, el))
            # find all nodes of type and plot all their sequences
            for key in self.esys_dict:
                if self.esys_dict[key][0] == el:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])
                    nd["sequences"].plot(figsize=(25,15))
                    plt.savefig(os.path.join(self.p_results, el, self.esys_dict[key][1] + '.pdf'))
                    plt.close()

        logging.info("Energy system plots saved to: "+self.p_results+'\n')

    def _sum_CO2(self):
        '''calculates total CO2 emission over simulation time'''
        print('************************************************************')
        if self.oemof_sim.emission_limit is not None and self.oemof_sim.skip_sim is False:
            print('total CO2 Emission over simulation time: {}\n'.format(
                self.oemof_sim.oemof_model.integral_limit_emission_factor())
            )

    def _total_cost(self):
        '''exports total cost (objective function)'''
        #print('************************************************************')
        if self.oemof_sim.skip_sim is False:
            print('total cost: {}\n'.format(
                self.oemof_sim.oemof_solph_meta_results['objective']
            ))

    def _sum_flows(self):
        '''calculates total flows of buses over simulation time'''
        print('******************** total flows of the buses ********************')
        for k in ['bus']:
            # find all nodes of type and print all the flows
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])
                    print(nd["sequences"].sum())

    def _max_flows(self):
        '''calculates total flows of buses over simulation time'''
        print('******************** max flows of the buses ********************')
        for k in ['bus']:
            # find all nodes of type and print the maximum value of the flows
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])
                    print(nd["sequences"].max())

    def _invest(self):
        '''calculates total flows of buses over simulation time'''
        print('******************** investment ********************')
        for k in ['bus', 'source', 'sink', 'transformer', 'storage']:
            # find all nodes of type and print the maximum value of the flows
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])
                    if nd.get('scalars') is not None:
                        print(nd['scalars'])

    def _result_to_excel(self):
        # define the file names for the results
        excel_result = 'results.xlsx'

        # define the writer
        writer = pd.ExcelWriter(path=os.path.join(self.p_results, excel_result), engine='xlsxwriter')


        # save the meta data of simulation to a dataframe
        if self.oemof_sim.skip_sim is False:
            if self.oemof_sim.emission_limit is not None:
                meta_data = {'Total cost': self.oemof_sim.oemof_solph_meta_results['objective'],
                             'Total CO2 emission': self.oemof_sim.oemof_model.integral_limit_emission_factor()}
            else:
                meta_data = {'Total cost': self.oemof_sim.oemof_solph_meta_results['objective']}
            df_meta = pd.DataFrame.from_dict(meta_data, orient='index', columns=['Value'])


        # create new dataframes for simulation results to put needed information (names and values) together,
        # which are separated in different result dataframes created by oemof
        df_scalar = pd.DataFrame(columns=['Component', 'Invest'])
        df_sum_flows = pd.DataFrame()

        for k in ['source', 'transformer']:
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])

                    # save investment results (except for storage) in the created dataframe
                    if nd.get('scalars') is not None:
                        new_row_scalar = pd.DataFrame({'Component': [self.esys_dict[key][1]],
                                                       'Invest': [nd['scalars'][0]]})
                        df_scalar = pd.concat([df_scalar, new_row_scalar], axis=0)
                        # print(nd)

        for k in ['storage']:
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])

                    # save investment results of storage in the created dataframe
                    if nd.get('scalars') is not None:
                        new_row_scalar = pd.DataFrame({'Component': [self.esys_dict[key][1]],
                                                       'Invest': [nd['scalars'][1]]})
                        df_scalar = pd.concat([df_scalar, new_row_scalar], axis=0)

                    # write sequences results into Excel file
                    if nd.get('sequences') is not None:
                        nd['sequences'].to_excel(writer, sheet_name=self.esys_dict[key][1])

        for k in ['bus']:
            for key in self.esys_dict:
                if self.esys_dict[key][0] == k:
                    nd = solph.views.node(self.oemof_res_raw, self.esys_dict[key][1])
                    # print(f'{self.esys_dict[key][0]} ---- {self.esys_dict[key][1]}')
                    # write sequences results into Excel file
                    # and save the total flows in created dataframe
                    if nd.get('sequences') is not None:
                        nd['sequences'].to_excel(writer, sheet_name=self.esys_dict[key][1])
                        sum_flows_series = nd['sequences'].sum()
                        new_row_sum_flows = pd.DataFrame({'From, To': sum_flows_series.index,
                                                          'Total flow': sum_flows_series.values})
                        # print(f'{new_row_sum_flows}')
                        df_sum_flows = pd.concat([df_sum_flows, new_row_sum_flows], axis=0)

        pd.set_option('display.max_rows', None)
        pd.set_option('display.max_columns', None)
        # print(df_sum_flows)
        print('******************** investment saved to excel file ********************')
        print(df_scalar)

        # Drop certain unwanted rows which contains status data but not flows
        # Status data are only generated for Transformers with nonconvex operation areas
        # df_sum_flows.drop(df_sum_flows[df_sum_flows['From, To'].astype(str).str.contains('status')].index,inplace=True)
        df_sum_flows = df_sum_flows[df_sum_flows['From, To'].astype(str).str.contains('flow', case=False, na=False)]
        # print(df_sum_flows)
        # print(f'col num: {df_sum_flows.shape[0]}')

        # set proper name for the columns of flow directions for the total flows
        df_sum_flows['From, To'] = df_sum_flows['From, To'].astype(str).str.replace('(', '', regex=False)\
            .str.replace(')', '', regex=False).str.replace(", 'flow'", '', regex=False)\
            .str.replace("'", '', regex=False).str.replace(", None", '', regex=False)
        df_sum_flows[['From', 'To']] = df_sum_flows['From, To'].str.split(',', expand=True)
        df_sum_flows.drop('From, To', axis=1, inplace=True)

        # put the results in wanted order (values in last column)
        cols_sum_flows = list(df_sum_flows.columns)
        cols_sum_flows.append(cols_sum_flows.pop(cols_sum_flows.index('Total flow')))
        df_sum_flows = df_sum_flows[cols_sum_flows]
        print('******************** total flows saved to excel file ********************')

        # write results of investment and total flows to Excel file
        if self.oemof_sim.skip_sim is False:
            df_meta.to_excel(writer, sheet_name='meta')
        df_scalar.to_excel(writer, sheet_name='invest', index=False)
        df_sum_flows.to_excel(writer, sheet_name='sum_flows', index=False)

        # close the writer to save the results completely to the file
        writer.close()




import math

import pyomo.environ as po
from logic.utilities import get_info_from_excel_data
import pandas as pd

def chp_funding_limit(pre=None, om=None, esys=None, xls_data=None, chp_funding_flh=None, chp_label=None):
    """
    Specify a feed in limit for a specific source and flow
    :param obj om: oemof energy system model
    :param obj esys: oemof energy system model
    :param obj xls_data: pandas excel data to search for data of node objects
    :param chp_funding_flh: maximum full load hours subsidized per year
    :param str chp_label: label of transformer
    """

    if chp_funding_flh is not None:
        if chp_label is None:
            chp_label=[]
            for i, chp in pre.nodes_data["chp"].iterrows():
                if chp['active']:
                    chp_label.append(chp['label'])
        def sum_invest_chp():
            sum_invest_chp = 0
            # get bus info from excel data and set trafo objects
            for label in chp_label:
                trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=label)
                trafo_to_el_bus_label = trafo_dict['to 2']
                trafo = esys.groups[label]
                trafo_to_el_bus = esys.groups[trafo_to_el_bus_label]
                # print(type(trafo))
                # print(type(trafo_to_el_bus))
                sum_invest_chp += om.InvestmentFlowBlock.invest[trafo, trafo_to_el_bus]
            return sum_invest_chp

        # set limited feed in flows
        limited_flows = [
            # of one component (feed in trafo)
            # if there are more components with the attribute,
            # the sum of all flows will be limited
            k for (k, v) in om.flows.items() if hasattr(v, "runtime_limit_factor")
        ]

        limit_name = "integral_limit_chp_funding"
        setattr(om, limit_name, po.Expression(
            expr=sum(om.flow[inflow, outflow, t]
                     for (inflow, outflow) in limited_flows
                     for t in om.TIMESTEPS)))

        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(getattr(om, limit_name)
                # <= chp_funding_flh * om.InvestmentFlowBlock.invest[trafo, trafo_to_el_bus]))
                <= chp_funding_flh * sum_invest_chp()))
                )

# def invest_min_limit(pre=None, om=None, esys=None, xls_data=None, invest_min=None):
#     """
#     Specify a feed in limit for a specific source and flow
#     :param obj om: oemof energy system model
#     :param obj esys: oemof energy system model
#     :param obj xls_data: pandas excel data to search for data of node objects
#     :param chp_funding_flh: maximum full load hours subsidized per year
#     :param invest_min: if minimum allowed capacities of investment is considered
#     """
#
#     if invest_min:
#         invest_component=[]
#         component_types = ['transformers', 'chp', 'hp']
#         component_dfs = [pre.nodes_data[t] for t in component_types]
#
#         for t, df in zip(component_types, component_dfs):
#             for i,j in df.iterrows():
#                 if j['active'] and j['invest']:
#                     invest_component.append(j['label'])
#
#         for label in invest_component:
#             trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=label)
#             trafo_to_bus_label = trafo_dict['to 2'] if isinstance(trafo_dict['to 2'],str) else trafo_dict['to 1']
#             invest_min = trafo_dict['minimum']
#             trafo = esys.groups[label]
#             trafo_to_bus = esys.groups[trafo_to_bus_label]
#             invest = om.InvestmentFlowBlock.invest[trafo, trafo_to_bus]
#
#             limit_name = "investment_min"
#             setattr(om, limit_name, po.Expression(
#                 expr=(invest - 0.5*invest_min)**2 - (0.5*invest_min)**2
#             ))
#
#             setattr(om, limit_name + "_constraint", po.Constraint(
#                 expr=(getattr(om, limit_name)
#                       >=0)
#             ))

def WNS40_funding_limit(om=None, esys=None, xls_data=None, WNS40_renewable_share_limit=None,
                        demand_th_label=None):
    """
    Specify a feed in limit for a specific source and flow
    :param obj om: oemof energy system model
    :param obj esys: oemof energy system model
    :param obj xls_data: pandas excel data to search for data of node objects
    :param WNS40_renewable_share: desired renewable share
    :param str bmk_label: label of transformer
    :param str demand_th_label: label of thermal demand
    """

    # get bus info from excel data and set biomass boiler objects
    # trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=bmk_label)
    # trafo_to_bus_label = trafo_dict['to 1']
    # trafo = esys.groups[bmk_label]
    # trafo_to_bus = esys.groups[trafo_to_bus_label]

    demand_dict = get_info_from_excel_data(xls_data=xls_data, label=demand_th_label)
    demand_th = demand_dict['nominal value']

    if WNS40_renewable_share_limit is not None:

        # set limited feed in flows
        limited_flows = [
            # of one component (feed in trafo)
            # if there are more components with the attribute,
            # the sum of all flows will be limited
            k for (k, v) in om.flows.items() if hasattr(v, "renewable_share")
        ]

        heat_production = [
            k for (k, v) in om.flows.items() if hasattr(v, "generator")
        ]

        # # flows_bm = {(trafo, trafo_to_bus)}
        # flows_bm = [
        #     k for (k, v) in om.flows.items() if hasattr(v, "BM")
        # ]

        limit_name = "integral_limit_WNS40_funding_1"
        setattr(om, limit_name, po.Expression(
            expr=(sum(om.flow[inflow, outflow, t]
                     for (inflow, outflow) in limited_flows
                     for t in om.TIMESTEPS)) #/
                  #sum(om.flow[inflow, outflow, t]
                  #   for (inflow, outflow) in heat_production
                  #   for t in om.TIMESTEPS)
        ))

        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(getattr(om, limit_name)
                >= WNS40_renewable_share_limit * demand_th)
        ))

        # limit_name = "integral_limit_WNS40_funding_2"
        # setattr(om, limit_name, po.Expression(
        #     expr=(sum(om.flow[inflow, outflow, t]
        #              for (inflow, outflow) in limited_flows
        #              for t in om.TIMESTEPS) -
        #           sum(om.flow[inflow, outflow, t]
        #              for (inflow, outflow) in flows_bm
        #              for t in om.TIMESTEPS)) #/
        #           #sum(om.flow[inflow, outflow, t]
        #           #   for (inflow, outflow) in heat_production
        #           #   for t in om.TIMESTEPS)
        # ))
        #
        # setattr(om, limit_name + "_constraint", po.Constraint(
        #     expr=(getattr(om, limit_name)
        #           >= WNS40_renewable_share_limit * 0.5 * demand_th)
        # ))

        #################### limitation of total produced heat ####################
        limit_name = "integral_limit_WNS40_funding_3"
        setattr(om, limit_name, po.Expression(
            expr=(sum(om.flow[inflow, outflow, t]
                     for (inflow, outflow) in heat_production
                     for t in om.TIMESTEPS))
        ))

        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(getattr(om, limit_name)
                  <= demand_th * 1.05)
        ))

# def chp_feed_in_limit(om=None, esys=None, xls_data=None, chp_label=None, feed_in_trafo_label=None):
#     """
#     Constrain with feed in limit "full_load_hours" from excel for a specific transformer source and feed in trafo
#     :param obj om: oemof energy system model
#     :param obj esys: oemof energy system model
#     :param obj xls_data: pandas excel data to search for data of node objects
#     :param str chp_label: label of transformer
#     :param str feed_in_trafo_label: label of feed in transformer
#     """
#
#     # get bus info from excel data and set trafo objects
#     trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=chp_label)
#     trafo_to_bus_label = trafo_dict['to 2']
#     trafo = esys.groups[chp_label]
#     trafo_to_bus = esys.groups[trafo_to_bus_label]
#     # set full load hours
#     flh = trafo_dict['full_load_hours']
#     # n_el = trafo_dict['efficiency 2']
#
#     # get bus info from excel data and set feed in trafo objects
#     feed_in_trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=feed_in_trafo_label)
#     feed_in_trafo_to_bus_label = feed_in_trafo_dict['to 1']
#     feed_in_trafo = esys.groups[feed_in_trafo_label]
#     feed_in_to_bus = esys.groups[feed_in_trafo_to_bus_label]
#
#     # set feed in flows
#     flows = {(feed_in_trafo, feed_in_to_bus)}
#
#     limit_name = "integral_limit_" + chp_label + "_feed_in"
#     setattr(om, limit_name, po.Expression(
#         expr=sum(om.flow[inflow, outflow, t]
#                  for (inflow, outflow) in flows
#                  for t in om.TIMESTEPS)))
#
#     setattr(om, limit_name + "_constraint", po.Constraint(
#         expr=(getattr(om, limit_name) <= flh * om.InvestmentFlowBlock.invest[trafo, trafo_to_bus])))

def roof_area_limit(om=None, esys=None, xls_data=None, solar_thermal_label=None,
                    pv_label=None, max_roof_area=None):
    """
    Specify a roof area limit for solar systems
    :param obj om: oemof model to use constraint on
    :param obj esys: oemof energy system model
    :param obj xls_data: pandas excel data to search for data of node objects
    :param str solar_thermal_label: solar thermal label for oemof object from energy system
    :param str pv_label: pv label for oemof object from energy system
    :param float max_roof_area: Maximum roof area
    """
    limit_name = "roof_area_limit_" # + str(pv_label) + "_" + str(solar_thermal_label)
    # needed objects for constraint
    solar_thermal = None
    solar_thermal_bus = None
    pv = None
    pv_bus = None

    # get bus info from excel data and set objects
    if solar_thermal_label is not None:
        solar_thermal_dict = get_info_from_excel_data(xls_data=xls_data, label=solar_thermal_label)
        solar_thermal_bus_label = solar_thermal_dict['to']
        solar_thermal = esys.groups[solar_thermal_label]
        solar_thermal_bus = esys.groups[solar_thermal_bus_label]

    if pv_label is not None:
        pv_dict = get_info_from_excel_data(xls_data=xls_data, label=pv_label)
        pv_bus_label = pv_dict['to']
        pv = esys.groups[pv_label]
        pv_bus = esys.groups[pv_bus_label]

    # differentiate depending on if pv and thermal was given or only one of them
    if solar_thermal_label is None and pv_label is not None:
        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(om.InvestmentFlowBlock.invest[pv, pv_bus] <= max_roof_area)))

    elif solar_thermal_label is not None and pv_label is None:
        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(om.InvestmentFlowBlock.invest[solar_thermal, solar_thermal_bus] <= max_roof_area)))

    elif solar_thermal_label is not None and pv_label is not None:
        setattr(om, limit_name + "_constraint", po.Constraint(
            expr=(om.InvestmentFlowBlock.invest[solar_thermal, solar_thermal_bus] +
                  om.InvestmentFlowBlock.invest[pv, pv_bus] <= max_roof_area)))


def vbH_min_limit(pre=None, om=None, esys=None, xls_data=None, vbH_min=None, Anlage_name=None, Anlage_label=None):
    """
    Specify a feed in limit for a specific source and flow
    :param obj om: oemof energy system model
    :param obj esys: oemof energy system model
    :param obj xls_data: pandas excel data to search for data of node objects
    :param chp_funding_flh: maximum full load hours subsidized per year
    :param str chp_label: label of transformer
    """

    if vbH_min is not None:
        if Anlage_name is not None and Anlage_label is not None:

        # get bus info from excel data and set trafo objects
            trafo_dict = get_info_from_excel_data(xls_data=xls_data, label=Anlage_name)
            Anlage_capacity = trafo_dict['capacity']

            # set limited feed in flows
            limited_flows = [
                # of one component (feed in trafo)
                # if there are more components with the attribute,
                # the sum of all flows will be limited
                k for (k, v) in om.flows.items() if hasattr(v, Anlage_label)
            ]

            limit_name = f"integral_limit_vbH_{Anlage_label}"
            setattr(om, limit_name, po.Expression(
                expr=sum(om.flow[inflow, outflow, t]
                         for (inflow, outflow) in limited_flows
                         for t in om.TIMESTEPS)))

            setattr(om, limit_name + "_constraint", po.Constraint(
                expr=(getattr(om, limit_name)
                    # <= chp_funding_flh * om.InvestmentFlowBlock.invest[trafo, trafo_to_el_bus]))
                    >= vbH_min * Anlage_capacity))
                    )
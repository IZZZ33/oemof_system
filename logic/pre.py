import logging
import pandas as pd
import oemof.solph as solph
from oemof.tools import logger


class Pre(object):
    def __init__(self, project_name=None, oemof_input_file=None, start_date=None, time_steps=None, time_step_freq=None,
                 p_results=None):
        """
        :param oemof_input_file: path to oemof .xlsx input file
        :param project_name: Name of project
        """

        # Input data:
        self.project_name = project_name
        self.oemof_input_file = oemof_input_file
        self.xls_oemof_input_file = pd.ExcelFile(oemof_input_file)
        self.start_date = start_date
        self.time_steps = time_steps
        self.time_step_freq = time_step_freq
        self.p_results = p_results

        # Working data:
        self.nodes_data = None  # nodes data from excel
        self.nodes = None   # nodes as oemof objects
        self.esys = None

        # function calls:
        self._read_nodes_from_excel()
        self._create_energysystem_nodes()
        self._create_energysystem_oemof()

    def _read_nodes_from_excel(self):
        """Module for reading nodes of EnergySystem based on xls-file"""

        self.nodes_data = {
            "buses": self.xls_oemof_input_file.parse("buses"),
            "sources": self.xls_oemof_input_file.parse("sources"),
            "demand": self.xls_oemof_input_file.parse("demand"),
            "transformers": self.xls_oemof_input_file.parse("transformers"),
            'trans_offset': self.xls_oemof_input_file.parse("trans_offset"),
            "chp": self.xls_oemof_input_file.parse("chp"),
            "feed_in_trafo": self.xls_oemof_input_file.parse("feed_in_trafo"),
            "hp": self.xls_oemof_input_file.parse("hp"),
            "z_hp": self.xls_oemof_input_file.parse("z_hp"),
            "timeseries": self.xls_oemof_input_file.parse("time_series"),
            "storages": self.xls_oemof_input_file.parse("storages"),
            "renewables": self.xls_oemof_input_file.parse("renewables")
        }

        # Set the datetime index. Excel templates can report formatted-but-empty
        # rows as used rows. If another profile is written into those rows,
        # pandas keeps them and promotes the integer timestamps to floats because
        # the trailing timestamps are NaN. Trim only such trailing rows and reject
        # gaps or genuinely non-integer hour indices.
        timeseries = self.nodes_data["timeseries"]
        if "timestamp" not in timeseries.columns:
            raise ValueError("The time_series sheet has no 'timestamp' column.")

        timestamps = pd.to_numeric(timeseries["timestamp"], errors="coerce")
        trailing_start = len(timestamps)
        while trailing_start > 0 and pd.isna(timestamps.iloc[trailing_start - 1]):
            trailing_start -= 1

        if timestamps.iloc[:trailing_start].isna().any():
            bad_rows = (timestamps.iloc[:trailing_start].isna().to_numpy().nonzero()[0] + 2).tolist()
            raise ValueError(
                "The timestamp column in time_series contains missing or non-numeric "
                f"values in Excel row(s): {bad_rows}."
            )

        if trailing_start < len(timeseries):
            logging.warning(
                "Ignoring %d trailing time_series row(s) without timestamps in %s.",
                len(timeseries) - trailing_start,
                self.oemof_input_file,
            )
            timeseries = timeseries.iloc[:trailing_start].copy()
            timestamps = timestamps.iloc[:trailing_start]

        if timeseries.empty:
            raise ValueError("The time_series sheet contains no timestamp values.")
        if not timestamps.mod(1).eq(0).all():
            raise ValueError("All timestamp values in time_series must be integer hour indices.")

        integer_timestamps = timestamps.astype("int64")
        if integer_timestamps.duplicated().any() or not integer_timestamps.is_monotonic_increasing:
            raise ValueError("The timestamp values in time_series must be unique and increasing.")
        if len(integer_timestamps) > 1 and not integer_timestamps.diff().iloc[1:].eq(1).all():
            raise ValueError("The timestamp values in time_series must be consecutive integer hours.")

        timeseries["timestamp"] = integer_timestamps.to_numpy()
        timeseries.set_index("timestamp", inplace=True)
        timeseries.index = pd.to_datetime(timeseries.index)
        self.nodes_data["timeseries"] = timeseries

        logging.info("Data from Excel file {} imported.".format(self.oemof_input_file))

    def _create_energysystem_nodes(self):
        """Create nodes (oemof objects) from node dict"""

        nd = self.nodes_data
        if not nd:
            raise ValueError("No nodes data provided.")

        nodes = []

        # Create Bus objects from buses table
        busd = {}

        for i, b in nd["buses"].iterrows():
            if b["active"]:
                bus = solph.Bus(label=b["label"])
                nodes.append(bus)

                busd[b["label"]] = bus
                if b["excess"]:
                    nodes.append(
                        solph.components.Sink(
                            label=b["label"] + "_excess",
                            inputs={
                                busd[b["label"]]: solph.Flow(
                                    variable_costs=b["excess costs"]
                                )
                            },
                        )
                    )
                if b["shortage"]:
                    nodes.append(
                        solph.components.Source(
                            label=b["label"] + "_shortage",
                            outputs={
                                busd[b["label"]]: solph.Flow(
                                    variable_costs=b["shortage costs"]
                                )
                            },
                        )
                    )

        # Create Source objects from table 'sources'
        for i, cs in nd["sources"].iterrows():
            if cs["active"]:
                nodes.append(
                    solph.components.Source(
                        label=cs["label"],
                        outputs={
                            busd[cs["to"]]: solph.Flow(
                                variable_costs=cs["variable costs"],
                                custom_attributes={"emission_factor": cs["emission factor"]},
                                nominal_value=cs["nominal value"],
                                full_load_time_max = cs["full load time"]
                            )
                        },
                    )
                )

        # Create renewables objects with fixed time series from 'renewables' table
        for i, re in nd["renewables"].iterrows():
            if re["active"]:
                # set static outflow values
                outflow_args = {"nominal_value": re["capacity"]}
                # get time series for node and parameter
                for col in nd["timeseries"].columns.values:
                    if col.split(".")[0] == re["type"]:
                        # outflow_args[col.split(".")[1]] = nd["timeseries"][col]
                        outflow_args['max'] = nd["timeseries"][col]
                outflow_args.update(custom_attributes={"emission_factor": re['emission_factor']})
                if re['invest'] == 1:
                    outflow_args.update({'nominal_value': None})
                    ep_costs = re['ep_costs']
                    outflow_args.update({'investment': solph.Investment(
                        ep_costs=ep_costs,
                        maximum=re['maximum']
                    )})
                # create
                nodes.append(
                    solph.components.Source(
                        label=re["label"],
                        outputs={busd[re["to"]]: solph.Flow(**outflow_args)},
                    )
                )

        # Create Sink objects with fixed time series from 'demand' table
        for i, de in nd["demand"].iterrows():
            if de["active"]:
                # set static inflow values
                inflow_args = {"nominal_value": de["nominal value"]}
                found_demandseries = False
                # get time series for node and parameter
                for col in nd["timeseries"].columns.values:
                    if col.split(".")[0] == de["label"]:
                        inflow_args[col.split(".")[1]] = nd["timeseries"][col]
                        found_demandseries = True

                if not found_demandseries:
                    demand_label = str(de.get("label", "")).lower()
                    is_dhw_demand = "dhw" in demand_label or "domestic hot water" in demand_label
                    is_loss_demand = demand_label in {"loss", "network_heat_loss"}
                    if is_loss_demand and "Loss.fix" in nd["timeseries"].columns:
                        col = "Loss.fix"
                    elif is_dhw_demand and "Last_DHW.fix" in nd["timeseries"].columns:
                        col = "Last_DHW.fix"
                    elif "Last_SH.fix" in nd["timeseries"].columns:
                        col = "Last_SH.fix"
                    else:
                        col = "Last_th.fix"
                    inflow_args[col.split(".")[1]] = nd["timeseries"][col]

                # create
                nodes.append(
                    solph.components.Sink(
                        label=de["label"],
                        inputs={busd[de["from"]]: solph.Flow(**inflow_args)},
                    )
                )

        # Create CHP  from 'chp' table
        for i, chp in nd["chp"].iterrows():
            self.flow_limited_keyword = {}
            if chp["active"]:
                outflow_args_chp = ({})
                if chp['invest'] == 1:
                    ep_costs = chp['ep_costs']
                    minimum = chp['minimum']
                    maximum = chp['maximum']
                    nonconvex = chp['nonconvex']
                    offset = chp['offset']

                    outflow_args_chp = ({'investment': solph.Investment(
                        existing=chp['existing'],ep_costs=ep_costs, maximum=maximum,
                        minimum=minimum, offset=offset,
                        # sorgt dafür, dass nicht-konvexe Investment durchgeführt werden kann
                        nonconvex=nonconvex
                    )})

                    trafo = solph.components.Transformer(
                        label=chp["label"],
                        inputs={busd[chp["from"]]: solph.Flow()},
                        outputs={
                            busd[chp["to 1"]]: solph.Flow(
                                # custom_attributes={
                                #     "generator": 1
                                # }
                                # if chp["renewable_share"] == 0
                                # else {"renewable_share": chp["renewable_share"],
                                #       "generator": 1
                                #       },
                            ),
                            busd[chp["to 2"]]: solph.Flow(**outflow_args_chp)
                        },
                        conversion_factors={busd[chp["to 1"]]: chp["efficiency 1"],
                                            busd[chp["to 2"]]: chp["efficiency 2"]}
                    )

                else:
                    trafo = solph.components.Transformer(
                        label=chp["label"],
                        inputs={busd[chp["from"]]: solph.Flow()},
                        outputs={
                            busd[chp["to 1"]]: solph.Flow(
                                nominal_value=chp["maximum"],
                            ),
                            busd[chp["to 2"]]: solph.Flow(**outflow_args_chp)
                        },
                        conversion_factors={busd[chp["to 1"]]: chp["efficiency 1"],
                                            busd[chp["to 2"]]: chp["efficiency 2"]})

                nodes.append(trafo)

        # Create feed_in_trafo objects from 'feed_in_trafo' table
        for i, fit in nd["feed_in_trafo"].iterrows():
            if fit["active"]:
                # set static outflow values
                outflow_args_fit = {"variable_costs": fit["variable input costs"]}

                trafo = solph.components.Transformer(
                    label=fit["label"],
                    inputs={busd[fit["from"]]: solph.Flow()},
                    outputs={
                        busd[fit["to 1"]]: solph.Flow(
                            custom_attributes={"emission_factor": fit["emission_factor"]},
                            # if not fit["runtime_limit"]
                            # else {"emission_factor": fit["emission_factor"],
                            #       "runtime_limit_factor": 1
                            #       },
                            **outflow_args_fit)
                    },
                    conversion_factors={busd[fit["to 1"]]: fit["efficiency 1"]})
                nodes.append(trafo)

        # Create heat pump objects from 'hp' table
        for i, hp in nd["hp"].iterrows():
            if hp["active"]:
                outflow_args_hp = ({})
                if hp['invest'] == 1:
                    ep_costs = hp['ep_costs']
                    minimum = hp['minimum']
                    maximum = hp['maximum']
                    nonconvex = hp['nonconvex']
                    offset = hp['offset']

                    outflow_args_hp = ({'investment': solph.Investment(
                        ep_costs=ep_costs, maximum=maximum, minimum=minimum, offset=offset,
                        # sorgt dafür, dass nicht-konvexe Investment durchgeführt werden kann
                        nonconvex=nonconvex
                    )})

                cop = None
                for col in nd["timeseries"].columns.values:
                    if col.split(".")[0] == hp["type"]:
                        cop = nd["timeseries"][col].to_list()
                        conv_el = [1 / x for x in cop]  # conversion factor for electric source of hp
                        conv_therm = [(x - 1) / x for x in cop]  # conversion factor for thermal source of hp
                if cop is None:
                    fallback_cop = float(hp.get("efficiency 1", 1.0) or 1.0)
                    cop = [fallback_cop] * len(nd["timeseries"])
                    conv_el = [1 / x for x in cop]
                    conv_therm = [(x - 1) / x for x in cop]

                trafo = None
                if hp["n_inputs"] == 1:
                    trafo = solph.components.Transformer(
                        label=hp["label"],
                        inputs={busd[hp["from"]]: solph.Flow()},
                        outputs={
                            busd[hp["to 1"]]: solph.Flow(
                                # custom_attributes={
                                #     "generator": 1
                                # }
                                # if hp["renewable_share"]==0
                                # else {"renewable_share": hp["renewable_share"],
                                #       "generator": 1
                                #       },
                                **outflow_args_hp)
                        },
                        conversion_factors={busd[hp["to 1"]]: cop},
                    )
                elif hp["n_inputs"] == 2:
                    trafo = solph.components.Transformer(
                        label=hp["label"],
                        inputs={busd[hp["from"]]: solph.Flow(), busd[hp["from_2"]]: solph.Flow()},
                        outputs={
                            busd[hp["to 1"]]: solph.Flow(
                                # custom_attributes={
                                #     "generator": 1
                                # }
                                # if hp["renewable_share"] == 0
                                # else {"renewable_share": hp["renewable_share"],
                                #       "generator": 1
                                #       },
                                **outflow_args_hp)
                        },
                        conversion_factors={busd[hp["from"]]: conv_el, busd[hp["from_2"]]: conv_therm},
                    )

                nodes.append(trafo)

        for i, z_hp in nd["z_hp"].iterrows():
            if z_hp["active"]:
                outflow_args_hp = ({})

                trafo = None
                if z_hp["n_inputs"] == 1:
                    trafo = solph.components.Transformer(
                        label=z_hp["label"],
                        inputs={busd[z_hp["from"]]: solph.Flow(
                            nominal_value=z_hp["capacity_1"],
                        )},
                        outputs={
                            busd[z_hp["to 1"]]: solph.Flow(
                                **outflow_args_hp)
                        },
                        conversion_factors={busd[z_hp["to"]]: z_hp["efficiency_1"]},
                    )
                elif z_hp["n_inputs"] == 2:
                    trafo = solph.components.Transformer(
                        label=z_hp["label"],
                        inputs={busd[z_hp["from"]]: solph.Flow(nominal_value=z_hp["capacity_1"],),
                                busd[z_hp["from_2"]]: solph.Flow(nominal_value=z_hp["capacity_2"],)},
                        outputs={
                            busd[z_hp["to"]]: solph.Flow(
                                **outflow_args_hp)
                        },
                        conversion_factors={busd[z_hp["from"]]: z_hp["efficiency_1"],
                                            busd[z_hp["from_2"]]: z_hp["efficiency_2"]},
                    )

                nodes.append(trafo)

        # Create Transformer objects from 'transformers' table
        for i, t in nd["transformers"].iterrows():
            if t["active"]:
                # inflow_args = ({})
                outflow_args = ({})

                if t['invest'] == 1:
                    ep_costs = t['ep_costs']
                    minimum = t['minimum']
                    maximum = t['maximum']
                    nonconvex = t['nonconvex']
                    offset = t['offset']

                    outflow_args.update({'investment': solph.Investment(
                        ep_costs=ep_costs, maximum=maximum, minimum=minimum, offset=offset,
                        nonconvex=nonconvex),
                        "variable_costs": t["variable input costs"]
                    })

                    trafo = solph.components.Transformer(
                        label=t["label"],
                        inputs={busd[t["from"]]: solph.Flow()},
                        outputs={
                            busd[t["to"]]: solph.Flow(
                                # custom_attributes={
                                #     "generator": 1,
                                #     # "BM": t["BM"],
                                # }
                                # if t["renewable_share"] == 0
                                # else {"renewable_share": t["renewable_share"],
                                #       "generator": 1,
                                #       # "BM": t["BM"]
                                #       },
                                # nonconvex=solph.NonConvex() if t["Anlage"] != "-" else None,
                                # custom_attributes = {t["Anlage"]:1} if t["Anlage"] != "-" else None,
                                **outflow_args)
                        },
                        conversion_factors={busd[t["to"]]: t["efficiency"]},
                    )

                else:
                #     cop = None
                #     for col in nd['timeseries'].columns.values:
                #         if col.split(".")[0] == t["RKW"]:
                #             # max_load_RKW = nd['timeseries'][col].to_list()
                #             outflow_args[col.split(".")[1]] = nd["timeseries"][col]
                #         if col.split(".")[0] == t["WP"]:
                #             if col.split(".")[1] == "COP":
                #                 cop = nd["timeseries"][col]
                #             else:
                #                 outflow_args[col.split(".")[1]] = nd["timeseries"][col]

                    trafo = solph.components.Transformer(
                        label=t["label"],
                        inputs={
                            busd[t["from"]]: solph.Flow()},
                        outputs={
                            busd[t["to"]]: solph.Flow(
                                nominal_value=t["capacity"],
                                full_load_time_max = t["full_load_t"],
                                # max=max_load_RKW,
                                # nonconvex=solph.NonConvex() if t["Anlage"] != "-" else None,
                                # custom_attributes={t["Anlage"]: 1} if t["Anlage"] != "-" else None,
                                **outflow_args
                            )},
                        # conversion_factors={busd[t["to"]]: t["efficiency"]} if cop is None else {busd[t["to"]]: cop},
                        conversion_factors={busd[t["to"]]: t["efficiency"]},
                    )

                nodes.append(trafo)


        for i, to in nd["trans_offset"].iterrows():
            if to["active"]:
                 inflow_args = ({})
                 outflow_args = ({})

            load_min = to["min_load"]
            load_max = to["max_load"]
            eta_min = to["eta_min"]
            eta_max = to["eta_max"]

            Q_out_min = to["capacity"] * load_min
            Q_out_max = to["capacity"]
            el_in_min = Q_out_min / eta_min
            el_in_max = Q_out_max / eta_max

            c1 = (Q_out_max - Q_out_min) / (el_in_max - el_in_min)
            c0 = Q_out_max - c1 * el_in_max

            trafo = solph.components.OffsetTransformer(
                label=to["label"],
                inputs={busd[to["from"]]: solph.Flow(
                nominal_value=to["capacity"],
                max=to["max_load"],
                min=to["min_load"],
                nonconvex=solph.NonConvex()
            )},
                outputs={busd[to["to"]]: solph.Flow()},
                coefficients= [c0, c1]
            )

            nodes.append(trafo)


        for i, s in nd["storages"].iterrows():
            if s["active"]:
                if s["invest"] == 1:
                    nodes.append(
                        solph.components.GenericStorage(
                            label=s["label"],
                            inputs={
                                busd[s["bus"]]: solph.Flow(
                                    nominal_value=s["capacity inflow"],
                                    variable_costs=s["variable input costs"],
                                )
                            },
                            outputs={
                                busd[s["bus"]]: solph.Flow(
                                    nominal_value=s["capacity outflow"],
                                    variable_costs=s["variable output costs"],
                                )
                            },
                            investment = solph.Investment(ep_costs=s["ep_costs"],
                                                          maximum=s["max capacity"],
                                                          minimum=s["min capacity"]),
                            loss_rate=s["loss rate"],
                            initial_storage_level=s["initial storage level"],
                            max_storage_level=s["max storage level"],
                            min_storage_level=s["min storage level"],
                            inflow_conversion_factor=s["efficiency inflow"],
                            outflow_conversion_factor=s["efficiency outflow"],
                        )
                    )
                else:
                    nodes.append(
                        solph.components.GenericStorage(
                            label=s["label"],
                            inputs={
                                busd[s["bus"]]: solph.Flow(
                                    nominal_value=s["capacity inflow"],
                                    variable_costs=s["variable input costs"],
                                )
                            },
                            outputs={
                                busd[s["bus"]]: solph.Flow(
                                    nominal_value=s["capacity outflow"],
                                    variable_costs=s["variable output costs"],
                                )
                            },
                            nominal_storage_capacity=s["nominal capacity"],
                            loss_rate=s["loss rate"],
                            initial_storage_level=s["initial storage level"],
                            max_storage_level=s["max storage level"],
                            min_storage_level=s["min storage level"],
                            inflow_conversion_factor=s["efficiency inflow"],
                            outflow_conversion_factor=s["efficiency outflow"],
                        )
                    )

        self.nodes = nodes
        logging.info("Nodes from Excel file {} created.".format(self.oemof_input_file))

    def _create_energysystem_oemof(self):

        logger.define_logging()
        # create time index for optimization
        datetime_index = pd.date_range(self.start_date, periods=self.time_steps, freq=self.time_step_freq)

        # model creation and solving
        logging.info("Creating Oemof Energy System")

        # initialisation of the energy system
        self.esys = solph.EnergySystem(timeindex=datetime_index, infer_last_interval=True)

        # add nodes and flows to energy system
        self.esys.add(*self.nodes)

        print("*********************************************************")
        print("The following objects have been created from excel sheet:")
        for n in self.esys.nodes:
            oobj = str(type(n)).replace("<class 'oemof.solph.", "").replace("'>", "")
            print(oobj + ":", n.label)
        print("*********************************************************")
        logging.info("Energy System creation for Scenario {} done.".format(self.project_name))

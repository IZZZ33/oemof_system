import streamlit as st
import pandas as pd
import sys
import os
import matplotlib.pyplot as plt
import plotly.subplots as sp
import plotly.express as px
import plotly.graph_objects as go
import seaborn as sns
import ast
import re
from pathlib import Path
from logic.project_paths import get_project_results_dir, get_results_root
import argparse

TECH_MAPPING = {
    "B_NG": "Natural gas boiler",
    "B_BG": "Biogas boiler",
    "B_BM": "Biomass boiler",
    "P2H": "Power to Heat",
    "CHP_NG": "Combined heat and power (natural gas)",
    "CHP_BG": "Combined heat and power (Biogas)",
    "ASHP": "Air source heat pump",
    "GSHP_C": "Ground source heat pump (collector)",
    "GSHP_B": "Ground source heat pump (borehole)"
}

RESULTS_DIR = get_results_root()

def get_project_from_cli():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project", default=None)
    args, _ = parser.parse_known_args()
    return args.project

class MultiScenarioViewer:
    def __init__(self, project_name: str):
        self.project_name = project_name
        self.scenario_data = self.load_project_scenarios(project_name)

    def natural_key(self, s):
        return [int(text) if text.isdigit() else text.lower()
                for text in re.split(r'(\d+)', str(s))]

    @staticmethod
    def comparison_chart_width(scenario_count: int) -> int:
        """Keep few-scenario charts compact and give larger sets more room."""
        return max(720, min(2200, 420 + 150 * max(int(scenario_count), 1)))

    @staticmethod
    def comparison_bar_width(scenario_count: int) -> float:
        """Use narrow bars for sparse comparisons, widening gradually."""
        count = max(int(scenario_count), 1)
        return min(0.50, 0.16 + 0.07 * count)

    def load_project_scenarios(self, project_name: str):
        scenario_data = {}

        project_root = Path(RESULTS_DIR) / project_name
        if not project_root.exists():
            st.warning(f"No results folder found for project '{project_name}': {project_root}")
            return scenario_data

        for scenario_dir in project_root.iterdir():
            if not scenario_dir.is_dir():
                continue

            xlsx_path = scenario_dir / "results.xlsx"
            if not xlsx_path.is_file():
                continue

            # keep key simple now (scenario only) OR still "project/scenario"
            scenario_key = scenario_dir.name  # ✅ only scenarios of this project

            try:
                xls = pd.ExcelFile(xlsx_path)
                data = {sheet: xls.parse(sheet) for sheet in xls.sheet_names}
                scenario_data[scenario_key] = data
            except Exception as e:
                st.warning(f"Failed to load {scenario_key}: {e}")

        return scenario_data

    def get_scenario_colors(self):
        scenarios = sorted(self.scenario_data.keys())
        if not scenarios:
            return {}
        colors = sns.color_palette("Set2", n_colors=len(scenarios)).as_hex()
        return dict(zip(scenarios, colors))

    def show_summary_comparison(self):
        st.subheader("💰 CO₂ and Cost Summary")

        summary_rows = []
        for scenario, data in self.scenario_data.items():
            try:
                meta = data["meta"].copy()
                meta.set_index(meta.columns[0], inplace=True)
                total_cost = float(str(meta.loc["Total cost", "Value"]).replace(",", "."))
                total_emission = float(str(meta.loc["Total CO2 emission", "Value"]).replace(",", "."))
                summary_rows.append({
                    "Scenario": scenario,
                    "Total Cost (€)": total_cost,
                    "Total CO₂ Emission (kg)": total_emission
                })
            except Exception as e:
                st.warning(f"Error reading meta sheet for {scenario}: {e}")

        if summary_rows:
            df = pd.DataFrame(summary_rows)
            styled_df = df.style.format({
                "Total Cost (€)": "{:.2f}",
                "Total CO₂ Emission (kg)": "{:.2f}"
            }).set_properties(**{
                'color': 'black',
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'thead th','props': [('color', 'black'),
                                                 ('font-weight', 'bold'),
                                                 ('border-bottom', '2px solid black'),
                                                 ('background-color', '#f9f9f9')]},
                {'selector': 'td','props': [('color', 'black'), ('padding', '6px')]}
            ])
            styled_df = styled_df.hide(axis="index")
            st.write(styled_df)

            # Plot bar chart
            fig = px.bar(df.melt(id_vars="Scenario"),
                         x="Scenario", y="value", color="variable",
                         barmode="group",
                         labels={"value": "Value",
                                 "variable": " "
                                 },
                         title="CO₂ and Cost per Scenario")

            bar_width = self.comparison_bar_width(df["Scenario"].nunique())
            fig.update_traces(marker_line_color="black",
                              marker_line_width=0.5,
                              width=bar_width)
            fig.update_layout(
                width=self.comparison_chart_width(df["Scenario"].nunique()),
                plot_bgcolor='white',
                paper_bgcolor='white',
                xaxis=dict(
                    showline=True,
                    linewidth=1,
                    linecolor='black',
                    tickfont=dict(color='black'),
                    mirror='all',
                    ticks='outside'
                ),
                yaxis=dict(
                    showline=True,
                    linewidth=1,
                    linecolor='black',
                    tickfont=dict(color='black'),
                    mirror='all',
                    ticks='outside'
                ),
                font=dict(color='black'),
                legend=dict(
                    bgcolor='white',
                    bordercolor='white',
                    borderwidth=1,
                    x=1.10,
                    xanchor='left',
                    y=1,
                    yanchor='top'
                ),
                margin=dict(l=40, r=240, t=40, b=40),
            )
            st.plotly_chart(fig, use_container_width=False)

    def show_investment_comparison(self):
        st.subheader("🏗️ Investment Capacity Comparison")

        generator_rows = []
        storage_rows = []
        for scenario, data in self.scenario_data.items():
            if "invest" not in data:
                continue
            df = data["invest"]

            # Generator capacities
            for abbr, full_name in TECH_MAPPING.items():
                mask = df["Component"].str.contains(abbr)
                capacity = df.loc[mask, "Invest"].sum()
                if capacity > 0:
                    generator_rows.append({
                        "Scenario": scenario,
                        "Technology": TECH_MAPPING.get(abbr, abbr),
                        "Capacity (kW)": capacity
                    })

            # Storage capacity (one row per scenario)
            storage_mask = df["Component"] == "Storage_th"
            storage_capacity = df.loc[storage_mask, "Invest"].sum()
            if storage_capacity > 0:
                storage_rows.append({
                    "Scenario": scenario,
                    "Technology": "Thermal Storage",
                    "Capacity (kW)": storage_capacity
                })

        # Convert to DataFrames
        df_generators = pd.DataFrame(generator_rows)
        df_storage = pd.DataFrame(storage_rows)
        df_storage.rename(columns={"Capacity (kW)": "Capacity (kWh)"}, inplace=True)
        df_all = pd.concat([df_generators, df_storage], ignore_index=True)

        if df_all.empty or "Scenario" not in df_all.columns:
            st.info("No investment data found across scenarios.")
            return

        # Assign consistent colors to scenarios
        unique_scenarios = df_all["Scenario"].unique()
        scenario_colors = self.get_scenario_colors()

        def color_scenario_font(s):
            return [
                f'color: {scenario_colors[val]}; font-weight: bold' if col == 'Scenario' else ''
                for col, val in zip(s.index, s)
            ]

        # -------- Generator Table --------
        if not df_generators.empty:
            st.markdown("#### 🔌 Generator Investment (kW)")
            styled_gen_df = df_generators.style.format({
                "Capacity (kW)": "{:.0f}"
            }).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]}
            ])
            styled_gen_df = styled_gen_df.hide(axis="index")
            st.write(styled_gen_df)

        # -------- Storage Table --------
        if not df_storage.empty:
            st.markdown("#### 🔋 Thermal Storage Investment (kWh)")
            styled_sto_df = df_storage.style.format({
                "Capacity (kWh)": "{:.0f}"
            }).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]}
            ])
            styled_sto_df = styled_sto_df.hide(axis="index")
            st.write(styled_sto_df)

            fig = go.Figure()

            # Add generator technologies (primary y-axis)
            bar_width = self.comparison_bar_width(df_all["Scenario"].nunique())

            for tech in df_generators["Technology"].unique():
                tech_df = df_generators[df_generators["Technology"] == tech]
                fig.add_trace(go.Bar(
                    x=tech_df["Scenario"],
                    y=tech_df["Capacity (kW)"],
                    name=tech,  # Already full name in df
                    marker=dict(line=dict(color='black', width=1)),
                    width=bar_width,
                    offsetgroup="generators",
                    alignmentgroup="investments",
                    cliponaxis=False,
                    opacity=1,
                    yaxis="y1"
                ))

            # Add storage (secondary y-axis, single bar per scenario)
            fig.add_trace(go.Bar(
                x=df_storage["Scenario"],
                y=df_storage["Capacity (kWh)"],
                name="Thermal Storage",
                marker=dict(color="lightgray",
                            line=dict(color='black', width=1)),
                width=bar_width,
                offsetgroup="storage",
                alignmentgroup="investments",
                cliponaxis=False,
                opacity=1,
                yaxis="y2"
            ))

            desired_order = sorted(df_all["Scenario"].unique(), key=self.natural_key)

            fig.update_layout(
                width=self.comparison_chart_width(len(unique_scenarios)),
                barmode="stack",
                plot_bgcolor='white',
                paper_bgcolor='white',
                xaxis=dict(
                    type='category',
                    showline=True,
                    linecolor='black',
                    ticks='outside',
                    tickfont=dict(color='black'),
                    mirror='all',
                    # automargin=True
                ),
                yaxis=dict(
                    title="Generator Capacity (kW)",
                    showline=True,
                    linecolor='black',
                    ticks='outside',
                    tickfont=dict(color='black'),
                    mirror='all',
                ),
                yaxis2=dict(
                    title="Storage Capacity (kWh)",
                    overlaying='y',
                    side='right',
                    showline=True,
                    linecolor='black',
                    ticks='outside',
                    tickfont=dict(color='black')
                ),
                legend=dict(
                    bgcolor='white',
                    bordercolor='white',
                    x=1.3,
                    xanchor='left',
                    y=1,
                    yanchor='top'
                ),
                margin=dict(l=60, r=240, t=40, b=40)
            )

            fig.update_xaxes(categoryorder='array', categoryarray=desired_order)

            st.plotly_chart(fig, use_container_width=False)

            # st.markdown("### 🧾 Abbreviation Legend")
            # for abbr, full in TECH_MAPPING.items():
            #     st.markdown(f"- **{abbr}**: {full}")
            # st.markdown("- **Thermal Storage**: Heat storage capacity")
        else:
            st.info("No investment data found across scenarios.")


    def show_generation_comparison(self):
        st.subheader("🔥 Heat and ⚡ Electricity Generation Comparison")

        heat_rows = []
        elec_rows = []

        for scenario, data in self.scenario_data.items():
            if "sum_flows" not in data:
                continue
            df = data["sum_flows"].copy()
            df.columns = [str(c).strip() for c in df.columns]

            # Try to normalize common variants to expected names
            rename_map = {}
            for c in df.columns:
                cl = c.lower().strip()
                if cl == "from":
                    rename_map[c] = "From"
                elif cl == "to":
                    rename_map[c] = "To"
                elif cl in {"total flow", "total_flow", "total", "sum", "sum flow", "sum_flow"}:
                    rename_map[c] = "Total flow"
            if rename_map:
                df = df.rename(columns=rename_map)

            required = {"From", "To", "Total flow"}
            if not required.issubset(set(df.columns)):
                st.warning(f"Scenario '{scenario}' sum_flows missing required columns. Found: {list(df.columns)}")
                continue


            # Heat generation: from generators to thermal buses "b_th_"
            heat_mask = df["From"].apply(lambda x: any(gen in x for gen in TECH_MAPPING.keys())) & df["To"].str.contains("b_th_")
            heat_df = df.loc[heat_mask, ["From", "To", "Total flow"]]

            # Sum heat per generator abbreviation
            for abbr in TECH_MAPPING.keys():
                gen_mask = heat_df["From"].str.contains(abbr)
                total_heat = heat_df.loc[gen_mask, "Total flow"].sum()
                if total_heat > 0:
                    heat_rows.append({
                        "Scenario": scenario,
                        "Technology": TECH_MAPPING.get(abbr, abbr),
                        "Heat Generation (MWh)": total_heat / 1000  # convert kWh to MWh
                    })

            # Electricity generation: from generators to electrical bus "b_el"
            elec_mask = df["From"].apply(lambda x: any(gen in x for gen in TECH_MAPPING.keys())) & df["To"].str.contains("b_el")
            elec_df = df.loc[elec_mask, ["From", "To", "Total flow"]]

            for abbr in TECH_MAPPING.keys():
                gen_mask = elec_df["From"].str.contains(abbr)
                total_elec = elec_df.loc[gen_mask, "Total flow"].sum()
                if total_elec > 0:
                    elec_rows.append({
                        "Scenario": scenario,
                        "Technology": TECH_MAPPING.get(abbr, abbr),
                        "Electricity Generation (MWh)": total_elec / 1000  # convert kWh to MWh
                    })

        # Create DataFrames
        df_heat = pd.DataFrame(heat_rows, columns=["Scenario", "Technology", "Heat Generation (MWh)"])
        df_elec = pd.DataFrame(elec_rows, columns=["Scenario", "Technology", "Electricity Generation (MWh)"])

        # If nothing at all, stop early
        if df_heat.empty and df_elec.empty:
            st.info("No heat/electricity generation data found across scenarios (sum_flows missing or zero flows).")
            return

        # Assign colors (only for scenarios that actually appear)
        unique_scenarios = sorted(
            set(df_heat["Scenario"].dropna().unique()).union(set(df_elec["Scenario"].dropna().unique())),
            key=self.natural_key
        )
        scenario_colors = self.get_scenario_colors()

        # Assign colors (reuse colors from investment tab)
        unique_scenarios = list(set(df_heat["Scenario"].unique()) | set(df_elec["Scenario"].unique()))
        scenario_colors = self.get_scenario_colors()

        def color_scenario_font(s):
            return [
                f'color: {scenario_colors[val]}; font-weight: bold' if col == 'Scenario' else ''
                for col, val in zip(s.index, s)
            ]

        # Show heat generation table
        if not df_heat.empty:
            st.markdown("### 🔥 Heat Generation by Technology")
            styled_heat = df_heat.style.format({"Heat Generation (MWh)": "{:.2f}"}).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]}
            ]).hide(axis="index")
            st.write(styled_heat)

        else:
            st.info("No heat generation data found.")

        # Show electricity generation table
        if not df_elec.empty:
            st.markdown("### ⚡ Electricity Generation by Technology")
            styled_elec = df_elec.style.format({"Electricity Generation (MWh)": "{:.2f}"}).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]}
            ]).hide(axis="index")
            st.write(styled_elec)

        else:
            st.info("No electricity generation data found.")

        # Create bar charts for heat and electricity generation side by side
        fig = go.Figure()

        # Bar width calculated based on number of scenarios
        if len(unique_scenarios) > 0:
            bar_width = self.comparison_bar_width(len(unique_scenarios))
        else:
            bar_width = 0.3

        # Heat generation bars (stacked per tech)
        elec_scenarios = df_elec["Scenario"].unique() if not df_elec.empty else []
        show_elec_bars = len(elec_scenarios) >= 2

        # Heat generation bars (stacked per tech)
        if not df_heat.empty:
            for tech in df_heat["Technology"].dropna().unique():
                tech_df = df_heat[df_heat["Technology"] == tech]
                fig.add_trace(go.Bar(
                    x=tech_df["Scenario"],
                    y=tech_df["Heat Generation (MWh)"],
                    name=f"Heat: {tech}",
                    marker=dict(line=dict(color='black', width=1)),
                    width=bar_width,
                    cliponaxis=False,
                    opacity=1,
                    yaxis="y1"
                ))

        # Electricity generation bars (optional)
        if show_elec_bars and not df_elec.empty:
            for tech in df_elec["Technology"].dropna().unique():
                tech_df = df_elec[df_elec["Technology"] == tech]
                fig.add_trace(go.Bar(
                    x=tech_df["Scenario"],
                    y=tech_df["Electricity Generation (MWh)"],
                    name=f"Elec: {tech}",
                    marker=dict(line=dict(color='black', width=1)),
                    width=bar_width,
                    cliponaxis=False,
                    offset=bar_width + 0.05,
                    opacity=1,
                    yaxis="y2"
                ))

        fig.update_traces(marker_line_width=1)

        all_scenarios = list(set(df_heat["Scenario"].unique()) | set(df_elec["Scenario"].unique()))
        desired_order = sorted(all_scenarios, key=self.natural_key)

        fig.update_layout(
            width=self.comparison_chart_width(len(desired_order)),
            barmode="stack",
            plot_bgcolor='white',
            paper_bgcolor='white',
            xaxis=dict(
                type='category',
                showline=True,
                linecolor='black',
                ticks='outside',
                tickfont=dict(color='black'),
                mirror='all',
            ),
            yaxis=dict(
                title="Heat Generation (MWh)",
                showline=True,
                linecolor='black',
                ticks='outside',
                tickfont=dict(color='black'),
                mirror='all',
                side='left'
            ),
            yaxis2=dict(
                title="Electricity Generation (MWh)",
                overlaying='y',
                side='right',
                showline=True,
                linecolor='black',
                ticks='outside',
                tickfont=dict(color='black'),
            ),
            legend=dict(
                bgcolor='white',
                bordercolor='white',
                x=1.3,
                xanchor='left',
                y=1,
                yanchor='top'
            ),
            margin=dict(l=60, r=240, t=40, b=40)
        )

        fig.update_xaxes(categoryorder='array', categoryarray=desired_order)

        st.plotly_chart(fig, use_container_width=False)

        # # Legend for abbreviations
        # st.markdown("### 🧾 Abbreviation Legend")
        # for abbr, full in TECH_MAPPING.items():
        #     st.markdown(f"- **{abbr}**: {full}")

    def show_heat_dispatch(self):
        st.subheader("🛠️ Operational Control Overview")

        buses = ["b_th_HT", "b_th_LT"]
        bus_labels = {
            "b_th_HT": "High Temperature Heat",
            "b_th_LT": "Low Temperature Heat"
        }

        total_width = 1200  # Adjusted for typical Streamlit container width
        margin_left = 40
        margin_right = 300  # Reserve space for legend on right side
        height_two_subplots = 900  # Reduced height for better fit
        height_single_plot = height_two_subplots // 2

        for scenario, data in self.scenario_data.items():
            scenario_panel = st.expander(f"Scenario: {scenario}", expanded=False)

            for bus in buses:
                if bus not in data:
                    scenario_panel.warning(f"Missing sheet '{bus}' in scenario '{scenario}'")
                    continue

                df = data[bus].copy()

                try:
                    df.columns = [
                        ast.literal_eval(col) if isinstance(col, str) and col.startswith("((") else col
                        for col in df.columns
                    ]
                except Exception as e:
                    scenario_panel.warning(f"Failed to parse column names in '{bus}' of '{scenario}': {e}")
                    continue

                tech_flows = {}
                storage_flows = {}

                for col in df.columns:
                    if not (isinstance(col, tuple) and col[1] == "flow"):
                        continue

                    src, tgt = col[0]
                    if "shortage" in src or "excess" in src or "shortage" in tgt or "excess" in tgt:
                        continue

                    flow_series = df[col] / 1000  # Convert to MWh

                    if tgt == bus:
                        tech_name = src
                        sign = +1
                    elif src == bus:
                        tech_name = tgt
                        sign = -1
                    else:
                        continue

                    if "z_HT_LT" in tech_name:
                        tech_label = "Transfer HT→LT"
                    else:
                        tech_label = next(
                            (full for abbr, full in TECH_MAPPING.items() if abbr in tech_name),
                            tech_name
                        )

                    signed_flow = sign * flow_series

                    if signed_flow.sum() < 0 and "storage" not in tech_label.lower():
                        continue

                    if "storage" in tech_label.lower():
                        if tech_label in storage_flows:
                            storage_flows[tech_label] += signed_flow
                        else:
                            storage_flows[tech_label] = signed_flow
                    else:
                        if tech_label in tech_flows:
                            tech_flows[tech_label] += signed_flow
                        else:
                            tech_flows[tech_label] = signed_flow

                if not tech_flows and not storage_flows:
                    scenario_panel.info(f"No flow data found for **{bus_labels.get(bus, bus)}** in **{scenario}**.")
                    continue

                if tech_flows and storage_flows:
                    fig = sp.make_subplots(
                        rows=2, cols=1,
                        shared_xaxes=True,
                        vertical_spacing=0.1,
                        # subplot_titles=(
                        #     f"Heat Flow from Generators at {bus_labels.get(bus, bus)}",
                        #     f"Storage Heat Flow at {bus_labels.get(bus, bus)}"
                        # )
                    )

                    for tech, series in tech_flows.items():
                        fig.add_trace(go.Scatter(y=series, mode="lines", name=tech), row=1, col=1)

                    for tech, series in storage_flows.items():
                        fig.add_trace(go.Scatter(y=series, mode="lines", name=tech,
                                                 # line=dict(dash="dot")
                                                 line=dict(dash="solid")
                                                 ),
                                      row=2, col=1)

                    fig.update_layout(
                        height=height_two_subplots,
                        width=total_width,
                        autosize=False,
                        title_text=f"Heat Dispatch for {bus_labels.get(bus, bus)} in Scenario {scenario}<br>",
                        title_x=0,
                        title_yanchor='top',
                        title_pad=dict(b=30),
                        showlegend=True,
                        legend=dict(
                            x=1.02,  # Just outside the plot area on the right
                            y=1,
                            xanchor='left',
                            yanchor='top',
                            bgcolor='rgba(255,255,255,0.9)',
                            borderwidth=0,
                        ),
                        plot_bgcolor="white",
                        margin=dict(l=margin_left, r=margin_right, t=120, b=40),  # increased top margin
                    )

                    # Update x- and y-axes for both subplots
                    for r in [1, 2]:
                        fig.update_xaxes(
                            title_text="Hour" if r == 2 else "",  # only bottom plot has label
                            showline=True,
                            linewidth=1,
                            linecolor='black',
                            mirror='all',
                            ticks='outside',
                            ticklen=5,
                            tickcolor='black',
                            row=r, col=1
                        )

                    fig.update_yaxes(
                        title_text="Flow (MWh)",
                        showline=True,
                        linewidth=1,
                        linecolor='black',
                        mirror='all',
                        ticks='outside',
                        ticklen=5,
                        tickcolor='black',
                    )

                else:
                    fig = go.Figure()

                    for tech, series in {**tech_flows, **storage_flows}.items():
                        # Plot all flows on one graph
                        # dash_style = "dot" if "storage" in tech.lower() else "solid"
                        dash_style = "solid"
                        fig.add_trace(go.Scatter(y=series, mode="lines", name=tech, line=dict(dash=dash_style)))

                    fig.update_layout(
                        height=height_single_plot,
                        width=total_width,
                        autosize=False,
                        title_text=f"Heat Flow at {bus_labels.get(bus, bus)} in Scenario {scenario}<br>",
                        title_x=0,
                        title_yanchor='top',
                        title_pad=dict(b=30),
                        showlegend=True,
                        legend=dict(
                            orientation="v",
                            y=1,
                            yanchor="top",
                            x=1.02,
                            xanchor="left",
                            bgcolor="rgba(255,255,255,0.9)",
                        ),
                        plot_bgcolor="white",
                        margin=dict(l=margin_left, r=margin_right, t=120, b=40),  # increased top margin
                    )

                    fig.update_xaxes(
                        title_text="Hour",
                        showline=True,
                        linewidth=1,
                        linecolor='black',
                        mirror='all',
                        ticks='outside',
                        ticklen=5,
                        tickcolor='black',
                    )

                    fig.update_yaxes(
                        title_text="Flow (MWh)",
                        showline=True,
                        linewidth=1,
                        linecolor='black',
                        mirror='all',
                        ticks='outside',
                        ticklen=5,
                        tickcolor='black',
                    )

                scenario_panel.plotly_chart(fig, use_container_width=True)

def main():
    st.set_page_config(page_title="Simulation results", layout="centered")
    st.title("📊 Simulation results")

    project = get_project_from_cli()
    if not project:
        st.error("No project specified. Launch this app with: -- --project <name>")
        st.stop()

    viewer = MultiScenarioViewer(project)

    tab1, tab2, tab3, tab4 = st.tabs([
        "📋 Summary (CO₂ & Cost)", "📈 Investment Capacity",
        "🔥 Generation Comparison", "🛠️ Operational Control Overview"
    ])

    with tab1: viewer.show_summary_comparison()
    with tab2: viewer.show_investment_comparison()
    with tab3: viewer.show_generation_comparison()
    with tab4: viewer.show_heat_dispatch()

    if st.button("❌ Close Results App"):
        # oemof-hri.py watches one project-level flag. Writing flags inside
        # scenario folders left the parent process waiting forever.
        flag = os.path.join(get_project_results_dir(viewer.project_name), "close_requested.flag")
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as f:
            f.write("close")
        st.success("Closing the results app and ending the district-planning program...")
        st.stop()

if __name__ == "__main__":
    main()

import os
os.environ.setdefault("MPLBACKEND", "Agg")
import streamlit as st
import pandas as pd
import sys
import matplotlib.pyplot as plt
import plotly.subplots as sp
import plotly.express as px
import plotly.graph_objects as go
import seaborn as sns
import ast
import re
from pathlib import Path
from logic.project_paths import (
    get_project_results_dir,
    get_results_root,
    get_scenario_dir_for_project,
)
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
    "GSHP_B": "Ground source heat pump (borehole)",
    "WWSHP": "Wastewater source heat pump",
}

HEAT_PUMP_TECHNOLOGIES = ("ASHP", "GSHP_C", "GSHP_B", "WWSHP")
LOCAL_COMPONENT_PATTERN = re.compile(r"(?:^|_)LC(?:\d+|_)", re.IGNORECASE)
DECENTRAL_COMPONENT_PATTERN = re.compile(r"(?:^|_)DC\d+(?:$|_)", re.IGNORECASE)
CENTRAL_PLANT_SUFFIX = " - central"
LOCAL_PLANT_SUFFIX = " - local"
DECENTRAL_PLANT_SUFFIX = " - decentral"

PV_PLANT_PREFIX = "PV_plant_"
PV_AREA_PATTERN = r"^PV_(?!plant_)"
PV_TECHNOLOGY_NAME = "Photovoltaics"
PV_GENERATION_BUSES = ("b_el_PV", "b_el")
PV_FEED_IN_PREFIX = "z_PV_Einspeisung"

ELECTRICITY_BALANCE_SHORT_NAMES = {
    "Combined heat and power (natural gas)": "CHP (natural gas)",
    "Combined heat and power (Biogas)": "CHP (biogas)",
    "Power to Heat": "P2H",
    "Air source heat pump": "Air source HP",
    "Ground source heat pump (collector)": "Ground source HP (collector)",
    "Ground source heat pump (borehole)": "Ground source HP (borehole)",
    "Wastewater source heat pump": "Wastewater source HP",
}

TECHNOLOGY_COLORS = {
    "Natural gas boiler": "#8C564B",
    "Biogas boiler": "#2CA02C",
    "Biomass boiler": "#7F7F0E",
    "Power to Heat": "#D62728",
    "Combined heat and power (natural gas)": "#9467BD",
    "Combined heat and power (Biogas)": "#17BECF",
    "Air source heat pump": "#1F77B4",
    "Ground source heat pump (collector)": "#FF7F0E",
    "Ground source heat pump (borehole)": "#E377C2",
    "Wastewater source heat pump": "#17A2A4",
    PV_TECHNOLOGY_NAME: "#F2B701",
    "Thermal Storage": "#7F7F7F",
    "Transfer HT→LT": "#4C78A8",
    "Grid import": "#6B7280",
    "PV grid feed-in": "#4C9BE8",
    "Electricity excess": "#BDBDBD",
}
FALLBACK_TECHNOLOGY_COLORS = (
    "#59A14F", "#EDC948", "#B07AA1", "#76B7B2", "#FF9DA7", "#9C755F"
)


def is_local_component(component: str) -> bool:
    return bool(LOCAL_COMPONENT_PATTERN.search(str(component)))


def component_scope(component: str) -> str:
    text = str(component)
    if LOCAL_COMPONENT_PATTERN.search(text):
        return "local"
    if DECENTRAL_COMPONENT_PATTERN.search(text):
        return "decentralized"
    return "central"


def scoped_technology_name(technology: str, component: str) -> str:
    """Add the modeled plant scope to a technology name."""
    suffix = {
        "central": CENTRAL_PLANT_SUFFIX,
        "local": LOCAL_PLANT_SUFFIX,
        "decentralized": DECENTRAL_PLANT_SUFFIX,
    }[component_scope(component)]
    return f"{technology}{suffix}"


def _lighter_color(color: str, fraction: float = 0.32) -> str:
    """Use a lighter shade for local plants while retaining technology identity."""
    value = str(color).lstrip("#")
    if len(value) != 6:
        return color
    channels = [int(value[index:index + 2], 16) for index in (0, 2, 4)]
    lightened = [round(channel + (255 - channel) * fraction) for channel in channels]
    return "#" + "".join(f"{channel:02X}" for channel in lightened)


def result_technology_color(label: str) -> str:
    """Return one stable color for a technology in every results graphic."""
    normalized = str(label)
    for prefix in ("Generation: ", "Consumption: "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    is_local = normalized.endswith(LOCAL_PLANT_SUFFIX)
    is_decentralized = normalized.endswith(DECENTRAL_PLANT_SUFFIX)
    for suffix in (
        CENTRAL_PLANT_SUFFIX, LOCAL_PLANT_SUFFIX, DECENTRAL_PLANT_SUFFIX
    ):
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]
            break
    if normalized in {"PV", "PV generation"} or normalized.startswith(PV_PLANT_PREFIX):
        normalized = PV_TECHNOLOGY_NAME
    for full_name, short_name in ELECTRICITY_BALANCE_SHORT_NAMES.items():
        if normalized == short_name:
            normalized = full_name
            break
    if "storage" in normalized.lower():
        normalized = "Thermal Storage"
    if normalized in TECHNOLOGY_COLORS:
        color = TECHNOLOGY_COLORS[normalized]
        return _lighter_color(color, 0.5 if is_decentralized else 0.32) \
            if (is_local or is_decentralized) else color
    for abbr, full_name in TECH_MAPPING.items():
        if abbr in normalized:
            color = TECHNOLOGY_COLORS[full_name]
            return _lighter_color(color, 0.5 if is_decentralized else 0.32) \
                if (is_local or is_decentralized) else color
    index = sum(ord(character) for character in normalized) % len(
        FALLBACK_TECHNOLOGY_COLORS
    )
    color = FALLBACK_TECHNOLOGY_COLORS[index]
    return _lighter_color(color, 0.5 if is_decentralized else 0.32) \
        if (is_local or is_decentralized) else color


def short_electricity_balance_name(label: str) -> str:
    """Use compact technology names in the wide electricity balance table."""
    value = str(label)
    for full_name, short_name in ELECTRICITY_BALANCE_SHORT_NAMES.items():
        if value.startswith(full_name):
            return short_name + value[len(full_name):]
    return value


def pv_generation_flow_mask(flow_totals: pd.DataFrame) -> pd.Series:
    """Identify PV plant output for shared and legacy electrical buses."""
    return (
        flow_totals["From"].astype(str).str.startswith(PV_PLANT_PREFIX)
        & flow_totals["To"].astype(str).isin(PV_GENERATION_BUSES)
    )


def extract_pv_hourly_generation(scenario_data: dict) -> dict[str, pd.Series]:
    """Read hourly PV output from the shared bus with legacy fallback."""
    pv_flows = {}
    for pv_bus in PV_GENERATION_BUSES:
        if pv_bus not in scenario_data:
            continue
        pv_df = scenario_data[pv_bus].copy()
        parsed_columns = []
        for column in pv_df.columns:
            if isinstance(column, str) and column.startswith("(("):
                try:
                    parsed_columns.append(ast.literal_eval(column))
                except (SyntaxError, ValueError):
                    parsed_columns.append(column)
            else:
                parsed_columns.append(column)
        pv_df.columns = parsed_columns

        for column in pv_df.columns:
            if not (
                isinstance(column, tuple)
                and len(column) == 2
                and column[1] == "flow"
                and isinstance(column[0], tuple)
                and len(column[0]) == 2
            ):
                continue
            source, target = column[0]
            if (
                target not in PV_GENERATION_BUSES
                or not str(source).startswith(PV_PLANT_PREFIX)
            ):
                continue
            series = pd.to_numeric(
                pv_df[column], errors="coerce"
            ).fillna(0) / 1000
            pv_flows[PV_TECHNOLOGY_NAME] = pv_flows.get(
                PV_TECHNOLOGY_NAME,
                pd.Series(0.0, index=series.index),
            ).add(series, fill_value=0)
    return pv_flows


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
        """Size charts from the number of scenario groups."""
        count = max(int(scenario_count), 1)
        return min(2200, 430 + 145 * count)

    @staticmethod
    def comparison_bar_width(scenario_count: int, groups_per_scenario: int = 2) -> float:
        """Keep each grouped bar narrow while scenario sets grow."""
        count = max(int(scenario_count), 1)
        groups = max(int(groups_per_scenario), 1)
        total_group_span = min(0.72, 0.44 + 0.04 * min(count, 7))
        return total_group_span / groups

    def load_project_scenarios(self, project_name: str):
        scenario_data = {}

        project_root = Path(RESULTS_DIR) / project_name
        if not project_root.exists():
            st.warning(f"No results folder found for project '{project_name}': {project_root}")
            return scenario_data

        scenario_dirs = sorted(
            project_root.iterdir(), key=lambda path: self.natural_key(path.name)
        )
        for scenario_dir in scenario_dirs:
            if not scenario_dir.is_dir():
                continue

            xlsx_path = scenario_dir / "results.xlsx"
            if not xlsx_path.is_file():
                continue

            # keep key simple now (scenario only) OR still "project/scenario"
            scenario_key = scenario_dir.name  # ✅ only scenarios of this project

            try:
                with pd.ExcelFile(xlsx_path) as xls:
                    data = {sheet: xls.parse(sheet) for sheet in xls.sheet_names}
                scenario_data[scenario_key] = data
            except Exception as e:
                st.warning(f"Failed to load {scenario_key}: {e}")

        return scenario_data

    def get_scenario_colors(self):
        scenarios = sorted(self.scenario_data.keys(), key=self.natural_key)
        if not scenarios:
            return {}
        colors = sns.color_palette("Set2", n_colors=len(scenarios)).as_hex()
        return dict(zip(scenarios, colors))

    def get_pv_scenario_metrics(self, scenario: str) -> dict:
        """Load the PV area ceiling and peak-power density used by a scenario."""
        scenario_file = (
            Path(get_scenario_dir_for_project(self.project_name)) / f"{scenario}.xlsx"
        )
        metrics = {
            "maximum_area_m2": None,
            "peak_kw_per_m2": None,
        }
        if not scenario_file.is_file():
            return metrics

        try:
            meta = pd.read_excel(scenario_file, sheet_name="meta")
            values = dict(zip(meta.iloc[:, 0].astype(str), meta.iloc[:, 1]))
            maximum_area = pd.to_numeric(
                values.get("Maximum PV Roof Area (m2)"),
                errors="coerce",
            )
            density = pd.to_numeric(
                values.get("PV Peak Power Density (kWp/m2)"), errors="coerce"
            )
            if pd.notna(maximum_area):
                metrics["maximum_area_m2"] = float(maximum_area)
            if pd.notna(density) and float(density) > 0:
                metrics["peak_kw_per_m2"] = float(density)
        except Exception:
            pass

        # Older scenarios do not yet have the effective collector-area field.
        # Their active renewable row still provides the optimization ceiling.
        try:
            renewables = pd.read_excel(scenario_file, sheet_name="renewables")
            labels = renewables["label"].fillna("").astype(str)
            active = pd.to_numeric(
                renewables["active"], errors="coerce"
            ).fillna(0).eq(1)
            maximum = pd.to_numeric(
                renewables.loc[active & labels.str.contains(PV_AREA_PATTERN), "maximum"],
                errors="coerce",
            ).dropna()
            if metrics["maximum_area_m2"] is None and not maximum.empty:
                metrics["maximum_area_m2"] = float(maximum.sum())
        except Exception:
            pass

        if metrics["peak_kw_per_m2"] is None:
            try:
                time_series = pd.read_excel(
                    scenario_file, sheet_name="time_series", usecols=["PV.fix"]
                )
                peak_radiation = pd.to_numeric(
                    time_series["PV.fix"], errors="coerce"
                ).clip(lower=0).max()
                if pd.notna(peak_radiation) and float(peak_radiation) > 0:
                    metrics["peak_kw_per_m2"] = float(peak_radiation) * 0.20
            except Exception:
                pass
        return metrics

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

            bar_width = self.comparison_bar_width(
                df["Scenario"].nunique(), groups_per_scenario=2
            )
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
        st.subheader("🔵 Investment Capacity Comparison")

        generator_rows = []
        storage_rows = []
        pv_area_rows = []
        for scenario, data in self.scenario_data.items():
            if "invest" not in data:
                continue
            df = data["invest"].copy()
            if not {"Component", "Invest"}.issubset(df.columns):
                continue
            component = df["Component"].fillna("").astype(str)
            investment = pd.to_numeric(df["Invest"], errors="coerce").fillna(0)

            # Generator capacities
            for abbr, full_name in TECH_MAPPING.items():
                technology_mask = component.str.contains(abbr, regex=False)
                for scope in ("central", "local", "decentralized"):
                    scope_mask = component.map(component_scope).eq(scope)
                    scoped_components = technology_mask & scope_mask
                    capacity = investment.loc[scoped_components].sum()
                    if capacity > 0:
                        representative = component.loc[scoped_components].iloc[0]
                        generator_rows.append({
                            "Scenario": scenario,
                            "Technology": scoped_technology_name(
                                full_name, representative
                            ),
                            "Capacity (kW)": capacity,
                        })

            pv_area = investment.loc[
                component.str.contains(PV_AREA_PATTERN, regex=True)
            ].sum()
            if pv_area > 0:
                pv_metrics = self.get_pv_scenario_metrics(scenario)
                peak_density = pv_metrics.get("peak_kw_per_m2")
                installed_kwp = (
                    float(pv_area) * float(peak_density)
                    if peak_density is not None else None
                )
                pv_area_rows.append({
                    "Scenario": scenario,
                    "Installed PV capacity (kWp)": installed_kwp,
                    "Maximum PV collector area (m²)": pv_metrics.get("maximum_area_m2"),
                    "Installed PV collector area (m²)": float(pv_area),
                })

            # Central and local storage capacities are reported separately.
            storage_mask = component.str.contains("Storage_th", regex=False)
            for scope in ("central", "local", "decentralized"):
                scoped_storage = (
                    storage_mask & component.map(component_scope).eq(scope)
                )
                storage_capacity = investment.loc[scoped_storage].sum()
                if storage_capacity > 0:
                    display_scope = (
                        "decentral" if scope == "decentralized" else scope
                    )
                    storage_rows.append({
                        "Scenario": scenario,
                        "Technology": f"Thermal Storage - {display_scope}",
                        "Capacity (kWh)": storage_capacity,
                    })

        df_generators = pd.DataFrame(generator_rows)
        df_storage = pd.DataFrame(storage_rows)
        df_pv_area = pd.DataFrame(pv_area_rows)
        df_all = pd.concat([df_generators, df_storage], ignore_index=True)

        if df_all.empty and df_pv_area.empty:
            st.info("No investment data found across scenarios.")
            return

        # Assign consistent colors to scenarios
        scenario_frames = [
            frame[["Scenario"]]
            for frame in (df_all, df_pv_area)
            if not frame.empty and "Scenario" in frame.columns
        ]
        unique_scenarios = pd.concat(
            scenario_frames, ignore_index=True
        )["Scenario"].unique()
        scenario_colors = self.get_scenario_colors()

        def color_scenario_font(s):
            return [
                f'color: {scenario_colors[val]}; font-weight: bold' if col == 'Scenario' else ''
                for col, val in zip(s.index, s)
            ]

        if not df_generators.empty:
            st.markdown("#### 🟦 Generator Investment (kW)")
            styled_gen_df = df_generators.style.format({
                "Capacity (kW)": "{:.1f}"
            }).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]}
            ])
            styled_gen_df = styled_gen_df.hide(axis="index")
            st.write(styled_gen_df)

        if not df_pv_area.empty:
            st.markdown("#### 🟨 PV Investment Details")
            styled_pv_df = df_pv_area.style.format({
                "Installed PV capacity (kWp)": "{:.1f}",
                "Maximum PV collector area (m²)": "{:.1f}",
                "Installed PV collector area (m²)": "{:.1f}",
            }, na_rep="Not available").apply(
                color_scenario_font, axis=1
            ).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]},
            ]).hide(axis="index")
            st.write(styled_pv_df)

            def format_pv_value(value, unit):
                numeric = pd.to_numeric(value, errors="coerce")
                return f"{float(numeric):,.1f} {unit}" if pd.notna(numeric) else "not available"

            area_text = "; ".join(
                f"{row['Scenario']}: "
                f"{format_pv_value(row['Installed PV capacity (kWp)'], 'kWp')}, "
                f"{format_pv_value(row['Installed PV collector area (m²)'], 'm²')} installed "
                f"of {format_pv_value(row['Maximum PV collector area (m²)'], 'm²')} maximum"
                for _, row in sorted(
                    df_pv_area.iterrows(),
                    key=lambda item: self.natural_key(item[1]["Scenario"]),
                )
            )
            st.caption(f"PV investment — {area_text}.")

        if not df_storage.empty:
            st.markdown("#### 🟩 Thermal Storage Investment (kWh)")
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

        if df_all.empty:
            return

        fig = go.Figure()
        bar_width = self.comparison_bar_width(
            df_all["Scenario"].nunique(),
            groups_per_scenario=2 if not df_storage.empty else 1,
        )
        if not df_generators.empty:
            for tech in df_generators["Technology"].unique():
                tech_df = df_generators[df_generators["Technology"] == tech]
                fig.add_trace(go.Bar(
                    x=tech_df["Scenario"],
                    y=tech_df["Capacity (kW)"],
                    name=tech,
                    marker=dict(
                        color=result_technology_color(tech),
                        line=dict(color='black', width=1),
                    ),
                    width=bar_width,
                    offsetgroup="generators",
                    alignmentgroup="investments",
                    cliponaxis=False,
                    opacity=1,
                    yaxis="y1"
                ))

        if not df_storage.empty:
            for technology in df_storage["Technology"].unique():
                technology_df = df_storage.loc[
                    df_storage["Technology"].eq(technology)
                ]
                fig.add_trace(go.Bar(
                    x=technology_df["Scenario"],
                    y=technology_df["Capacity (kWh)"],
                    name=technology,
                    marker=dict(
                        color=result_technology_color(technology),
                        line=dict(color='black', width=1),
                    ),
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
                type='category', showline=True, linecolor='black',
                ticks='outside', tickfont=dict(color='black'), mirror='all',
            ),
            yaxis=dict(
                title="Generator Capacity (kW)", showline=True,
                linecolor='black', ticks='outside',
                tickfont=dict(color='black'), mirror='all',
            ),
            yaxis2=dict(
                title="Storage Capacity (kWh)", overlaying='y', side='right',
                showline=True, linecolor='black', ticks='outside',
                tickfont=dict(color='black'),
            ),
            legend=dict(
                bgcolor='white', bordercolor='white', x=1.3,
                xanchor='left', y=1, yanchor='top',
            ),
            margin=dict(l=60, r=240, t=40, b=40),
        )
        fig.update_xaxes(categoryorder='array', categoryarray=desired_order)
        st.plotly_chart(fig, use_container_width=False)


    def show_generation_comparison(self):
        st.subheader("🔥 Heat and ⚡ Electricity Generation Comparison")

        heat_rows = []
        elec_rows = []
        electricity_balance_rows = []
        hp_cop_rows = []

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

            df["From"] = df["From"].fillna("").astype(str).str.strip()
            df["To"] = df["To"].fillna("").astype(str).str.strip()
            df["Total flow"] = pd.to_numeric(
                df["Total flow"], errors="coerce"
            ).fillna(0)

            invested_technologies = set()
            if "invest" in data:
                invest_df = data["invest"].copy()
                if {"Component", "Invest"}.issubset(invest_df.columns):
                    invest_components = invest_df["Component"].fillna("").astype(str)
                    invest_values = pd.to_numeric(
                        invest_df["Invest"], errors="coerce"
                    ).fillna(0)
                    invested_technologies = {
                        abbr
                        for abbr in TECH_MAPPING
                        if invest_values.loc[
                            invest_components.str.contains(abbr, regex=False)
                        ].sum() > 1e-6
                    }

            # Heat generation: from generators to thermal buses "b_th_"
            heat_mask = (
                df["From"].apply(lambda x: any(gen in x for gen in TECH_MAPPING))
                & df["To"].str.contains("b_th_", regex=False)
            )
            heat_df = df.loc[heat_mask, ["From", "To", "Total flow"]]

            # Annual system COP by HP technology: useful heat output divided
            # by the electricity delivered from the common electrical bus.
            for abbr in HEAT_PUMP_TECHNOLOGIES:
                for scope in ("central", "local", "decentralized"):
                    heat_component = heat_df["From"]
                    electricity_component = df["To"]
                    hp_heat_kwh = heat_df.loc[
                        heat_component.str.contains(abbr, regex=False)
                        & heat_component.map(component_scope).eq(scope),
                        "Total flow",
                    ].sum()
                    hp_electricity_kwh = df.loc[
                        df["From"].eq("b_el")
                        & electricity_component.str.contains(abbr, regex=False)
                        & electricity_component.map(component_scope).eq(scope),
                        "Total flow",
                    ].sum()
                    if hp_heat_kwh > 1e-6 and hp_electricity_kwh > 1e-6:
                        representative = {
                            "central": abbr,
                            "local": f"{abbr}_LC01",
                            "decentralized": f"{abbr}_DC01",
                        }[scope]
                        hp_cop_rows.append({
                            "Scenario": scenario,
                            "Heat pump": scoped_technology_name(
                                TECH_MAPPING[abbr], representative
                            ),
                            "Heat generation (MWh)": hp_heat_kwh / 1000,
                            "Electricity use (MWh)": hp_electricity_kwh / 1000,
                            "Average annual COP": hp_heat_kwh / hp_electricity_kwh,
                        })

            # Sum heat per generator abbreviation
            for abbr in TECH_MAPPING.keys():
                if abbr not in invested_technologies:
                    continue
                for scope in ("central", "local", "decentralized"):
                    heat_component = heat_df["From"]
                    gen_mask = (
                        heat_component.str.contains(abbr, regex=False)
                        & heat_component.map(component_scope).eq(scope)
                    )
                    total_heat = heat_df.loc[gen_mask, "Total flow"].sum()
                    if total_heat > 1e-6:
                        representative = heat_component.loc[gen_mask].iloc[0]
                        heat_rows.append({
                            "Scenario": scenario,
                            "Technology": scoped_technology_name(
                                TECH_MAPPING.get(abbr, abbr), representative
                            ),
                            "Heat Generation (MWh)": total_heat / 1000
                        })

            # Electricity generation: from generators to electrical bus "b_el"
            elec_mask = (
                df["From"].apply(lambda x: any(gen in x for gen in TECH_MAPPING))
                & df["To"].str.startswith("b_el")
            )
            elec_df = df.loc[elec_mask, ["From", "To", "Total flow"]]

            for abbr in TECH_MAPPING.keys():
                for scope in ("central", "local", "decentralized"):
                    electricity_component = elec_df["From"]
                    gen_mask = (
                        electricity_component.str.contains(abbr, regex=False)
                        & electricity_component.map(component_scope).eq(scope)
                    )
                    total_elec = elec_df.loc[gen_mask, "Total flow"].sum()
                    if total_elec > 1e-6:
                        representative = electricity_component.loc[gen_mask].iloc[0]
                        elec_rows.append({
                            "Scenario": scenario,
                            "Technology": scoped_technology_name(
                                TECH_MAPPING.get(abbr, abbr), representative
                            ),
                            "Electricity Generation (MWh)": total_elec / 1000
                        })

            pv_generation_mask = pv_generation_flow_mask(df)
            pv_feed_in_mask = (
                df["From"].str.startswith(PV_FEED_IN_PREFIX)
                & df["To"].eq("b_el_Einspeisung")
            )
            pv_generation_mwh = (
                df.loc[pv_generation_mask, "Total flow"].sum() / 1000
            )
            pv_feed_in_mwh = (
                df.loc[pv_feed_in_mask, "Total flow"].sum() / 1000
            )
            grid_import_mwh = (
                df.loc[df["From"].eq("el") & df["To"].eq("b_el"), "Total flow"].sum()
                / 1000
            )
            if pv_generation_mwh > 1e-6:
                elec_rows.append({
                    "Scenario": scenario,
                    "Technology": PV_TECHNOLOGY_NAME,
                    "Electricity Generation (MWh)": pv_generation_mwh,
                })

            electricity_balance_rows.extend([
                {
                    "Scenario": scenario,
                    "Balance side": "Supply",
                    "Flow": "PV",
                    "Electricity (MWh)": pv_generation_mwh,
                },
                {
                    "Scenario": scenario,
                    "Balance side": "Supply",
                    "Flow": "Grid import",
                    "Electricity (MWh)": grid_import_mwh,
                },
                {
                    "Scenario": scenario,
                    "Balance side": "Use",
                    "Flow": "PV grid feed-in",
                    "Electricity (MWh)": pv_feed_in_mwh,
                },
            ])

            for row in elec_rows:
                if row["Scenario"] != scenario or row["Technology"] == PV_TECHNOLOGY_NAME:
                    continue
                electricity_balance_rows.append({
                    "Scenario": scenario,
                    "Balance side": "Supply",
                    "Flow": short_electricity_balance_name(row["Technology"]),
                    "Electricity (MWh)": row["Electricity Generation (MWh)"],
                })

            electricity_consumption = df.loc[
                df["From"].eq("b_el")
                & ~df["To"].eq("b_el_excess")
                & df["Total flow"].gt(1e-6),
                ["To", "Total flow"],
            ]
            consumption_by_technology = {}
            for _, consumption in electricity_consumption.iterrows():
                component = str(consumption["To"])
                technology = next(
                    (
                        full_name
                        for abbr, full_name in TECH_MAPPING.items()
                        if abbr in component
                    ),
                    component,
                )
                if technology != component:
                    technology = scoped_technology_name(technology, component)
                consumption_by_technology[technology] = (
                    consumption_by_technology.get(technology, 0.0)
                    + float(consumption["Total flow"]) / 1000
                )
            for technology, consumption_mwh in consumption_by_technology.items():
                electricity_balance_rows.append({
                    "Scenario": scenario,
                    "Balance side": "Use",
                    "Flow": short_electricity_balance_name(technology),
                    "Electricity (MWh)": consumption_mwh,
                })

            electricity_excess_mwh = (
                df.loc[
                    df["From"].eq("b_el") & df["To"].eq("b_el_excess"),
                    "Total flow",
                ].sum()
                / 1000
            )
            if electricity_excess_mwh > 1e-6:
                electricity_balance_rows.append({
                    "Scenario": scenario,
                    "Balance side": "Use",
                    "Flow": "Electricity excess",
                    "Electricity (MWh)": electricity_excess_mwh,
                })

        # Create DataFrames
        df_heat = pd.DataFrame(heat_rows, columns=["Scenario", "Technology", "Heat Generation (MWh)"])
        df_elec = pd.DataFrame(elec_rows, columns=["Scenario", "Technology", "Electricity Generation (MWh)"])
        df_electricity_balance = pd.DataFrame(
            electricity_balance_rows,
            columns=["Scenario", "Balance side", "Flow", "Electricity (MWh)"],
        )
        df_hp_cop = pd.DataFrame(
            hp_cop_rows,
            columns=[
                "Scenario", "Heat pump", "Heat generation (MWh)",
                "Electricity use (MWh)", "Average annual COP",
            ],
        )

        # If nothing at all, stop early
        if df_heat.empty and df_elec.empty and df_electricity_balance.empty:
            st.info("No heat/electricity generation data found across scenarios (sum_flows missing or zero flows).")
            return

        unique_scenarios = sorted(
            set(df_heat["Scenario"].unique())
            | set(df_elec["Scenario"].unique())
            | set(df_electricity_balance["Scenario"].unique()),
            key=self.natural_key,
        )
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

        # Filled after the traces are prepared, keeping the heat graph directly
        # below its table and before all electricity results on the page.
        heat_chart_placeholder = st.empty()

        if not df_hp_cop.empty:
            st.markdown("### Average Annual Heat Pump COP")
            styled_hp_cop = df_hp_cop.style.format({
                "Heat generation (MWh)": "{:.2f}",
                "Electricity use (MWh)": "{:.2f}",
                "Average annual COP": "{:.2f}",
            }).apply(color_scenario_font, axis=1).set_properties(**{
                "white-space": "nowrap"
            }).set_table_styles([
                {"selector": "th", "props": [("color", "black"), ("font-weight", "bold")]},
                {"selector": "td", "props": [("padding", "5px")]},
            ]).hide(axis="index")
            st.write(styled_hp_cop)

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

        if not df_electricity_balance.empty:
            st.markdown("### ⚡ Electricity Supply and Use")
            balance_table = df_electricity_balance.pivot_table(
                index="Scenario",
                columns=["Balance side", "Flow"],
                values="Electricity (MWh)",
                aggfunc="sum",
                fill_value=0,
            )
            balance_table.columns = [
                f"{side} — {flow} (MWh)" for side, flow in balance_table.columns
            ]
            balance_table = balance_table.reset_index()
            preferred_columns = [
                "Scenario",
                "Supply — PV (MWh)",
                "Supply — Grid import (MWh)",
                "Use — PV grid feed-in (MWh)",
            ]
            balance_table = balance_table[
                [column for column in preferred_columns if column in balance_table.columns]
                + [
                    column for column in balance_table.columns
                    if column not in preferred_columns
                ]
            ]
            numeric_columns = [
                column for column in balance_table.columns if column != "Scenario"
            ]
            styled_pv = balance_table.style.format({
                column: "{:.2f}" for column in numeric_columns
            }).apply(color_scenario_font, axis=1).set_properties(**{
                'white-space': 'nowrap'
            }).set_table_styles([
                {'selector': 'th', 'props': [('color', 'black'), ('font-weight', 'bold')]},
                {'selector': 'td', 'props': [('padding', '5px')]},
            ]).hide(axis="index")
            st.write(styled_pv)

            pv_fig = go.Figure()
            pv_order = sorted(
                df_electricity_balance["Scenario"].unique(), key=self.natural_key
            )
            balance_sides = ["Supply", "Use"]
            x_scenarios = [
                scenario for scenario in pv_order for _ in balance_sides
            ]
            x_sides = balance_sides * len(pv_order)
            flow_order = [
                "PV", "Grid import", "PV grid feed-in"
            ] + sorted(
                flow for flow in df_electricity_balance["Flow"].unique()
                if flow not in {"PV", "Grid import", "PV grid feed-in"}
            )
            for flow in flow_order:
                flow_values = []
                for scenario, side in zip(x_scenarios, x_sides):
                    matching = df_electricity_balance.loc[
                        df_electricity_balance["Scenario"].eq(scenario)
                        & df_electricity_balance["Balance side"].eq(side)
                        & df_electricity_balance["Flow"].eq(flow),
                        "Electricity (MWh)",
                    ]
                    flow_values.append(float(matching.sum()))
                if max(flow_values, default=0) <= 1e-6:
                    continue
                pv_fig.add_trace(go.Bar(
                    x=[x_scenarios, x_sides],
                    y=flow_values,
                    name=flow,
                    marker=dict(
                        color=result_technology_color(flow),
                        line=dict(color="black", width=1),
                    ),
                ))
            pv_fig.update_layout(
                width=self.comparison_chart_width(len(pv_order)),
                barmode="stack",
                plot_bgcolor="white",
                paper_bgcolor="white",
                xaxis=dict(
                    type="multicategory", showline=True, linecolor="black",
                    ticks="outside", mirror="all",
                ),
                yaxis=dict(
                    title="Electricity (MWh)", showline=True,
                    linecolor="black", ticks="outside", mirror="all",
                ),
                legend=dict(
                    bgcolor="white", x=1.02, xanchor="left", y=1, yanchor="top",
                ),
                margin=dict(l=60, r=220, t=40, b=40),
            )
            st.plotly_chart(pv_fig, use_container_width=False)

        # Create the heat-generation graph in the placeholder above.
        fig = go.Figure()

        if len(unique_scenarios) > 0:
            bar_width = self.comparison_bar_width(
                len(unique_scenarios), groups_per_scenario=1,
            )
        else:
            bar_width = 0.3

        # Heat generation bars (stacked per tech)
        if not df_heat.empty:
            for tech in df_heat["Technology"].dropna().unique():
                tech_df = df_heat[df_heat["Technology"] == tech]
                fig.add_trace(go.Bar(
                    x=tech_df["Scenario"],
                    y=tech_df["Heat Generation (MWh)"],
                    name=tech,
                    marker=dict(
                        color=result_technology_color(tech),
                        line=dict(color='black', width=1),
                    ),
                    width=bar_width,
                    offsetgroup="heat",
                    alignmentgroup="generation",
                    cliponaxis=False,
                    opacity=1,
                    yaxis="y1"
                ))

        fig.update_traces(marker_line_width=1)

        all_scenarios = list(set(df_heat["Scenario"].unique()))
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

        if not df_heat.empty:
            heat_chart_placeholder.plotly_chart(fig, use_container_width=False)

        # # Legend for abbreviations
        # st.markdown("### 🧾 Abbreviation Legend")
        # for abbr, full in TECH_MAPPING.items():
        #     st.markdown(f"- **{abbr}**: {full}")

    def _show_heat_dispatch_legacy(self):
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
            scenario_panel.caption(
                "Hourly dispatch is available as CSV downloads; full hourly tables are not displayed."
            )

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
                        if tech_label != tech_name:
                            tech_label = scoped_technology_name(
                                tech_label, tech_name
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

                dispatch_download = pd.DataFrame({
                    "Hour": range(1, len(df) + 1),
                })
                time_col = None
                time_values = None
                for candidate_col in df.columns:
                    if isinstance(candidate_col, tuple):
                        continue
                    candidate_values = df[candidate_col]
                    if isinstance(candidate_values, pd.DataFrame):
                        candidate_values = candidate_values.iloc[:, 0]
                    if candidate_values.notna().any():
                        time_col = candidate_col
                        time_values = candidate_values
                        break
                if time_col is not None:
                    dispatch_download.insert(
                        0,
                        "Timestamp",
                        time_values.reset_index(drop=True),
                    )
                for tech, series in sorted(tech_flows.items()):
                    dispatch_download[f"Generator - {tech} (MWh)"] = series.reset_index(drop=True)
                for tech, series in sorted(storage_flows.items()):
                    dispatch_download[f"Storage - {tech} (MWh)"] = series.reset_index(drop=True)

                signal_columns = [
                    col for col in dispatch_download.columns
                    if col not in {"Timestamp", "Hour"}
                ]
                if signal_columns:
                    dispatch_download = dispatch_download.loc[
                        dispatch_download[signal_columns].notna().any(axis=1)
                    ].reset_index(drop=True)
                    dispatch_download["Hour"] = range(1, len(dispatch_download) + 1)

                safe_name = re.sub(
                    r"[^A-Za-z0-9._-]+",
                    "_",
                    f"{scenario}_{bus}_hourly_heat_dispatch",
                ).strip("_")
                scenario_panel.download_button(
                    f"Download hourly {bus_labels.get(bus, bus).lower()} dispatch (.csv)",
                    data=dispatch_download.to_csv(
                        index=False,
                        float_format="%.6f",
                    ).encode("utf-8-sig"),
                    file_name=f"{safe_name}.csv",
                    mime="text/csv",
                    key=f"download_dispatch_{self.project_name}_{scenario}_{bus}",
                    help=(
                        "Downloads one row per hour. Positive values supply heat; "
                        "negative storage values represent charging."
                    ),
                )

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
                        fig.add_trace(
                            go.Scatter(
                                y=series,
                                mode="lines",
                                name=tech,
                                line=dict(color=result_technology_color(tech)),
                            ),
                            row=1, col=1,
                        )

                    for tech, series in storage_flows.items():
                        fig.add_trace(go.Scatter(y=series, mode="lines", name=tech,
                                                 # line=dict(dash="dot")
                                                 line=dict(
                                                     dash="solid",
                                                     color=result_technology_color(tech),
                                                 )
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
                        fig.add_trace(go.Scatter(
                            y=series,
                            mode="lines",
                            name=tech,
                            line=dict(
                                dash=dash_style,
                                color=result_technology_color(tech),
                            ),
                        ))

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

            # New scenarios route every PV plant category through the shared
            # b_el_PV bus. Keep b_el as a fallback for older result files.
            pv_flows = extract_pv_hourly_generation(data)

            scenario_panel.markdown("#### ☀️ PV Electricity Generation")
            if not pv_flows:
                scenario_panel.info(f"No PV electricity generation flow was found in scenario '{scenario}'.")
                continue

            pv_fig = go.Figure()
            for plant, series in sorted(pv_flows.items()):
                pv_fig.add_trace(go.Scatter(
                    x=list(range(1, len(series) + 1)),
                    y=series,
                    mode="lines",
                    name=plant,
                    line=dict(color=result_technology_color(PV_TECHNOLOGY_NAME)),
                ))
            pv_fig.update_layout(
                height=height_single_plot,
                width=total_width,
                autosize=False,
                title_text=f"PV Electricity Generation in Scenario {scenario}",
                title_x=0,
                showlegend=True,
                legend=dict(
                    orientation="v", y=1, yanchor="top",
                    x=1.02, xanchor="left",
                    bgcolor="rgba(255,255,255,0.9)",
                ),
                plot_bgcolor="white",
                margin=dict(l=margin_left, r=margin_right, t=90, b=40),
            )
            pv_fig.update_xaxes(
                title_text="Hour", showline=True, linewidth=1,
                linecolor='black', mirror='all', ticks='outside',
            )
            pv_fig.update_yaxes(
                title_text="Electricity generation (MWh)", showline=True,
                linewidth=1, linecolor='black', mirror='all', ticks='outside',
            )
            scenario_panel.plotly_chart(pv_fig, use_container_width=True)

    def show_heat_dispatch(self):
        """Show central and local hourly generation using modeled target buses."""
        st.subheader("Operational Control Overview")

        total_width = 1200
        chart_height = 470

        def normalized_sum_flows(data):
            flows = data.get("sum_flows")
            if not isinstance(flows, pd.DataFrame):
                return pd.DataFrame(columns=["From", "To", "Total flow"])
            flows = flows.copy()
            rename = {}
            for column in flows.columns:
                normalized = str(column).strip().lower()
                if normalized == "from":
                    rename[column] = "From"
                elif normalized == "to":
                    rename[column] = "To"
                elif normalized in {
                    "total flow", "total_flow", "total", "sum", "sum flow", "sum_flow"
                }:
                    rename[column] = "Total flow"
            flows = flows.rename(columns=rename)
            if not {"From", "To", "Total flow"}.issubset(flows.columns):
                return pd.DataFrame(columns=["From", "To", "Total flow"])
            flows["From"] = flows["From"].fillna("").astype(str).str.strip()
            flows["To"] = flows["To"].fillna("").astype(str).str.strip()
            flows["Total flow"] = pd.to_numeric(
                flows["Total flow"], errors="coerce"
            ).fillna(0)
            return flows

        def scenario_network_level(scenario):
            scenario_file = (
                Path(get_scenario_dir_for_project(self.project_name))
                / f"{scenario}.xlsx"
            )
            try:
                meta = pd.read_excel(scenario_file, sheet_name="meta")
                values = dict(zip(meta.iloc[:, 0].astype(str), meta.iloc[:, 1]))
                level = str(values.get("Network Supply Temperature") or "HT").upper()
                return "LT" if level in {"LT", "NT"} else "HT"
            except Exception:
                return "HT"

        def parse_hourly_bus(data, bus):
            if bus not in data:
                return None
            frame = data[bus].copy()
            parsed_columns = []
            for column in frame.columns:
                if isinstance(column, str) and column.startswith("(("):
                    try:
                        parsed_columns.append(ast.literal_eval(column))
                    except (SyntaxError, ValueError):
                        parsed_columns.append(column)
                else:
                    parsed_columns.append(column)
            frame.columns = parsed_columns
            return frame

        def collect_generation(data, buses, expected_scope):
            generation = {}
            timestamps = None
            row_count = 0
            for bus in buses:
                frame = parse_hourly_bus(data, bus)
                if frame is None:
                    continue
                row_count = max(row_count, len(frame))
                if timestamps is None:
                    for column in frame.columns:
                        if isinstance(column, tuple):
                            continue
                        values = frame[column]
                        if isinstance(values, pd.DataFrame):
                            values = values.iloc[:, 0]
                        if values.notna().any():
                            timestamps = values.reset_index(drop=True)
                            break

                for column in frame.columns:
                    if not (
                        isinstance(column, tuple)
                        and len(column) == 2
                        and column[1] == "flow"
                        and isinstance(column[0], tuple)
                        and len(column[0]) == 2
                    ):
                        continue
                    source, target = map(str, column[0])
                    storage_component = None
                    storage_direction = None
                    storage_sign = 1.0
                    if target == bus and "storage_th" in source.lower():
                        storage_component = source
                        storage_direction = "discharging"
                    elif source == bus and "storage_th" in target.lower():
                        storage_component = target
                        storage_direction = "charging"
                        # Charging consumes heat from the plotted bus, so show
                        # it below zero in the hourly heat-dispatch chart.
                        storage_sign = -1.0

                    if storage_component is not None:
                        if component_scope(storage_component) != expected_scope:
                            continue
                        display_scope = (
                            "decentral"
                            if expected_scope == "decentralized"
                            else expected_scope
                        )
                        label = (
                            f"Thermal storage {storage_direction} - "
                            f"{display_scope}"
                        )
                        series = (
                            pd.to_numeric(frame[column], errors="coerce")
                            .fillna(0).reset_index(drop=True) / 1000
                        ) * storage_sign
                        if series.abs().max() <= 1e-9:
                            continue
                        if label in generation:
                            generation[label] = generation[label].add(
                                series, fill_value=0
                            )
                        else:
                            generation[label] = series
                        continue

                    if target != bus or component_scope(source) != expected_scope:
                        continue
                    technology = next(
                        (
                            full_name
                            for abbreviation, full_name in TECH_MAPPING.items()
                            if abbreviation in source
                        ),
                        None,
                    )
                    if technology is None:
                        # Transfers, shortages and storage discharge are not
                        # heat generation by a central or local plant.
                        continue
                    label = scoped_technology_name(technology, source)
                    series = pd.to_numeric(
                        frame[column], errors="coerce"
                    ).fillna(0).reset_index(drop=True) / 1000
                    if series.abs().max() <= 1e-9:
                        continue
                    if label in generation:
                        generation[label] = generation[label].add(
                            series, fill_value=0
                        )
                    else:
                        generation[label] = series
            return generation, timestamps, row_count

        def render_generation(
            panel, scenario, section_key, title, flows, timestamps, row_count,
            energy_label="Heat",
        ):
            panel.markdown(f"#### {title}")
            if not flows:
                panel.info(f"No {title.lower()} was found in scenario '{scenario}'.")
                return

            download = pd.DataFrame({"Hour": range(1, row_count + 1)})
            if timestamps is not None:
                download.insert(
                    0, "Timestamp", timestamps.reindex(range(row_count))
                )
            for technology, series in sorted(flows.items()):
                download[f"{technology} (MWh)"] = series.reindex(
                    range(row_count), fill_value=0
                )
            safe_name = re.sub(
                r"[^A-Za-z0-9._-]+", "_",
                f"{scenario}_{section_key}_hourly_{energy_label.lower()}",
            ).strip("_")
            panel.download_button(
                f"Download hourly {title.lower()} (.csv)",
                data=download.to_csv(index=False, float_format="%.6f").encode(
                    "utf-8-sig"
                ),
                file_name=f"{safe_name}.csv",
                mime="text/csv",
                key=(
                    f"download_generation_{self.project_name}_"
                    f"{scenario}_{section_key}"
                ),
            )

            storage_flows = {
                technology: series
                for technology, series in flows.items()
                if "storage" in technology.lower()
            }
            generation_flows = {
                technology: series
                for technology, series in flows.items()
                if technology not in storage_flows
            }

            def render_flow_chart(chart_flows, chart_title, y_axis_title):
                figure = go.Figure()
                for technology, series in sorted(chart_flows.items()):
                    is_storage_charging = (
                        "storage charging" in technology.lower()
                    )
                    figure.add_trace(go.Scatter(
                        x=list(range(1, len(series) + 1)),
                        y=series,
                        mode="lines",
                        name=technology,
                        line=dict(
                            color=result_technology_color(technology),
                            dash="dash" if is_storage_charging else "solid",
                        ),
                    ))
                figure.update_layout(
                    height=chart_height,
                    width=total_width,
                    autosize=False,
                    title_text=chart_title,
                    title_x=0,
                    showlegend=True,
                    legend=dict(
                        orientation="v", y=1, yanchor="top",
                        x=1.02, xanchor="left",
                        bgcolor="rgba(255,255,255,0.9)",
                    ),
                    plot_bgcolor="white",
                    margin=dict(l=55, r=300, t=90, b=45),
                )
                figure.update_xaxes(
                    title_text="Hour", showline=True, linewidth=1,
                    linecolor="black", mirror="all", ticks="outside",
                )
                figure.update_yaxes(
                    title_text=y_axis_title, showline=True, linewidth=1,
                    linecolor="black", mirror="all", ticks="outside",
                )
                panel.plotly_chart(figure, use_container_width=True)

            if generation_flows:
                render_flow_chart(
                    generation_flows,
                    f"{title} in Scenario {scenario}",
                    f"{energy_label} (MWh)",
                )
            elif not storage_flows:
                panel.info(
                    f"No {title.lower()} was found in scenario '{scenario}'."
                )

            if storage_flows:
                storage_title = {
                    "central": "Central thermal storage operation",
                    "local": "Local thermal storage operation",
                    "decentralized": "Decentral thermal storage operation",
                }.get(section_key, "Thermal storage operation")
                panel.markdown("##### Storage charging and discharging")
                render_flow_chart(
                    storage_flows,
                    f"{storage_title} in Scenario {scenario}",
                    "Storage heat flow (MWh)",
                )

        def hourly_flow(data, source, target):
            """Return one hourly edge series without double-counting bus views."""
            for bus in (target, source):
                frame = parse_hourly_bus(data, bus)
                if frame is None:
                    continue
                for column in frame.columns:
                    if not (
                        isinstance(column, tuple)
                        and len(column) == 2
                        and column[1] == "flow"
                        and isinstance(column[0], tuple)
                        and tuple(map(str, column[0])) == (source, target)
                    ):
                        continue
                    return (
                        pd.to_numeric(frame[column], errors="coerce")
                        .fillna(0).reset_index(drop=True) / 1000
                    )
            return None

        def collect_electricity(data, totals):
            generation = {}
            consumption = {}
            rows = 0
            for _, flow in totals.loc[totals["Total flow"].gt(1e-6)].iterrows():
                source = str(flow["From"])
                target = str(flow["To"])
                source_technology = next(
                    (name for abbreviation, name in TECH_MAPPING.items()
                     if abbreviation in source),
                    None,
                )
                target_technology = next(
                    (name for abbreviation, name in TECH_MAPPING.items()
                     if abbreviation in target),
                    None,
                )
                label = None
                destination = None
                if target.startswith("b_el"):
                    if source_technology:
                        label = scoped_technology_name(source_technology, source)
                    elif source.startswith(PV_PLANT_PREFIX):
                        label = PV_TECHNOLOGY_NAME
                    elif source in {"el", "electricity"}:
                        label = "Grid import"
                    destination = generation
                elif source == "b_el" and target_technology:
                    label = scoped_technology_name(target_technology, target)
                    destination = consumption
                if label is None or destination is None:
                    continue
                series = hourly_flow(data, source, target)
                if series is None or series.abs().max() <= 1e-9:
                    continue
                rows = max(rows, len(series))
                if label in destination:
                    destination[label] = destination[label].add(
                        series, fill_value=0
                    )
                else:
                    destination[label] = series
            return generation, consumption, rows

        for scenario, data in self.scenario_data.items():
            panel = st.expander(f"Scenario: {scenario}", expanded=False)
            panel.caption(
                "Hourly heat and electricity flows are classified from the "
                "connected buses in sum_flows and can be downloaded as CSV. "
                "Storage discharging is positive; storage charging is shown "
                "as a negative, dashed trace."
            )
            totals = normalized_sum_flows(data)
            destinations = set(
                totals.loc[totals["Total flow"].gt(1e-6), "To"].astype(str)
            )
            central_buses = [
                bus for bus in ("b_th_LT", "b_th_HT")
                if bus in destinations and bus in data
            ]
            local_buses = sorted(
                (
                    bus for bus in destinations
                    if re.match(r"^b_th_.*_LC\d+$", bus, flags=re.IGNORECASE)
                    and bus in data
                ),
                key=self.natural_key,
            )
            decentralized_buses = sorted(
                (
                    bus for bus in destinations
                    if re.match(r"^b_th_(?:HT|LT)_DC\d+$", bus, flags=re.IGNORECASE)
                    and bus in data
                ),
                key=self.natural_key,
            )

            central_flows, central_time, central_rows = collect_generation(
                data, central_buses, expected_scope="central"
            )
            local_flows, local_time, local_rows = collect_generation(
                data, local_buses, expected_scope="local"
            )
            decentralized_flows, decentralized_time, decentralized_rows = collect_generation(
                data, decentralized_buses, expected_scope="decentralized"
            )
            network_level = scenario_network_level(scenario)
            if decentralized_buses:
                render_generation(
                    panel,
                    scenario,
                    "decentralized",
                    "Heat generation of decentral plants",
                    decentralized_flows,
                    decentralized_time,
                    decentralized_rows,
                )
            else:
                render_generation(
                    panel,
                    scenario,
                    "central",
                    f"Heat generation at {network_level} central plant",
                    central_flows,
                    central_time,
                    central_rows,
                )
                if local_buses:
                    render_generation(
                        panel,
                        scenario,
                        "local",
                        "Heat generation of local plants",
                        local_flows,
                        local_time,
                        local_rows,
                    )

            electricity_generation, electricity_use, electricity_rows = (
                collect_electricity(data, totals)
            )
            render_generation(
                panel,
                scenario,
                "electricity_generation",
                "Electricity generation",
                electricity_generation,
                None,
                electricity_rows,
                energy_label="Electricity generation",
            )
            render_generation(
                panel,
                scenario,
                "electricity_use",
                "Electricity use by heat-generation technologies",
                electricity_use,
                None,
                electricity_rows,
                energy_label="Electricity use",
            )

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

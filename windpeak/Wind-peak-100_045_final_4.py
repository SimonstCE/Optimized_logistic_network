#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Converted from Wind-peak-100_045_final_4.ipynb
- All print() statements preserved exactly.
- All visualizations saved as PNG to ./visuals/ (descriptive filenames).
- Directory ./visuals created automatically.
- Gurobi optimization, data processing, and all logic kept intact.
"""

import os
from pathlib import Path
import pandas as pd
from itertools import zip_longest
import math
from geopy import distance
import geopandas as gpd
from shapely.geometry import Point, LineString, box
import osmnx as ox
import networkx as nx
from tqdm.auto import tqdm
import sys
import gurobipy as gp
from gurobipy import GRB
import random
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple, Dict
import fiona
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
from matplotlib.collections import LineCollection
import gc
import warnings
warnings.filterwarnings("ignore")

# ============================================================
# IMPORTS + INITIAL DATA LOADING
# ============================================================

print("Gurobi version check...")
print(sys.version)
print("gurobipy", ".".join(map(str, gp.gurobi.version())))
print("Gurobi Optimizer Version:", ".".join(map(str, gp.gurobi.version())))

# Einlesen Excel-Datei
import argparse

_parser = argparse.ArgumentParser(description="Run the GFRP-Waste network optimization for one input scenario.")
_parser.add_argument("input_excel", help="Path to the input Excel scenario file (e.g. Nur_Wind_Lichtenegger_294.xlsx)")
_args = _parser.parse_args()

dateipfad = _args.input_excel
OUTPUT_BASE = Path(__file__).parent / Path(dateipfad).stem
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
print(f"Eingabedatei: {dateipfad}")
print(f"Ausgabeverzeichnis: {OUTPUT_BASE}")

# ============================================================
# CREATE VISUALS DIRECTORY
# ============================================================
VISUALS_DIR = OUTPUT_BASE / "plots"
VISUALS_DIR.mkdir(parents=True, exist_ok=True)
print(f"✅ Visuals directory created/verified: {VISUALS_DIR.absolute()}")

xls = pd.read_excel(
    dateipfad,
    sheet_name=[
        "Kosten",
        "Faktoren",
        "supply",
        "recyling_location",
        "Integrierte Zementwerke",
        "Transportkosten"
    ]
)

def clean_cols(df):
    df.columns = df.columns.astype(str).str.strip()
    return df

kostendaten = clean_cols(xls["Kosten"]).dropna(subset=["Parameter"]).set_index("Parameter")
cost_params = kostendaten.drop(columns=["Einheit", "Kommentar", "Quelle", "Aspekt"], errors="ignore").to_dict(orient="index")
faktoren_df = clean_cols(xls["Faktoren"])

cost_matrix_persyn = clean_cols(xls["Transportkosten"])
cost_matrix_persyn = cost_matrix_persyn.set_index(cost_matrix_persyn.columns[0])
cost_matrix_persyn.index = cost_matrix_persyn.index.astype(str).str.strip()
cost_matrix_persyn.columns = cost_matrix_persyn.columns.astype(str).str.strip()

supply_wind = clean_cols(xls["supply"].copy())
supply_wind["Industrie"] = "Wind"
supply_df = supply_wind
supply_df["x"] = pd.to_numeric(supply_df["x"], errors='coerce')
supply_df["y"] = pd.to_numeric(supply_df["y"], errors='coerce')

recycling_df = clean_cols(xls["recyling_location"])

zement_raw = clean_cols(xls["Integrierte Zementwerke"])
zement_raw["NUTS_ID"] = zement_raw["NUTS_ID"].astype(str).str.strip()

zementwerke_agg = zement_raw.groupby("NUTS_ID")["Demand GFK in kg"].sum()
zementwerke_df = zementwerke_agg.to_frame()
zementwerke_df.index.name = "Z_ID"
zement_coords = zement_raw.groupby("NUTS_ID")[["x", "y"]].mean()
zementwerke_df = zementwerke_df.join(zement_coords)

print("✅ Daten importiert und Spalten bereinigt.")
print(f"Supply Spalten: {supply_df.columns.tolist()}")

# GEO-DATAFRAMES
supply_df_clean = supply_df.dropna(subset=["x", "y"])
supply_gdf = gpd.GeoDataFrame(
    supply_df_clean,
    geometry=gpd.points_from_xy(supply_df_clean["x"], supply_df_clean["y"]),
    crs="EPSG:4326"
)
recycling_gdf = gpd.GeoDataFrame(
    recycling_df,
    geometry=gpd.points_from_xy(recycling_df["x"], recycling_df["y"]),
    crs="EPSG:4326"
)
cement_gdf = gpd.GeoDataFrame(
    zementwerke_df,
    geometry=gpd.points_from_xy(zementwerke_df["x"], zementwerke_df["y"]),
    crs="EPSG:4326"
)
print("✅ GeoDataFrames erstellt.")

# =============================================================================
# PROJECTED GEODATAFRAMES (EPSG:3035) + ISO CODES
# =============================================================================
print("Start: Projektion → EPSG:3035")

# Load NUTS2 shapefile (must be available at this relative path)
nuts2_shp = "/home/kit/iw6717/NUTS/NUTS_RG_20M_2016_3035.shp"
print("1. Lade NUTS-2-Shapefile …")
nuts2 = gpd.read_file(nuts2_shp, engine="fiona")
print(f"   → NUTS-2 geladen: {len(nuts2)} Regionen")
nuts0 = nuts2.dissolve(by="CNTR_CODE")
nuts0["geometry"] = nuts0["geometry"].simplify(tolerance=100)
nuts0_sindex = nuts0.sindex

# ---------------------------------------------------------------------------
# LEGACY WORKAROUND (no longer needed after running fix_excel_coordinates.py)
# Some NUTS IDs in the Excel file (e.g. PT16/PT17/PT18, NL33) had wrong x/y
# values — Portuguese regions were stored at Romanian coordinates, a Dutch
# region at Slovak coordinates. The block below overwrote the Excel-based GDFs
# with authoritative centroids from the NUTS shapefile to paper over those errors.
# After fix_excel_coordinates.py has been run once on every input .xlsx file,
# the x/y columns in Excel are correct and the GDFs created above (from Excel)
# are already accurate — this block is redundant.
# ---------------------------------------------------------------------------
# nuts2_4326 = nuts2.to_crs("EPSG:4326")
# nuts_centroid_map = nuts2_4326.set_index("NUTS_ID").geometry.centroid.to_dict()
#
# def _nuts_geom(nuts_id):
#     return nuts_centroid_map.get(str(nuts_id))
#
# supply_gdf = gpd.GeoDataFrame(
#     supply_df_clean,
#     geometry=[_nuts_geom(n) for n in supply_df_clean["NUTS_ID"]],
#     crs="EPSG:4326"
# ).loc[lambda d: d.geometry.notna()].copy()
#
# recycling_gdf = gpd.GeoDataFrame(
#     recycling_df,
#     geometry=[_nuts_geom(n) for n in recycling_df["NUTS_ID"]],
#     crs="EPSG:4326"
# ).loc[lambda d: d.geometry.notna()].copy()
#
# cement_gdf = gpd.GeoDataFrame(
#     zementwerke_df,
#     geometry=[_nuts_geom(n) for n in zementwerke_df.index],
#     crs="EPSG:4326"
# ).loc[lambda d: d.geometry.notna()].copy()

iso_codes = nuts0.index.tolist()
print("5. Verfügbare ISO-Länderkürzel (NUTS-0-Index):")
print(iso_codes)

supply_gdf_proj    = supply_gdf.to_crs("EPSG:3035")
recycling_gdf_proj = recycling_gdf.to_crs("EPSG:3035")
cement_gdf_proj    = cement_gdf.to_crs("EPSG:3035")

# Assign ISO codes via spatial join
supply_gdf_proj["iso"]    = gpd.sjoin(supply_gdf_proj,    nuts0, how="left")["CNTR_CODE"]
recycling_gdf_proj["iso"] = gpd.sjoin(recycling_gdf_proj, nuts0, how="left")["CNTR_CODE"]
cement_gdf_proj["iso"]    = gpd.sjoin(cement_gdf_proj,    nuts0, how="left")["CNTR_CODE"]

# Manual ISO fixes for boundary cases
cement_gdf_proj.loc["FI1D", "iso"] = "FI"
cement_gdf_proj.loc["UKJ4", "iso"] = "UK"

for name, gdf in [("Supply", supply_gdf_proj), ("Recycling", recycling_gdf_proj), ("Cement", cement_gdf_proj)]:
    fehl = gdf["iso"].isna().sum()
    print(f"{name:9s}: {fehl} fehlende ISO-Codes nach Korrektur")

print("✅ Projected GeoDataFrames und ISO-Codes erstellt.")

# =============================================================================
# DATEN AUSLESEN UND VARIABLEN SPEICHERN
# =============================================================================
A_count = len(supply_df)
R_count = len(recycling_df)
Z_count = len(zementwerke_df)
groessen = ["groß"]
G_count = len(groessen)
industrien = ["Wind"]
I_count = len(industrien)
T = [2026, 2031, 2036, 2041, 2046]
T_count = len(T)
year_map = {
    2026: "2026-2030",
    2031: "2031-2035",
    2036: "2036-2040",
    2041: "2041-2045",
    2046: "2046-2050"
}
jahres_cols = list(year_map.values())
print(f"Anzahl Jahre (Perioden): {T_count}")
print(f"Perioden T: {T}")
print(f"Excel-Spalten: {jahres_cols}")

K_r_groß = faktoren_df.iloc[2, 1]
K_r_klein = faktoren_df.iloc[3, 1]
print(f"Große Kapazität ist: {K_r_groß}")
print(f"kleine Kapazität ist: {K_r_klein}")
u = faktoren_df.iloc[0, 1]
print(f"Umwandlungsfaktor u ist: {u}")

q_supply_wind = supply_df[jahres_cols]
q_demand_z = zementwerke_df["Demand GFK in kg"]

# =============================================================================
# KOSTEN DICTS
# =============================================================================
metadata_cols = ["Einheit", "Aspekt", "Parameter"]
iso_cols = [c for c in kostendaten.columns if c not in metadata_cols]

def pick(prefix):
    return kostendaten.loc[kostendaten.index.str.startswith(prefix)]

def to_dict(prefix, drop_prefix=True):
    df = pick(prefix)
    if df.empty:
        return {}
    if len(df) == 1:
        return df.iloc[0][iso_cols].dropna().to_dict()
    out = {}
    for param, row in df.iterrows():
        suffix = param.replace(prefix, "") if drop_prefix else param
        suffix = suffix.strip("_")
        out[suffix] = row[iso_cols].dropna().to_dict()
    return out

cd = {
    ("Wind", "groß"): to_dict("cd_wind_groß"),
    ("Wind", "klein"): to_dict("cd_wind_klein"),
    ("Auto", "klein"): to_dict("cd_auto_klein"),
}
cz = {"groß": to_dict("cz_groß"), "klein": to_dict("cz_klein")}
cr_fix = {"groß": to_dict("cr_fix_groß"), "klein": to_dict("cr_fix_klein")}
lk = {"groß": to_dict("lK_gr"), "klein": to_dict("lK_kl"), "GT": to_dict("lK_GT")}
cb = to_dict("cb")
lkGT = lk["GT"]
fee_gfk = {"groß": to_dict("Annahmegebühr_GFK_groß"), "klein": to_dict("Annahmegebühr_GFK_klein")}
fee_spu = to_dict("Annahmegebühr_Spu")
czw = kostendaten.loc["czw", iso_cols].dropna().to_dict()
infl = {yr: 1.02 ** (yr - 2025) for yr in range(2025, 2056)}

print("✔ Alle Kosten-/Erlös-Dicts erstellt & bereinigt")

# =============================================================================
# TRANSPORTKOSTENMATRIZEN
# =============================================================================
print("Erstelle Kostenmatrizen aus Persyn-Daten...")
A = supply_df["NUTS_ID"].unique().tolist()
R = recycling_df["NUTS_ID"].unique().tolist()
Z = zementwerke_df.index.tolist()
print(f"Anzahl Angebotsorte (A): {len(A)}")
print(f"Anzahl Recyclingorte (R): {len(R)}")
print(f"Anzahl Zement-Regionen (Z): {len(Z)}")

cost_matrix_sup_rec = cost_matrix_persyn.reindex(index=A, columns=R)
cost_matrix_rec_cem = cost_matrix_persyn.reindex(index=R, columns=Z)

missing_ar = cost_matrix_sup_rec.isna().sum().sum()
missing_rz = cost_matrix_rec_cem.isna().sum().sum()
if missing_ar > 0 or missing_rz > 0:
    print(f"⚠️ ACHTUNG: Lücken in den Kostendaten gefunden!")
    penalty_cost = 1000.0
    cost_matrix_sup_rec = cost_matrix_sup_rec.fillna(penalty_cost)
    cost_matrix_rec_cem = cost_matrix_rec_cem.fillna(penalty_cost)
else:
    print("✅ Perfekt! Alle benötigten Verbindungen sind in der Persyn-Matrix enthalten.")

cap_z = zementwerke_df['Demand GFK in kg'].to_dict()

# =============================================================================
# ISO CODES FROM NUTS IDs
# =============================================================================
iso_A = pd.Series(supply_df["NUTS_ID"].str[:2].values, index=supply_df["NUTS_ID"]).to_dict()
iso_R = pd.Series(recycling_df["NUTS_ID"].str[:2].values, index=recycling_df["NUTS_ID"]).to_dict()
iso_Z = pd.Series(zementwerke_df.index.str[:2], index=zementwerke_df.index).to_dict()
print("✅ ISO-Codes aus NUTS-IDs extrahiert.")

# =============================================================================
# GUROBI WRAPPER + MODELL INITIALISIEREN
# =============================================================================
_STATUS_NAMES = {getattr(GRB, name, -1): name for name in dir(GRB) if name.isupper()}

def _sanitize_ascii_name(name: Optional[str], fallback: str = "ct") -> str:
    if not name:
        return fallback
    ascii_name = (
        unicodedata.normalize("NFKD", str(name))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    ascii_name = re.sub(r"[^0-9A-Za-z_\[\].,:=-]+", "_", ascii_name)
    ascii_name = ascii_name.strip("_")
    return ascii_name[:255] if ascii_name else fallback


class Model:
    def __init__(self, name: str = ""):
        self._model = gp.Model(_sanitize_ascii_name(name or "GurobiModel"))
        self._ignored_params = {}

    def __getattr__(self, item):
        return getattr(self._model, item)

    def add_constraint(self, constr, ctname: Optional[str] = None):
        if ctname and str(ctname).strip():
            safe_name = _sanitize_ascii_name(ctname)
            return self._model.addConstr(constr, name=safe_name)
        else:
            return self._model.addConstr(constr)

    def print_information(self):
        self._model.update()
        print("Gurobi-Modellinformation")
        print(f"  Modellname            : {self._model.ModelName}")
        print(f"  Variablen gesamt      : {self._model.NumVars}")
        print(f"  Nebenbedingungen      : {self._model.NumConstrs}")

    def continuous_var_dict(self, keys, name="", lb=0, ub=GRB.INFINITY):
        safe_name = _sanitize_ascii_name(name) if name else ""
        return self._model.addVars(keys, vtype=GRB.CONTINUOUS, name=safe_name, lb=lb, ub=ub)

    def binary_var_dict(self, keys, name=""):
        safe_name = _sanitize_ascii_name(name) if name else ""
        return self._model.addVars(keys, vtype=GRB.BINARY, name=safe_name)

    def sum(self, iterable):
        return gp.quicksum(iterable)

    def minimize(self, expr):
        self._model.setObjective(expr, GRB.MINIMIZE)

    def solve(self, log_output: bool = False):
        self._model.Params.OutputFlag = 1 if log_output else 0
        self._model.optimize()
        if self._model.SolCount > 0:
            return self._model
        return None

mip = Model("Gurobi")
print("Gurobi-Modell initialisiert.")

A = cost_matrix_sup_rec.index.tolist()
R = cost_matrix_sup_rec.columns.tolist()
Z = cost_matrix_rec_cem.columns.tolist()
G = ["groß"]
I = ["Wind"]
K = ["groß", "klein"]

print(f"Anzahl Angebotsorte (A): {len(A)}")
print(f"Anzahl Recyclingorte (R): {len(R)}")
print(f"Anzahl Zement-Regionen (Z): {len(Z)}")
print(f"Betrachtete Jahre (T): {T}")

# =============================================================================
# ENTSCHEIDUNGSVARIABLEN
# =============================================================================
XARG = [(a, r, g, t) for a in A for r in R for g in G for t in T]
XRZ = [(r, z, t) for r in R for z in Z for t in T]
YRK = [(r, k, t) for r in R for k in K for t in T]
BRT = [(r, t) for r in R for t in T]
XRKT = [(r, k, t) for r in R for k in K for t in T]

x_ar_g = mip.continuous_var_dict(XARG, name="x_ar_g")
x_rz = mip.continuous_var_dict(XRZ, name="x_rz")
y_r_k = mip.binary_var_dict(YRK, name="y_r_k")
b_rt = mip.binary_var_dict(BRT, name="b_rt")
dy_r_k = mip.binary_var_dict(YRK, name="dy_r_k")
db_rt = mip.binary_var_dict(BRT, name="db_rt")
flow_rk = mip.continuous_var_dict(XRKT, name="flow_rk", lb=0)

print("Entscheidungsvariablen inkl. Delta-Variablen angelegt.")

# =============================================================================
# NEBENBEDINGUNGEN
# =============================================================================
q_supply_data = supply_df.set_index(["NUTS_ID", "Industrie"])[jahres_cols].copy()
col_to_year = {v: k for k, v in year_map.items()}
q_supply_data.rename(columns=col_to_year, inplace=True)
q_supply_data = q_supply_data.fillna(0.0)

for a in A:
    for t in T:
        qty = q_supply_data.at[(a, "Wind"), t] if (a, "Wind") in q_supply_data.index else 0
        mip.add_constraint(
            mip.sum(x_ar_g[a, r, g, t] for r in R for g in G) == qty,
            ctname=f"supply_bal_a{a}_t{t}"
        )

for r in R:
    for t in T:
        inflow = mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)
        capacity = K_r_groß * y_r_k[r, "groß", t] + K_r_klein * y_r_k[r, "klein", t]
        mip.add_constraint(inflow <= capacity, ctname=f"cap_r{r}_t{t}")

for r in R:
    for t in T:
        mip.add_constraint(
            y_r_k[r, "groß", t] + y_r_k[r, "klein", t] <= 1,
            ctname=f"only_one_size_r{r}_t{t}"
        )

for z in Z:
    N_z = q_demand_z.at[z]
    for t in T:
        mip.add_constraint(
            mip.sum(x_rz[r, z, t] for r in R) <= N_z,
            ctname=f"demand_cap_z{z}_t{t}"
        )

for r in R:
    for t in T:
        mip.add_constraint(
            mip.sum(x_ar_g[a, r, "groß", t] for a in A) <= b_rt[r, t] * K_r_groß
        )

for r in R:
    for t in T:
        mip.add_constraint(
            u * mip.sum(x_ar_g[a, r, g, t] for a in A for g in G) ==
            mip.sum(x_rz[r, z, t] for z in Z),
            ctname=f"mass_balance_r{r}_t{t}"
        )

for r in R:
    for k in K:
        for t_idx in range(len(T) - 1):
            t = T[t_idx]
            t_next = T[t_idx + 1]
            mip.add_constraint(y_r_k[r, k, t] <= y_r_k[r, k, t_next])

for r in R:
    for t_idx in range(len(T) - 1):
        t = T[t_idx]
        t_next = T[t_idx + 1]
        mip.add_constraint(b_rt[r, t] <= b_rt[r, t_next])

for z in Z:
    for t in T:
        mip.add_constraint(
            mip.sum(x_rz[r, z, t] for r in R) <= cap_z[z],
            ctname=f"cap_Z_{z}_{t}"
        )

# Delta-Variablen
for r in R:
    for k in K:
        first_t = T[0]
        mip.add_constraint(dy_r_k[r, k, first_t] == y_r_k[r, k, first_t])
        for t in T[1:]:
            prev = T[T.index(t) - 1]
            mip.add_constraint(dy_r_k[r, k, t] >= y_r_k[r, k, t] - y_r_k[r, k, prev])
            mip.add_constraint(dy_r_k[r, k, t] <= y_r_k[r, k, t])

for r in R:
    first_t = T[0]
    mip.add_constraint(db_rt[r, first_t] == b_rt[r, first_t])
    for t in T[1:]:
        prev = T[T.index(t) - 1]
        mip.add_constraint(db_rt[r, t] >= b_rt[r, t] - b_rt[r, prev])
        mip.add_constraint(db_rt[r, t] <= b_rt[r, t])

# Linearisierung
for r in R:
    for t in T:
        total_inflow = mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)
        mip.add_constraint(total_inflow == flow_rk[r, "groß", t] + flow_rk[r, "klein", t])
        mip.add_constraint(flow_rk[r, "groß", t] <= K_r_groß * y_r_k[r, "groß", t])
        mip.add_constraint(flow_rk[r, "klein", t] <= K_r_klein * y_r_k[r, "klein", t])

print("Alle Nebenbedingungen sind angelegt")
mip.print_information()

# =============================================================================
# ZIELFUNKTION
# =============================================================================
rate = 0.02
infl_yearly = {yr: (1 + rate) ** (yr - 2026) for yr in range(2026, 2056)}
infl_start = {t: infl_yearly[t] for t in T}
infl_avg = {}
for t in T:
    infl_avg[t] = sum(infl_yearly[yr] for yr in range(t, t + 5)) / 5.0

expr = mip.sum([])
VOL_FACTOR_GROSS = 3.33

for t in T:
    period_weight = 5.0
    infl_inv = infl_start[t]
    cost_factor = infl_avg[t] * period_weight

    for (a, r, g, tt) in XARG:
        if tt != t: continue
        iso_a = iso_A.get(a)
        dem_c = cd.get(("Wind", g), {}).get(iso_a, 0)
        expr += cost_factor * dem_c * x_ar_g[a, r, g, t]

    for (a, r, g, tt) in XARG:
        if tt != t: continue
        tk_ar = cost_matrix_sup_rec.at[a, r]
        if g == "groß":
            tk_ar = tk_ar * VOL_FACTOR_GROSS
        expr += cost_factor * tk_ar * x_ar_g[a, r, g, t]

    for (r, z, tt) in XRZ:
        if tt != t: continue
        tk_rz = cost_matrix_rec_cem.at[r, z]
        expr += cost_factor * tk_rz * x_rz[r, z, t]

    for r in R:
        iso_r = iso_R.get(r) or r[:2] or "DE"
        for k in K:
            expr += infl_inv * cr_fix[k].get(iso_r, cr_fix[k].get("DE", 0)) * dy_r_k[r, k, t]
            expr += cost_factor * lk[k].get(iso_r, lk[k].get("DE", 0)) * y_r_k[r, k, t]
            expr += cost_factor * cz[k].get(iso_r, cz[k].get("DE", 0)) * flow_rk[r, k, t]
        expr += infl_inv * cb.get(iso_r, cb.get("DE", 0)) * db_rt[r, t]
        expr += cost_factor * lkGT.get(iso_r, lkGT.get("DE", 0)) * b_rt[r, t]

    for (r, z, tt) in XRZ:
        if tt != t: continue
        iso_z = iso_Z.get(z) or z[:2] or "DE"
        expr += cost_factor * czw.get(iso_z, czw.get("DE", 0)) * x_rz[r, z, t]

mip.minimize(expr)
print("✅ Zielfunktion gesetzt.")

# =============================================================================
# SOLVE
# =============================================================================
SOLUTION_DIR = OUTPUT_BASE / "gurobi_solutions"
SOLUTION_DIR.mkdir(parents=True, exist_ok=True)
solution_filepath = SOLUTION_DIR / "best_solution_Wind.sol"
lp_filepath = SOLUTION_DIR / "model_for_gurobi.lp"

gmodel = mip._model
gmodel.write(str(lp_filepath))

gmodel.Params.TimeLimit = 340000
gmodel.Params.MIPGap = 0.0045
gmodel.Params.Threads = 16
gmodel.Params.SoftMemLimit = 75
gmodel.Params.MemLimit = 95
gmodel.Params.Method = 1
gmodel.Params.NodeMethod = 1
gmodel.Params.PreSparsify = 2

print("Starte Gurobi-Lösungsprozess...")
gmodel.optimize()

if gmodel.SolCount > 0:
    gmodel.write(str(solution_filepath))
    print(f"Lösung gespeichert unter: {solution_filepath}")
    print(f"Zielfunktionswert: {gmodel.ObjVal}")
else:
    print("Keine zulässige Lösung gefunden.")

# =============================================================================
# SOLUTION EXTRACTION (correct: variable name is prepended to key tuple)
# =============================================================================
class GurobiSolutionAdapter:
    def __init__(self, model: gp.Model):
        self.model = model

    @staticmethod
    def _accept_value(value: float, keep_zeros: bool, precision: float = 1e-6) -> bool:
        if not value:
            return keep_zeros
        return abs(value) >= precision

    def get_value_dict(self, var_dict, keep_zeros: bool = True, precision: float = 1e-6):
        if precision < 0:
            raise ValueError("precision muss >= 0 sein")
        if self.model.SolCount <= 0:
            raise RuntimeError("Es ist keine Lösung verfügbar.")
        value_dict = {}
        for key, var in var_dict.items():
            value = var.X
            if self._accept_value(value, keep_zeros, precision):
                value_dict[key] = value
        return value_dict


def backup_solution_tidy(model, var_dicts, output_dir, filename="solution_tidy.csv"):
    # Build flat dict with variable name prepended to each key tuple
    flat = {}
    for var_name, vdict in var_dicts.items():
        for idx, var_ref in vdict.items():
            if isinstance(idx, tuple):
                key = (var_name, *idx)
            else:
                key = (var_name, idx)
            flat[key] = var_ref

    gurobi_solution = GurobiSolutionAdapter(model)
    sol_dict = gurobi_solution.get_value_dict(flat, keep_zeros=False)

    records = []
    for key_tuple, value in sol_dict.items():
        rec = {"variable_type": key_tuple[0], "value": value}
        for i, idx in enumerate(key_tuple[1:]):
            rec[f"idx_{i}"] = idx
        records.append(rec)

    df_tidy = pd.DataFrame.from_records(records)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    csv_path = out_path / filename
    df_tidy.to_csv(csv_path, index=False)
    print(f"→ Tidy-Backup geschrieben nach: {csv_path}")
    return df_tidy


var_dicts = {
    "x_ar_g": x_ar_g,
    "x_rz": x_rz,
    "y_r_k": y_r_k,
    "b_rt": b_rt,
    "dy_r_k": dy_r_k,
    "db_rt": db_rt,
    "flow_rk": flow_rk
}

if gmodel.SolCount > 0:
    print("✅ Zulässige Lösung gefunden")
    print("   Kosten       =", gmodel.ObjVal)
    print("   Solver-Status:", gmodel.Status)
    df_sol_tidy = backup_solution_tidy(gmodel, var_dicts, SOLUTION_DIR)
else:
    print("⚠️ Keine zulässige Lösung gefunden (infeasible oder Time-Limit).")
    import sys; sys.exit(1)

# =============================================================================
# AUSWERTUNG: TIDY DATAFRAMES
# =============================================================================
idx_names = {
    "x_ar_g": ["angebot", "recycling", "größe", "periode"],
    "x_rz": ["recycling", "zementwerk", "periode"],
    "y_r_k": ["recycling", "kapazität", "periode"],
    "b_rt": ["recycling", "periode"],
    "dy_r_k": ["recycling", "kapazität", "periode"],
    "db_rt": ["recycling", "periode"],
    "flow_rk": ["recycling", "kapazität", "periode"]
}

def split_named_indices(df_tidy, mapping):
    result = {}
    for var, names in mapping.items():
        sub = df_tidy.loc[df_tidy.variable_type == var].copy()
        if sub.empty:
            continue
        sub = sub.rename(columns={f"idx_{i}": names[i] for i in range(len(names))})
        cols = ["variable_type", "value"] + names
        result[var] = sub[cols].reset_index(drop=True)
    return result

dfs = split_named_indices(df_sol_tidy, idx_names)

# =============================================================================
# GEOMETRIEN ZUWEISEN
# =============================================================================
sup_geo_unique = supply_gdf.drop_duplicates(subset=["NUTS_ID"])[["geometry", "NUTS_ID"]]
rec_geo_unique = recycling_gdf.drop_duplicates(subset=["NUTS_ID"])[["geometry", "NUTS_ID"]]

dfs_geom = {}

df = dfs["x_ar_g"].copy()
df = df.merge(sup_geo_unique, left_on="angebot", right_on="NUTS_ID", how="left").rename(columns={"geometry": "geom_offer"})
df = df.merge(rec_geo_unique, left_on="recycling", right_on="NUTS_ID", how="left").rename(columns={"geometry": "geom_recycling"})
dfs_geom["x_ar_g"] = gpd.GeoDataFrame(df, geometry="geom_offer", crs=supply_gdf.crs)

df = dfs["x_rz"].copy()
df = df.merge(rec_geo_unique, left_on="recycling", right_on="NUTS_ID", how="left").rename(columns={"geometry": "geom_recycling"})
df = df.merge(cement_gdf[["geometry"]], left_on="zementwerk", right_index=True, how="left").rename(columns={"geometry": "geom_zement"})
dfs_geom["x_rz"] = gpd.GeoDataFrame(df, geometry="geom_recycling", crs=recycling_gdf.crs)

df = dfs["y_r_k"].copy()
df = df.merge(rec_geo_unique, left_on="recycling", right_on="NUTS_ID", how="left").rename(columns={"geometry": "geom_recycling"})
dfs_geom["y_r_k"] = gpd.GeoDataFrame(df, geometry="geom_recycling", crs=recycling_gdf.crs)

for var in ["b_rt", "dy_r_k", "db_rt"]:
    if var in dfs:
        df = dfs[var].copy()
        df = df.merge(rec_geo_unique, left_on="recycling", right_on="NUTS_ID", how="left").rename(columns={"geometry": "geom_recycling"})
        dfs_geom[var] = gpd.GeoDataFrame(df, geometry="geom_recycling", crs=recycling_gdf.crs)
        missing = dfs_geom[var].geometry.isna().sum()
        if missing > 0:
            print(f"⚠️ {var}: {missing} Einträge haben keine Geometrie gefunden!")

print("✅ Geometrien erfolgreich zugewiesen.")

dfs_geom_nz = {name: gdf[gdf["value"] != 0].copy() for name, gdf in dfs_geom.items()}

# =============================================================================
# POLYGONE MIT ABFALLMENGEN VERKNÜPFEN (polys)
# =============================================================================
supply_agg = (
    supply_gdf_proj
    .groupby("NUTS_ID")[jahres_cols]
    .sum()
    .reset_index()
)

col_map_rev = {v: str(k) for k, v in year_map.items()}
supply_agg.rename(columns=col_map_rev, inplace=True)

nuts_level2 = nuts2[nuts2['LEVL_CODE'] == 2].copy()

polys = (
    nuts_level2.merge(supply_agg, on="NUTS_ID", how="left")
    .fillna(0)
)
polys.crs = nuts2.crs

year_cols = [c for c in polys.columns if c.isdigit()]

weights = {'2026': 5, '2031': 5, '2036': 5, '2041': 5, '2046': 5}
polys["sum_all_years"] = 0.0
for col in year_cols:
    w = weights.get(col, 5)
    polys["sum_all_years"] += polys[col] * w

print("✅ Polygone verknüpft und sum_all_years berechnet.")
print(polys[['NUTS_ID', 'CNTR_CODE', '2026', '2031']].head())

# =============================================================================
# FLOW LINE GEOMETRIEN AUFBAUEN (für Jahreskarten)
# =============================================================================
proj_crs = polys.crs
FLOW_PLOT_TOL_KG = 1.0

def _norm_id_series(s):
    return s.astype(str).str.strip()

def _project_points(df, geom_col, target_crs):
    return gpd.GeoSeries(df[geom_col], crs=4326).to_crs(target_crs)

def _build_flow_lines(df, start_col, end_col, target_crs, keep_ids):
    raw = df.copy()
    raw["value"] = pd.to_numeric(raw["value"], errors="coerce").fillna(0)
    raw["periode"] = pd.to_numeric(raw["periode"], errors="coerce")
    raw = raw.loc[raw["value"].abs() > FLOW_PLOT_TOL_KG].copy()
    raw = raw.dropna(subset=[start_col, end_col])
    start_proj = _project_points(raw, start_col, target_crs)
    end_proj = _project_points(raw, end_col, target_crs)
    mask = start_proj.notna() & end_proj.notna() & (~start_proj.is_empty) & (~end_proj.is_empty)
    raw = raw.loc[mask].copy()
    start_proj = start_proj.loc[mask]
    end_proj = end_proj.loc[mask]
    for col in keep_ids:
        if col in raw.columns:
            raw[col] = _norm_id_series(raw[col])
    raw["_start_point_proj"] = list(start_proj)
    raw["_end_point_proj"] = list(end_proj)
    raw["geom_line"] = [LineString([a, b]) for a, b in zip(start_proj, end_proj)]
    return gpd.GeoDataFrame(raw, geometry="geom_line", crs=target_crs)

def _aggregate_routes(gdf, route_cols):
    if gdf.empty:
        return gdf.copy()
    tmp = gdf.copy()
    tmp["value"] = pd.to_numeric(tmp["value"], errors="coerce").fillna(0)
    tmp["periode"] = pd.to_numeric(tmp["periode"], errors="coerce")
    agg = (
        tmp.groupby(route_cols + ["periode"], as_index=False)
           .agg(
               value=("value", "sum"),
               geom_line=("geom_line", "first"),
               _start_point_proj=("_start_point_proj", "first"),
               _end_point_proj=("_end_point_proj", "first")
           )
    )
    return gpd.GeoDataFrame(agg, geometry="geom_line", crs=gdf.crs)

ry_df = dfs_geom_nz["y_r_k"].copy()
ry_df["value"] = pd.to_numeric(ry_df["value"], errors="coerce").fillna(0)
ry_df["periode"] = pd.to_numeric(ry_df["periode"], errors="coerce")
ry_df["recycling"] = _norm_id_series(ry_df["recycling"])
ry_df = ry_df.loc[ry_df["value"] > 0].copy().to_crs(proj_crs)

flows_gdf = _build_flow_lines(
    dfs_geom_nz["x_rz"],
    start_col="geom_recycling",
    end_col="geom_zement",
    target_crs=proj_crs,
    keep_ids=["recycling", "zementwerk"]
)
flows_gdf = _aggregate_routes(flows_gdf, ["recycling", "zementwerk"])

flows_ar_gdf = _build_flow_lines(
    dfs_geom_nz["x_ar_g"],
    start_col="geom_offer",
    end_col="geom_recycling",
    target_crs=proj_crs,
    keep_ids=["angebot", "recycling", "größe"]
)
flows_ar_gdf = _aggregate_routes(flows_ar_gdf, ["angebot", "recycling"])

cem_gdf_plot = cement_gdf_proj.copy().to_crs(proj_crs)
cem_gdf_plot["zementwerk"] = cem_gdf_plot.index.astype(str).str.strip()

print(f"✅ Geometrien für A->R und R->Z erstellt.")
print(f"   A->R Routen > Toleranz: {len(flows_ar_gdf):,}")
print(f"   R->Z Routen > Toleranz: {len(flows_gdf):,}")

# =============================================================================
# PLOT 1-4: CUMULATIVE NETWORK MAPS (4 figures)
# =============================================================================
plt.rcParams["agg.path.chunksize"] = 10_000

NOTEBOOK_NAME = OUTPUT_BASE.name
export_dir = VISUALS_DIR  # save to ./visuals/ instead of nl8336 path

SOURCE_WASTE_COL = "sum_all_years"
xlim = (2.5e6, 6.1e6)
ylim = (1.3e6, 5.5e6)
visible_bbox = box(xlim[0], ylim[0], xlim[1], ylim[1])

period_starts = [2026, 2031, 2036, 2041, 2046]
period_col_map = {}
for y in period_starts:
    if str(y) in polys.columns:
        period_col_map[y] = str(y)
    elif y in polys.columns:
        period_col_map[y] = y
valid_periods = sorted(period_col_map.keys())
period_cols = [period_col_map[y] for y in valid_periods]
horizon_label = f"{min(valid_periods)}-{max(valid_periods) + 4}"

FLOW_LW_MIN = 0.25
FLOW_LW_MAX = 4.20
SHOW_PLOTS = False
SAVE_DPI = 180
FIGSIZE = (18.5, 13.0)
LINE_CHUNK_SIZE = 4000

PLOT1_AR_COLOR = "darkorange"
PLOT2_AR_COLOR = "#7f7f7f"
RZ_COLOR = "purple"
CEMENT_FACE = "#f1c40f"
CEMENT_EDGE_BLUE = "#4b0082"
CEMENT_EDGE_HOTSPOT = "#6a3d9a"
CEMENT_MARKERSIZE = 120
CEMENT_EDGEWIDTH = 1.8

plot1_opening_color_map = {
    "2026-2030": "#d7191c",
    "2031-2035": "#ffd92f",
    "2036-2040": "#ff4fa3",
    "2041-2045": "#90ee90",
    "2046-2050": "#c8ad7f",
}
plot2_opening_color_map = {
    "2026-2030": "#2ca25f",
    "2031-2035": "#ffd92f",
    "2036-2040": "#20b2aa",
    "2041-2045": "#e34a33",
    "2046-2050": "#ff4fa3",
}
size_map = {"klein": 65, "groß": 170}
NO_DATA_LABEL = "no data available"
waste_class_order = [
    NO_DATA_LABEL,
    "<10,000 t GFRP-Waste",
    "10,000-20,000 t GFRP-Waste",
    "20,000-50,000 t GFRP-Waste",
    "50,000-80,000 t GFRP-Waste",
    "80,000-100,000 t GFRP-Waste",
    "100,000-130,000 t GFRP-Waste",
    ">130,000 t GFRP-Waste",
]
hotspot_palette = {
    NO_DATA_LABEL: "#d9d9d9",
    "<10,000 t GFRP-Waste": "#fff7bc",
    "10,000-20,000 t GFRP-Waste": "#fee391",
    "20,000-50,000 t GFRP-Waste": "#fec44f",
    "50,000-80,000 t GFRP-Waste": "#fe9929",
    "80,000-100,000 t GFRP-Waste": "#ec7014",
    "100,000-130,000 t GFRP-Waste": "#cc4c02",
    ">130,000 t GFRP-Waste": "#8c2d04",
}
blue_class_palette = {
    NO_DATA_LABEL: "#d9d9d9",
    "<10,000 t GFRP-Waste": "#deebf7",
    "10,000-20,000 t GFRP-Waste": "#c6dbef",
    "20,000-50,000 t GFRP-Waste": "#9ecae1",
    "50,000-80,000 t GFRP-Waste": "#6baed6",
    "80,000-100,000 t GFRP-Waste": "#4292c6",
    "100,000-130,000 t GFRP-Waste": "#2171b5",
    ">130,000 t GFRP-Waste": "#084594",
}

BINARY_ACTIVE_TOL = 0.5


def detect_offmap_ids(visible_bbox_arg):
    exclude = set()
    def _collect(gdf, id_col):
        if gdf is None or gdf.empty:
            return
        tmp = gdf.copy()
        if tmp.crs != polys.crs:
            tmp = tmp.to_crs(polys.crs)
        if id_col not in tmp.columns:
            tmp[id_col] = tmp.index.astype(str)
        mask_visible = (
            tmp.geometry.notna()
            & (~tmp.geometry.is_empty)
            & tmp.geometry.intersects(visible_bbox_arg)
        )
        exclude.update(tmp.loc[~mask_visible, id_col].astype(str).str.strip().tolist())
    _collect(supply_gdf_proj, "NUTS_ID")
    _collect(recycling_gdf_proj, "NUTS_ID")
    _collect(cem_gdf_plot, "zementwerk")
    return sorted(i for i in exclude if i)

exclude_ids = detect_offmap_ids(visible_bbox)
print(f"Automatisch erkannte Off-Map-IDs: {exclude_ids}")


def normalize_capacity_label(x):
    s = str(x).strip().lower().replace("ß", "ss")
    if any(k in s for k in ["klein", "small"]):
        return "klein"
    if any(k in s for k in ["gross", "large", "big"]):
        return "groß"
    return np.nan


def build_recycling_capacity_lookup(dfs_geom_nz_arg, exclude_ids_arg, binary_tol=BINARY_ACTIVE_TOL):
    candidate_keys = [k for k in ["dy_r_k", "y_r_k"] if k in dfs_geom_nz_arg]
    for key in candidate_keys:
        df = dfs_geom_nz_arg[key].copy()
        if ("recycling" not in df.columns) or ("kapazität" not in df.columns):
            continue
        df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
        df["recycling"] = df["recycling"].astype(str).str.strip()
        df = df.loc[(df["value"] > binary_tol) & (~df["recycling"].isin(exclude_ids_arg))].copy()
        if df.empty:
            continue
        df["kapazität"] = df["kapazität"].apply(normalize_capacity_label)
        df = df.dropna(subset=["kapazität"]).copy()
        if df.empty:
            continue
        out = df[["recycling", "kapazität"]].drop_duplicates().copy()
        out["markersize"] = out["kapazität"].map(size_map).fillna(100)
        return out.reset_index(drop=True), key
    return pd.DataFrame(columns=["recycling", "kapazität", "markersize"]), None


def opening_bucket(p):
    if 2026 <= p <= 2030:   return "2026-2030"
    elif 2031 <= p <= 2035: return "2031-2035"
    elif 2036 <= p <= 2040: return "2036-2040"
    elif 2041 <= p <= 2045: return "2041-2045"
    elif 2046 <= p <= 2050: return "2046-2050"
    return "outside horizon"


def prepare_recycling_openings(dfs_geom_nz_arg, valid_periods_arg, proj_crs_arg,
                                exclude_ids_arg, visible_bbox_arg, capacity_lookup_arg,
                                binary_tol=BINARY_ACTIVE_TOL):
    source_keys = [k for k in ["dy_r_k", "y_r_k"] if k in dfs_geom_nz_arg]
    if not source_keys:
        raise KeyError("Neither 'dy_r_k' nor 'y_r_k' exists in dfs_geom_nz.")
    last_error = None
    for key in source_keys:
        try:
            ry = dfs_geom_nz_arg[key].copy()
            ry["periode"] = pd.to_numeric(ry["periode"], errors="coerce")
            ry["value"] = pd.to_numeric(ry["value"], errors="coerce").fillna(0.0)
            ry = ry.loc[ry["periode"].isin(valid_periods_arg) & (ry["value"] > binary_tol)].copy()
            if ry.empty:
                continue
            ry["recycling"] = ry["recycling"].astype(str).str.strip()
            ry = ry.loc[~ry["recycling"].isin(exclude_ids_arg)].copy()
            if ry.empty:
                continue
            ry_proj = ry.to_crs(proj_crs_arg)
            geom_col = ry_proj.geometry.name
            ry_proj = ry_proj.loc[ry_proj.geometry.notna() & ~ry_proj.geometry.is_empty].copy()
            if ry_proj.empty:
                continue
            if "kapazität" in ry_proj.columns:
                ry_proj["kapazität"] = ry_proj["kapazität"].apply(normalize_capacity_label)
                ry_proj = ry_proj.dropna(subset=["kapazität"]).copy()
                if ry_proj.empty:
                    continue
                ry_open = (
                    ry_proj.sort_values(["recycling", "kapazität", "periode"])
                    .groupby(["recycling", "kapazität"], as_index=False)
                    .agg({"periode": "min", geom_col: "first"})
                )
            else:
                ry_open = (
                    ry_proj.sort_values(["recycling", "periode"])
                    .groupby(["recycling"], as_index=False)
                    .agg({"periode": "min", geom_col: "first"})
                )
                ry_open["kapazität"] = np.nan
            ry_open = gpd.GeoDataFrame(ry_open, geometry=geom_col, crs=proj_crs_arg)
            if capacity_lookup_arg is not None and not capacity_lookup_arg.empty:
                merge_cols = ["recycling", "kapazität"] if "kapazität" in ry_open.columns else ["recycling"]
                lookup_cols = merge_cols + ["markersize"]
                ry_open = ry_open.merge(
                    capacity_lookup_arg[lookup_cols].drop_duplicates(), on=merge_cols, how="left"
                )
            ms = ry_open["kapazität"].map(size_map) if "kapazität" in ry_open.columns else pd.Series(index=ry_open.index, dtype=float)
            if "markersize" in ry_open.columns:
                ms = ms.fillna(pd.to_numeric(ry_open["markersize"], errors="coerce"))
            ry_open["markersize"] = ms.fillna(100)
            ry_open["opening_bucket"] = ry_open["periode"].apply(opening_bucket)
            ry_open = ry_open.loc[
                ry_open.geometry.notna() & ~ry_open.geometry.is_empty & ry_open.intersects(visible_bbox_arg)
            ].copy()
            if not ry_open.empty:
                return ry_open, key
        except Exception as e:
            last_error = e
            continue
    if last_error is not None:
        raise last_error
    raise ValueError("No active/open recycling plants found.")


def classify_waste_tonnes(x):
    if pd.isna(x) or x < 1:    return NO_DATA_LABEL
    elif x < 10_000:            return "<10,000 t GFRP-Waste"
    elif x <= 20_000:           return "10,000-20,000 t GFRP-Waste"
    elif x <= 50_000:           return "20,000-50,000 t GFRP-Waste"
    elif x <= 80_000:           return "50,000-80,000 t GFRP-Waste"
    elif x <= 100_000:          return "80,000-100,000 t GFRP-Waste"
    elif x <= 130_000:          return "100,000-130,000 t GFRP-Waste"
    else:                       return ">130,000 t GFRP-Waste"


def compute_linewidths(values, reference_values=None, lw_min=0.25, lw_max=4.20):
    vals = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0)
    ref = pd.to_numeric(pd.Series(reference_values if reference_values is not None else vals), errors="coerce").fillna(0)
    ref_pos = ref[ref > 0]
    if ref_pos.empty:
        return np.full(len(vals), lw_min)
    floor = ref_pos.min()
    safe_vals = vals.clip(lower=floor)
    log_vals = np.log10(safe_vals)
    log_ref = np.log10(ref_pos)
    vmin = np.nanpercentile(log_ref, 5) if len(log_ref) > 1 else log_ref.iloc[0]
    vmax = np.nanpercentile(log_ref, 95) if len(log_ref) > 1 else log_ref.iloc[0]
    if np.isclose(vmin, vmax):
        return np.full(len(vals), (lw_min + lw_max) / 2)
    log_vals = np.clip(log_vals, vmin, vmax)
    widths = lw_min + (log_vals - vmin) / (vmax - vmin) * (lw_max - lw_min)
    return widths.to_numpy()


def add_weighted_flow_lines(ax, gdf, value_col, color, reference_values=None,
                             alpha=0.5, zorder=1, lw_min=0.25, lw_max=4.20, chunk_size=LINE_CHUNK_SIZE):
    if gdf.empty:
        return
    g = gdf.loc[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
    if g.empty:
        return
    g["_lw"] = compute_linewidths(g[value_col], reference_values=reference_values, lw_min=lw_min, lw_max=lw_max)
    g = g.sort_values("_lw")
    seg_buffer, width_buffer = [], []
    def flush_buffers():
        if seg_buffer:
            lc = LineCollection(seg_buffer, colors=color, linewidths=width_buffer,
                                alpha=alpha, zorder=zorder, capstyle="round", joinstyle="round")
            ax.add_collection(lc)
            seg_buffer.clear(); width_buffer.clear()
    for geom, lw in zip(g.geometry, g["_lw"]):
        parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else ([geom] if geom.geom_type == "LineString" else [])
        for part in parts:
            coords = np.asarray(part.coords)
            if coords.shape[0] < 2:
                continue
            seg_buffer.append(coords); width_buffer.append(float(lw))
            if len(seg_buffer) >= chunk_size:
                flush_buffers()
    flush_buffers()


def make_projected_lines(df, start_geom_col, end_geom_col, target_crs, keep_projected_points=None):
    raw = df.copy().dropna(subset=[start_geom_col, end_geom_col])
    start_proj = gpd.GeoSeries(raw[start_geom_col], crs=4326).to_crs(target_crs)
    end_proj = gpd.GeoSeries(raw[end_geom_col], crs=4326).to_crs(target_crs)
    mask = (~start_proj.is_empty) & (~end_proj.is_empty)
    raw = raw.loc[mask].copy()
    start_proj = start_proj.loc[mask]; end_proj = end_proj.loc[mask]
    if keep_projected_points in (True, "both"):
        raw["_start_point_proj"] = list(start_proj); raw["_end_point_proj"] = list(end_proj)
    elif keep_projected_points == "start":
        raw["_start_point_proj"] = list(start_proj)
    elif keep_projected_points == "end":
        raw["_end_point_proj"] = list(end_proj)
    raw["geometry"] = [LineString([a, b]) for a, b in zip(start_proj, end_proj)]
    return gpd.GeoDataFrame(raw, geometry="geometry", crs=target_crs)


def aggregate_route_flows(gdf, route_cols, valid_periods_arg):
    out = gdf.copy()
    out["periode"] = pd.to_numeric(out["periode"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce").fillna(0)
    out = out.loc[out["periode"].isin(valid_periods_arg) & (out["value"].abs() > FLOW_PLOT_TOL_KG)].copy()
    if out.empty:
        return gpd.GeoDataFrame(columns=route_cols + ["value", "geometry"], geometry="geometry", crs=gdf.crs)
    out = (
        out.groupby(route_cols, as_index=False)
        .agg(value=("value", "sum"), geometry=("geometry", "first"))
    )
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def plot_recycling_opening_layers(ax, ry_open_arg, opening_color_map_arg):
    if ry_open_arg is None or ry_open_arg.empty:
        return
    for bucket, color in opening_color_map_arg.items():
        sub = ry_open_arg.loc[ry_open_arg["opening_bucket"] == bucket].copy()
        if sub.empty:
            continue
        sub_large = sub.loc[sub["kapazität"] == "groß"].copy()
        if not sub_large.empty:
            sub_large.plot(ax=ax, marker="o", markersize=sub_large["markersize"],
                           color=color, edgecolor="black", linewidth=0.95, zorder=5.0)
        sub_other = sub.loc[sub["kapazität"].isna() | (~sub["kapazität"].isin(["klein", "groß"]))].copy()
        if not sub_other.empty:
            sub_other.plot(ax=ax, marker="o", markersize=sub_other["markersize"],
                           color=color, edgecolor="black", linewidth=0.95, zorder=5.5)
        sub_small = sub.loc[sub["kapazität"] == "klein"].copy()
        if not sub_small.empty:
            sub_small.plot(ax=ax, marker="o", markersize=sub_small["markersize"] * 1.35,
                           color="white", edgecolor="white", linewidth=0.0, zorder=5.8)
            sub_small.plot(ax=ax, marker="o", markersize=sub_small["markersize"],
                           color=color, edgecolor="black", linewidth=1.10, zorder=6.0)


def draw_side_panel(ax_side, class_palette, class_title):
    ax_side.axis("off")
    legend_handles = [Patch(facecolor=class_palette[label], edgecolor="black", label=label)
                      for label in waste_class_order]
    leg = ax_side.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(0.00, 1.00),
                         borderaxespad=0.0, frameon=True, title=class_title,
                         fontsize=8.8, title_fontsize=10.0, labelspacing=0.42,
                         handlelength=1.3, handletextpad=0.6)
    ax_side.add_artist(leg)
    ax_side.text(0.00, 0.42, "Ranking tables exported\nseparately to output folder.",
                 transform=ax_side.transAxes, ha="left", va="top", fontsize=9.5)


def add_bottom_legends(fig, ar_color, opening_color_map_arg, include_ar=True):
    network_handles = []
    if include_ar:
        network_handles.append(Line2D([0], [0], color=ar_color, lw=2.6, label="Supply -> recycling GFRP-Waste flow"))
    network_handles.extend([
        Line2D([0], [0], color=RZ_COLOR, lw=2.6, label="Recycling -> cement flow"),
        Line2D([0], [0], marker="^", color="w", markerfacecolor=CEMENT_FACE,
               markeredgecolor="black", markersize=10, linestyle="None", label="Cement plant with incoming flow")
    ])
    opening_handles = [
        Line2D([0], [0], marker="o", color="w", markerfacecolor=color,
               markeredgecolor="black", markersize=9.5, linestyle="None", label=label)
        for label, color in opening_color_map_arg.items()
    ]
    fig.legend(handles=network_handles, loc="lower center", bbox_to_anchor=(0.43, 0.100),
               ncol=max(2, len(network_handles)), frameon=True, title="Network overlays",
               fontsize=9.5, title_fontsize=10.5)
    fig.legend(handles=opening_handles, loc="lower center", bbox_to_anchor=(0.43, 0.030),
               ncol=5, frameon=True,
               title="Recycling plant opening period (circle size = plant capacity)",
               fontsize=9.5, title_fontsize=10.5)


# Prepare polygon data
polys_all = polys.copy()
source_raw_kg = pd.to_numeric(polys_all[SOURCE_WASTE_COL], errors="coerce")
polys_all["source_waste_kg"] = source_raw_kg
polys_all["Cumulative_GFRP_Waste_tonnes"] = source_raw_kg / 1000.0
polys_all["is_no_data"] = source_raw_kg.isna() | (polys_all["Cumulative_GFRP_Waste_tonnes"] < 1)
polys_plot = polys_all.loc[polys_all.intersects(visible_bbox)].copy()
polys_plot["waste_class"] = np.where(
    polys_plot["is_no_data"], NO_DATA_LABEL,
    polys_plot["Cumulative_GFRP_Waste_tonnes"].apply(classify_waste_tonnes)
)
polys_boundary_plot = polys_plot.boundary

# Prepare flow data for cumulative maps
flows_rz_all = make_projected_lines(dfs_geom_nz["x_rz"], "geom_recycling", "geom_zement",
                                     proj_crs, keep_projected_points="both")
flows_ar_all = make_projected_lines(dfs_geom_nz["x_ar_g"], "geom_offer", "geom_recycling",
                                     proj_crs, keep_projected_points="both")

for _col in ["recycling", "zementwerk"]:
    if _col in flows_rz_all.columns:
        flows_rz_all[_col] = flows_rz_all[_col].astype(str).str.strip()
for _col in ["angebot", "recycling"]:
    if _col in flows_ar_all.columns:
        flows_ar_all[_col] = flows_ar_all[_col].astype(str).str.strip()

flows_rz_all = flows_rz_all.loc[
    ~flows_rz_all["recycling"].astype(str).isin(exclude_ids) &
    ~flows_rz_all["zementwerk"].astype(str).isin(exclude_ids)
].copy()
flows_ar_all = flows_ar_all.loc[
    ~flows_ar_all["angebot"].astype(str).isin(exclude_ids) &
    ~flows_ar_all["recycling"].astype(str).isin(exclude_ids)
].copy()

start_points_ar = gpd.GeoSeries(flows_ar_all["_start_point_proj"], crs=proj_crs)
end_points_ar = gpd.GeoSeries(flows_ar_all["_end_point_proj"], crs=proj_crs)
flows_ar_plot_base = flows_ar_all.loc[
    start_points_ar.intersects(visible_bbox) & end_points_ar.intersects(visible_bbox)
].copy()

start_points_rz = gpd.GeoSeries(flows_rz_all["_start_point_proj"], crs=proj_crs)
end_points_rz = gpd.GeoSeries(flows_rz_all["_end_point_proj"], crs=proj_crs)
flows_rz_plot_base = flows_rz_all.loc[
    start_points_rz.intersects(visible_bbox) & end_points_rz.intersects(visible_bbox)
].copy()

flows_ar_agg_full = aggregate_route_flows(flows_ar_all, ["angebot", "recycling"], valid_periods)
flows_ar_agg_full["tonnes_model"] = pd.to_numeric(flows_ar_agg_full.get("value", 0), errors="coerce").fillna(0) / 1000.0

flows_ar_agg_plot = aggregate_route_flows(flows_ar_plot_base, ["angebot", "recycling"], valid_periods)
flows_ar_agg_plot = flows_ar_agg_plot.loc[flows_ar_agg_plot.intersects(visible_bbox)].copy()
flows_ar_agg_plot["tonnes_plot"] = pd.to_numeric(flows_ar_agg_plot.get("value", 0), errors="coerce").fillna(0) / 1000.0

flows_rz_agg_full = aggregate_route_flows(flows_rz_all, ["recycling", "zementwerk"], valid_periods)
flows_rz_agg_full["tonnes_model"] = pd.to_numeric(flows_rz_agg_full.get("value", 0), errors="coerce").fillna(0) / 1000.0

ar_inbound_by_recycling = (
    flows_ar_agg_full.groupby("recycling", as_index=False)["tonnes_model"]
    .sum().rename(columns={"tonnes_model": "ar_inbound_total_t"})
)
rz_outbound_by_recycling = (
    flows_rz_agg_full.groupby("recycling", as_index=False)["tonnes_model"]
    .sum().rename(columns={"tonnes_model": "rz_outbound_total_t"})
)
flows_rz_agg_full = flows_rz_agg_full.merge(ar_inbound_by_recycling, on="recycling", how="left")
flows_rz_agg_full = flows_rz_agg_full.merge(rz_outbound_by_recycling, on="recycling", how="left")
flows_rz_agg_full["rz_scale_factor"] = np.where(
    flows_rz_agg_full["ar_inbound_total_t"].notna() & (flows_rz_agg_full["rz_outbound_total_t"] > 0),
    flows_rz_agg_full["ar_inbound_total_t"] / flows_rz_agg_full["rz_outbound_total_t"], 1.0
)
flows_rz_agg_full["tonnes_plot"] = flows_rz_agg_full["tonnes_model"] * flows_rz_agg_full["rz_scale_factor"]

visible_rz_routes = aggregate_route_flows(flows_rz_plot_base, ["recycling", "zementwerk"], valid_periods)
if visible_rz_routes.empty:
    flows_rz_agg_plot = gpd.GeoDataFrame(columns=list(flows_rz_agg_full.columns), geometry="geometry", crs=flows_rz_agg_full.crs)
else:
    flows_rz_agg_plot = flows_rz_agg_full.merge(
        visible_rz_routes[["recycling", "zementwerk"]].drop_duplicates(), on=["recycling", "zementwerk"], how="inner"
    )
    flows_rz_agg_plot = gpd.GeoDataFrame(flows_rz_agg_plot, geometry="geometry", crs=flows_rz_agg_full.crs)

flow_reference_t = pd.concat([flows_ar_agg_plot["tonnes_plot"], flows_rz_agg_plot["tonnes_plot"]], ignore_index=True)

# Prepare recycling plants
capacity_lookup, capacity_source_key = build_recycling_capacity_lookup(dfs_geom_nz, exclude_ids)
ry_open, opening_source_key = prepare_recycling_openings(
    dfs_geom_nz, valid_periods, proj_crs, exclude_ids, visible_bbox, capacity_lookup
)
print(f"Recyclingwerke im Plot: {len(ry_open):,}")

# Prepare cement plants
cem_base = cement_gdf_proj.copy()
if cem_base.crs is None:
    cem_base = cem_base.set_crs(proj_crs, allow_override=True)
elif cem_base.crs != proj_crs:
    cem_base = cem_base.to_crs(proj_crs)
cem_base["zementwerk"] = cem_base.index.astype(str).str.strip()
cem_base = cem_base.loc[~cem_base["zementwerk"].isin(exclude_ids)].copy()
active_z_ids = flows_rz_agg_plot["zementwerk"].astype(str).str.strip().unique() if not flows_rz_agg_plot.empty else []
active_cem = cem_base.loc[cem_base["zementwerk"].isin(active_z_ids)].copy()
active_cem = gpd.GeoDataFrame(active_cem, geometry=active_cem.geometry.name, crs=cem_base.crs)
active_cem = active_cem.loc[active_cem.intersects(visible_bbox)].copy()


def create_map_figure(include_ar, ar_color, opening_color_map_arg, class_palette,
                      figure_title, export_filename, cement_edgecolor="black"):
    fig = plt.figure(figsize=FIGSIZE)
    gs = fig.add_gridspec(1, 2, width_ratios=[5.8, 0.9], wspace=0.02)
    ax_map = fig.add_subplot(gs[0, 0])
    ax_side = fig.add_subplot(gs[0, 1])
    ax_map.set_aspect("equal")

    map_colors = polys_plot["waste_class"].map(class_palette).fillna("#d9d9d9")
    polys_plot.plot(ax=ax_map, color=map_colors, edgecolor="none", linewidth=0, zorder=0.1)
    polys_boundary_plot.plot(ax=ax_map, color="#111111", linewidth=0.55, alpha=0.90, zorder=0.9)

    if include_ar:
        add_weighted_flow_lines(ax=ax_map, gdf=flows_ar_agg_plot, value_col="tonnes_plot",
                                 color=ar_color, reference_values=flow_reference_t,
                                 alpha=0.48, zorder=1.2, lw_min=FLOW_LW_MIN, lw_max=FLOW_LW_MAX)
    add_weighted_flow_lines(ax=ax_map, gdf=flows_rz_agg_plot, value_col="tonnes_plot",
                             color=RZ_COLOR, reference_values=flow_reference_t,
                             alpha=0.60, zorder=1.3, lw_min=FLOW_LW_MIN, lw_max=FLOW_LW_MAX)
    if not active_cem.empty:
        active_cem.plot(ax=ax_map, marker="^", markersize=CEMENT_MARKERSIZE,
                        color=CEMENT_FACE, edgecolor=cement_edgecolor,
                        linewidth=CEMENT_EDGEWIDTH, zorder=4)
    plot_recycling_opening_layers(ax_map, ry_open, opening_color_map_arg)

    draw_side_panel(ax_side, class_palette, "Cumulative GFRP-Waste classes (tonnes)")
    add_bottom_legends(fig, ar_color, opening_color_map_arg, include_ar=include_ar)

    ax_map.set_xlim(*xlim); ax_map.set_ylim(*ylim); ax_map.axis("off")
    fig.suptitle(f"{NOTEBOOK_NAME} | {figure_title}", fontsize=15, y=0.975)
    fig.subplots_adjust(bottom=0.20, top=0.93)

    out_path = export_dir / export_filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=SAVE_DPI, format="png", facecolor="white", bbox_inches="tight")
    plt.close(fig)
    gc.collect()
    print(f"✅ Saved: {out_path}")
    return out_path


print("Saving main network maps...")
create_map_figure(
    include_ar=True, ar_color=PLOT1_AR_COLOR,
    opening_color_map_arg=plot1_opening_color_map,
    class_palette=blue_class_palette,
    figure_title=f"Cumulative GFRP-Waste network {horizon_label}:\nBlue class | with A->R | circle = opening period/capacity",
    export_filename="01_blue_classes_with_A_to_R.png",
    cement_edgecolor=CEMENT_EDGE_BLUE
)
create_map_figure(
    include_ar=True, ar_color=PLOT2_AR_COLOR,
    opening_color_map_arg=plot2_opening_color_map,
    class_palette=hotspot_palette,
    figure_title=f"Cumulative GFRP-Waste hotspot {horizon_label}:\nHotspot | with A->R | circle = opening period/capacity",
    export_filename="02_hotspot_with_A_to_R.png",
    cement_edgecolor=CEMENT_EDGE_HOTSPOT
)
create_map_figure(
    include_ar=False, ar_color=PLOT1_AR_COLOR,
    opening_color_map_arg=plot1_opening_color_map,
    class_palette=blue_class_palette,
    figure_title=f"Cumulative GFRP-Waste network {horizon_label}:\nBlue class | R->Z only | circle = opening period/capacity",
    export_filename="03_blue_classes_R_to_Z_only.png",
    cement_edgecolor=CEMENT_EDGE_BLUE
)
create_map_figure(
    include_ar=False, ar_color=PLOT2_AR_COLOR,
    opening_color_map_arg=plot2_opening_color_map,
    class_palette=hotspot_palette,
    figure_title=f"Cumulative GFRP-Waste hotspot {horizon_label}:\nHotspot | R->Z only | circle = opening period/capacity",
    export_filename="04_hotspot_R_to_Z_only.png",
    cement_edgecolor=CEMENT_EDGE_HOTSPOT
)

# =============================================================================
# PLOT 5+: YEARLY FLOW MAPS (one per period)
# =============================================================================
from matplotlib.colors import LogNorm

VMIN_FIXED = 1_000
VMAX_FIXED = 20_000_000

visible_bbox_plot = box(xlim[0], ylim[0], xlim[1], ylim[1])

def _inside_bbox(geoms, bbox, crs):
    gs = gpd.GeoSeries(geoms, crs=crs)
    return gs.notna() & (~gs.is_empty) & gs.intersects(bbox)

ry_df_proj = ry_df.copy()
flows_gdf_proj = flows_gdf.copy()
flows_ar_gdf_proj = flows_ar_gdf.copy()
cem_gdf_proj2 = cem_gdf_plot.copy()

ry_df_proj["recycling"] = ry_df_proj["recycling"].astype(str).str.strip()
flows_gdf_proj["recycling"] = flows_gdf_proj["recycling"].astype(str).str.strip()
flows_gdf_proj["zementwerk"] = flows_gdf_proj["zementwerk"].astype(str).str.strip()
flows_ar_gdf_proj["angebot"] = flows_ar_gdf_proj["angebot"].astype(str).str.strip()
flows_ar_gdf_proj["recycling"] = flows_ar_gdf_proj["recycling"].astype(str).str.strip()
cem_gdf_proj2["zementwerk"] = cem_gdf_proj2["zementwerk"].astype(str).str.strip()

ry_df_proj = ry_df_proj.loc[~ry_df_proj["recycling"].isin(exclude_ids)].copy()
flows_gdf_proj = flows_gdf_proj.loc[
    ~flows_gdf_proj["recycling"].isin(exclude_ids) & ~flows_gdf_proj["zementwerk"].isin(exclude_ids)
].copy()
flows_ar_gdf_proj = flows_ar_gdf_proj.loc[
    ~flows_ar_gdf_proj["angebot"].isin(exclude_ids) & ~flows_ar_gdf_proj["recycling"].isin(exclude_ids)
].copy()
cem_gdf_proj2 = cem_gdf_proj2.loc[~cem_gdf_proj2["zementwerk"].isin(exclude_ids)].copy()

mask_rz_visible = (
    _inside_bbox(flows_gdf_proj["_start_point_proj"], visible_bbox_plot, flows_gdf_proj.crs) &
    _inside_bbox(flows_gdf_proj["_end_point_proj"], visible_bbox_plot, flows_gdf_proj.crs)
)
mask_ar_visible = (
    _inside_bbox(flows_ar_gdf_proj["_start_point_proj"], visible_bbox_plot, flows_ar_gdf_proj.crs) &
    _inside_bbox(flows_ar_gdf_proj["_end_point_proj"], visible_bbox_plot, flows_ar_gdf_proj.crs)
)

flows_visible = flows_gdf_proj.loc[mask_rz_visible].copy()
flows_ar_visible = flows_ar_gdf_proj.loc[mask_ar_visible].copy()
ry_visible = ry_df_proj.loc[_inside_bbox(ry_df_proj.geometry, visible_bbox_plot, ry_df_proj.crs)].copy()
cem_visible = cem_gdf_proj2.loc[_inside_bbox(cem_gdf_proj2.geometry, visible_bbox_plot, cem_gdf_proj2.crs)].copy()

years_plot = [2026, 2031, 2036, 2041, 2046]
for yr in years_plot:
    col = str(yr)
    if col not in polys.columns:
        continue

    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw={"aspect": "equal"})

    polys.loc[polys[col] == 0].plot(ax=ax, color="#f0f0f0", edgecolor="white", linewidth=0.2)
    pos = polys.loc[polys[col] > 0]
    if not pos.empty:
        pos.plot(column=col, ax=ax, cmap="YlGnBu",
                 norm=LogNorm(vmin=VMIN_FIXED, vmax=VMAX_FIXED),
                 legend=False, linewidth=0.2, edgecolor="#d0d0d0")

    current_flows_ar = flows_ar_visible.loc[
        (flows_ar_visible["periode"] == yr) &
        (pd.to_numeric(flows_ar_visible["value"], errors="coerce").fillna(0).abs() > FLOW_PLOT_TOL_KG)
    ].copy()
    if not current_flows_ar.empty:
        w_ar = pd.to_numeric(current_flows_ar["value"], errors="coerce").fillna(0).to_numpy()
        w_max_ar = w_ar.max() if w_ar.max() > 0 else 1
        lw_ar = 0.5 + 3.0 * (w_ar / w_max_ar)
        current_flows_ar.plot(ax=ax, linewidth=lw_ar, color="darkorange", alpha=0.6, zorder=1)

    current_flows = flows_visible.loc[
        (flows_visible["periode"] == yr) &
        (pd.to_numeric(flows_visible["value"], errors="coerce").fillna(0).abs() > FLOW_PLOT_TOL_KG)
    ].copy()
    if not current_flows.empty:
        w = pd.to_numeric(current_flows["value"], errors="coerce").fillna(0).to_numpy()
        w_max = w.max() if w.max() > 0 else 1
        lw = 0.5 + 3.0 * (w / w_max)
        current_flows.plot(ax=ax, linewidth=lw, color="purple", alpha=0.6, zorder=2)

    active_z_ids_yr = current_flows["zementwerk"].astype(str).str.strip().unique() if not current_flows.empty else []
    active_cem_yr = cem_visible.loc[cem_visible["zementwerk"].astype(str).str.strip().isin(active_z_ids_yr)].copy()
    if not active_cem_yr.empty:
        active_cem_yr.plot(ax=ax, marker="^", markersize=60, color="#f1c40f",
                           edgecolor="black", linewidth=0.5, zorder=4)

    r_pts = ry_visible.loc[
        (ry_visible["periode"] == yr) &
        (pd.to_numeric(ry_visible["value"], errors="coerce").fillna(0) > 0)
    ].copy()
    if not r_pts.empty:
        r_pts.plot(ax=ax, markersize=r_pts["kapazität"].map({"klein": 40, "groß": 100}).fillna(40),
                   marker="o", color="#e74c3c", edgecolor="black", linewidth=0.8, zorder=5)

    ax.set_xlim(*xlim); ax.set_ylim(*ylim)
    ax.set_title(f"{NOTEBOOK_NAME} | Jahr {yr}: Stoffströme, Recycling & Zement-Logistik", fontsize=14)
    ax.axis("off")
    plt.tight_layout()
    out_path = VISUALS_DIR / f"flow_map_year_{yr}.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close()
    print(f"✅ Saved yearly flow map: {out_path}")

# =============================================================================
# KPI BERECHNUNGEN
# =============================================================================
cap_map_kpi = {"groß": K_r_groß, "klein": K_r_klein}

dy_df   = dfs_geom_nz.get("dy_r_k", pd.DataFrame())
y_df    = dfs_geom_nz.get("y_r_k",  pd.DataFrame())
db_df   = dfs_geom_nz.get("db_rt",  pd.DataFrame())
b_df    = dfs_geom_nz.get("b_rt",   pd.DataFrame())

flows_ar_kpi = dfs_geom_nz["x_ar_g"].copy()
flows_rz_kpi = dfs_geom_nz["x_rz"].copy()
flow_rk_kpi  = dfs.get("flow_rk", pd.DataFrame())
if not flow_rk_kpi.empty:
    flow_rk_kpi = flow_rk_kpi[flow_rk_kpi["value"] > 0].copy()

VOL_FACTOR_GROSS_KPI = 3.33

def get_ar_costs_kpi(row):
    base_cost = cost_matrix_sup_rec.at[row["angebot"], row["recycling"]]
    return base_cost * VOL_FACTOR_GROSS_KPI if row["größe"] == "groß" else base_cost

flows_ar_kpi["tk_ar"] = flows_ar_kpi.apply(get_ar_costs_kpi, axis=1)
flows_rz_kpi["tk_rz"] = flows_rz_kpi.apply(
    lambda r: cost_matrix_rec_cem.at[r["recycling"], r["zementwerk"]], axis=1
)

if not y_df.empty and "iso" not in y_df.columns:
    y_df = y_df.copy()
    y_df["iso"] = y_df["recycling"].map(iso_R)


def get_iso_robust(nuts_id, iso_dict):
    iso = iso_dict.get(nuts_id)
    if iso is None and isinstance(nuts_id, str):
        return nuts_id[:2]
    return iso


def get_cost_value(iso, cost_structure, capacity=None):
    try:
        if capacity is not None:
            if capacity not in cost_structure: return 0.0
            country_dict = cost_structure[capacity]
            val = country_dict.get(iso)
            if val is None: val = country_dict.get("DE", 0.0)
            return val
        else:
            val = cost_structure.get(iso)
            if val is None: val = cost_structure.get("DE", 0.0)
            return val
    except:
        return 0.0


def calc_site_kpis(year):
    yr = int(year)
    works = y_df.query("periode == @yr") if not y_df.empty else pd.DataFrame()
    if works.empty:
        return {"Werke_gesamt": 0, "Werke_klein": 0, "Werke_groß": 0, "Werke_GT": 0,
                "Länder_mit_Werk": 0, "Ø_Auslastung": 0, "Median_Auslastung": 0}
    inflows = flows_ar_kpi.query("periode == @yr").groupby("recycling")["value"].sum().reset_index()
    caps = works[["recycling", "kapazität"]].drop_duplicates().assign(cap_kg=lambda df: df["kapazität"].map(cap_map_kpi))
    util = pd.DataFrame()
    if not inflows.empty:
        util = inflows.merge(caps, on="recycling", how="left").assign(util=lambda df: df["value"] / df["cap_kg"])
    n_gt = 0
    if not b_df.empty:
        n_gt = b_df.query("periode == @yr & value > 0")["recycling"].nunique()
    n_total = works["recycling"].nunique()
    mix = works.groupby("kapazität")["recycling"].nunique()
    return {
        "Werke_gesamt": n_total, "Werke_klein": mix.get("klein", 0), "Werke_groß": mix.get("groß", 0),
        "Werke_GT": n_gt,
        "Länder_mit_Werk": works["iso"].nunique() if "iso" in works.columns else 0,
        "Ø_Auslastung": util["util"].mean() if not util.empty else 0,
        "Median_Auslastung": util["util"].median() if not util.empty else 0,
    }


def calc_cost_kpis(year):
    yr = int(year)
    period_weight = 5.0
    fact_inv = infl_start[yr]
    fact_run = infl_avg[yr] * period_weight

    def safe_sum(series): return series.sum() if not series.empty else 0.0

    ar_y  = flows_ar_kpi.query("periode == @yr")
    rz_y  = flows_rz_kpi.query("periode == @yr")
    dy_y  = dy_df.query("periode == @yr") if not dy_df.empty else pd.DataFrame()
    y_y   = y_df.query("periode == @yr")  if not y_df.empty  else pd.DataFrame()
    db_y  = db_df.query("periode == @yr") if not db_df.empty else pd.DataFrame()
    b_y   = b_df.query("periode == @yr")  if not b_df.empty  else pd.DataFrame()
    flow_y = flow_rk_kpi.query("periode == @yr") if not flow_rk_kpi.empty else pd.DataFrame()

    trans_ar = safe_sum(ar_y["value"] * ar_y["tk_ar"])
    trans_rz = safe_sum(rz_y["value"] * rz_y["tk_rz"])

    base_dem = 0
    if not ar_y.empty:
        def get_dem_cost(r):
            iso = get_iso_robust(r['angebot'], iso_A)
            costs = cd.get(("Wind", r['größe']), {})
            val = costs.get(iso)
            if val is None: val = costs.get("DE", 0)
            return r['value'] * val
        base_dem = safe_sum(ar_y.apply(get_dem_cost, axis=1))

    base_ents = 0
    if not rz_y.empty:
        df_clean = rz_y.drop(columns=['geom_recycling', 'geom_zement', 'geom_line'], errors='ignore')
        def get_ents_cost(r):
            iso = get_iso_robust(r['zementwerk'], iso_Z)
            val = czw.get(iso)
            if val is None: val = czw.get("DE", 0)
            return r['value'] * val
        base_ents = safe_sum(df_clean.apply(get_ents_cost, axis=1))

    def calc_with_helper(df_source, cost_dict, use_cap=True):
        if df_source.empty: return 0.0
        df_clean = df_source.drop(columns=['geom_recycling'], errors='ignore')
        return safe_sum(df_clean.apply(lambda r: r['value'] * get_cost_value(
            get_iso_robust(r['recycling'], iso_R), cost_dict,
            capacity=r['kapazität'] if use_cap else None
        ), axis=1))

    base_inv_fix  = calc_with_helper(dy_y,  cr_fix, use_cap=True)
    base_lauf_fix = calc_with_helper(y_y,   lk,     use_cap=True)
    base_inv_gt   = calc_with_helper(db_y,  cb,     use_cap=False)
    base_lauf_gt  = calc_with_helper(b_y,   lkGT,   use_cap=False)

    base_var_cz = 0
    if not flow_y.empty:
        base_var_cz = safe_sum(flow_y.apply(lambda r: r['value'] * get_cost_value(
            get_iso_robust(r['recycling'], iso_R), cz, capacity=r['kapazität']
        ), axis=1))

    cost_run = (base_dem + trans_ar + trans_rz + base_lauf_fix + base_var_cz + base_lauf_gt + base_ents) * fact_run
    cost_inv = (base_inv_fix + base_inv_gt) * fact_inv
    total = cost_run + cost_inv

    return {
        "Demontage":      base_dem * fact_run,
        "Transport_AR":   trans_ar * fact_run,
        "Transport_RZ":   trans_rz * fact_run,
        "Invest_Werk":    base_inv_fix * fact_inv,
        "Fixkosten_Werk": base_lauf_fix * fact_run,
        "Zerkleinerung":  base_var_cz * fact_run,
        "Invest_GT":      base_inv_gt * fact_inv,
        "Fixkosten_GT":   base_lauf_gt * fact_run,
        "Entsorgung":     base_ents * fact_run,
        "Gesamtkosten":   total,
    }


def aggregate_kpis(years_kpi):
    rows = []
    for yr in years_kpi:
        row = {"Jahr": yr}
        row.update(calc_site_kpis(yr))
        row.update(calc_cost_kpis(yr))
        rows.append(row)
    return pd.DataFrame(rows).set_index("Jahr").sort_index()


years_kpi = range(2026, 2051, 5)
kpi_tbl = aggregate_kpis(years_kpi)
print(kpi_tbl)

# KPI verification
kpi_sum = kpi_tbl["Gesamtkosten"].sum()
solver_sum = gmodel.ObjVal
print(f"KPI-Berechnung:  {kpi_sum:,.2f} €")
print(f"Solver-Lösung:   {solver_sum:,.2f} €")
diff = abs(kpi_sum - solver_sum)
print(f"Differenz:       {diff:,.2f} €")
if diff < 100:
    print("✅ MATCH: Die KPI-Berechnung bildet exakt ab, was der Solver getan hat.")
else:
    print("⚠️ MISMATCH: Die KPI-Berechnung weicht ab.")

# KPI export to Excel
kpi_out_dir = Path("./KPI")
kpi_out_dir.mkdir(parents=True, exist_ok=True)
excel_path = kpi_out_dir / "kpi_Wind_Peak_100_045_final_4.xlsx"
kpi_tbl.to_excel(excel_path, sheet_name="KPIs")
print(f"✅ KPI-Datei geschrieben nach: {excel_path}")

# Also save KPI to visuals dir for convenience
kpi_tbl.to_excel(VISUALS_DIR / "kpi_summary.xlsx", index=True)
print(f"✅ KPI also saved to: {VISUALS_DIR / 'kpi_summary.xlsx'}")

print("\n🎉 CONVERSION COMPLETE!")
print(f"All visualizations saved to: {VISUALS_DIR.absolute()}")
print("Run this script with: python Wind-peak-100_045_final_4.py")

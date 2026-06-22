#!/usr/bin/env python
# coding: utf-8

# # Installationen

# In[ ]:


# Notebook-Zelle (oder Anaconda Prompt)
#!conda install -y --override-channels -c conda-forge "osmnx>=2.1"


# In[ ]:


# in einer Notebook-Zelle oder in der Anaconda Prompt ausführen
#!conda install -y -c conda-forge fiona


# In[ ]:


#Import notwendiger Pakete
#%pip install geopy
#%pip install geopandas
#%pip install osmnx

# in einer leeren Notebook-Zelle ausführen (Shell-Aufruf mit "!"):
#!conda install -y -c conda-forge osmnx pyrosm





# # Importe

# In[ ]:


import pandas as pd
from itertools import zip_longest
import math
from geopy import distance
import os
from pathlib import Path
import geopandas as gpd
from shapely.geometry import Point
import osmnx as ox
import networkx as nx
from tqdm.auto import tqdm


# In[ ]:


import sys
print(sys.version)
import cplex, docplex
print(cplex.__version__, docplex.__version__)


# In[ ]:


import cplex
print(cplex.Cplex().get_version())


# In[ ]:


#Einlesen Excel-Datei ---------HIER DIE DATENBASIS ÄNDERN----------------------
import argparse

_parser = argparse.ArgumentParser(description="Run the GFRP-Waste network optimization for one input scenario.")
_parser.add_argument("input_excel", help="Path to the input Excel scenario file (e.g. Kombiniert_Lichtenegger_294.xlsx)")
_args = _parser.parse_args()

dateipfad = _args.input_excel
OUTPUT_BASE = Path(__file__).parent / Path(dateipfad).stem
OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
print(f"Eingabedatei: {dateipfad}")
print(f"Ausgabeverzeichnis: {OUTPUT_BASE}")

xls = pd.read_excel(
    dateipfad,
    sheet_name=[
        "Kosten",
        "Faktoren",
        "supply wind",
        "supply auto",
        "recyling_location",
        "Integrierte Zementwerke",
        "Transportkosten"
    ]
)

# --- Hilfsfunktion: Spaltennamen säubern ---
def clean_cols(df):
    # Macht alles zu Strings und entfernt Leerzeichen vorne/hinten
    df.columns = df.columns.astype(str).str.strip()
    return df

# --- A) Kosten & Faktoren (Variable in 'kostendaten' umbenannt für Kompatibilität) ---
kostendaten = clean_cols(xls["Kosten"]).dropna(subset=["Parameter"]).set_index("Parameter")
cost_params = kostendaten.drop(columns=["Einheit", "Kommentar", "Quelle", "Aspekt"], errors="ignore").to_dict(orient="index")
faktoren_df = clean_cols(xls["Faktoren"])

# --- B) Transportkosten-Matrix (Persyn) ---
cost_matrix_persyn = clean_cols(xls["Transportkosten"])
# Erste Spalte als Index setzen (enthält die NUTS-IDs)
cost_matrix_persyn = cost_matrix_persyn.set_index(cost_matrix_persyn.columns[0])
# Indizes säubern
cost_matrix_persyn.index = cost_matrix_persyn.index.astype(str).str.strip()
cost_matrix_persyn.columns = cost_matrix_persyn.columns.astype(str).str.strip()

# 1. Wind laden
supply_wind = clean_cols(xls["supply wind"].copy()) # Achten Sie auf den Sheet-Namen in Ihrer Excel!
supply_wind["Industrie"] = "Wind"

# 2. Auto laden (NEU DAZU)
supply_auto = clean_cols(xls["supply auto"].copy()) # Sheet-Name prüfen!
supply_auto["Industrie"] = "Auto"

# 3. Zusammenfügen
supply_df = pd.concat([supply_wind, supply_auto], ignore_index=True)

# 4. Koordinaten säubern
supply_df["x"] = pd.to_numeric(supply_df["x"], errors='coerce')
supply_df["y"] = pd.to_numeric(supply_df["y"], errors='coerce')

# --- D) Recycling-Standorte ---
recycling_df = clean_cols(xls["recyling_location"])

# --- E) Zementwerke (Aggregation + Koordinaten retten) ---
zement_raw = clean_cols(xls["Integrierte Zementwerke"])

# 1. Nachfrage summieren (für das Modell)
# Wir gruppieren nach 'NUTS_ID'
zementwerke_agg = zement_raw.groupby("NUTS_ID")["Demand GFK in kg"].sum()
zementwerke_df = zementwerke_agg.to_frame() 
zementwerke_df.index.name = "Z_ID"

# 2. Koordinaten für die Karte retten (Mittelwert der Koordinaten pro NUTS-ID)
# Damit haben wir einen Punkt pro NUTS-Region für die Visualisierung
zement_coords = zement_raw.groupby("NUTS_ID")[["x", "y"]].mean()
# Koordinaten an das aggregierte DataFrame anfügen
zementwerke_df = zementwerke_df.join(zement_coords)


print("✅ Daten importiert (Wind + Auto).")
print(f"Anzahl Datensätze Supply: {len(supply_df)}")

# =============================================================================
# GEO-DATAFRAMES ERSTELLEN
# =============================================================================

# Supply-Standorte
# Wir entfernen Zeilen ohne Koordinaten, um Fehler zu vermeiden
supply_df_clean = supply_df.dropna(subset=["x", "y"])
supply_gdf = gpd.GeoDataFrame(
    supply_df_clean,
    geometry=gpd.points_from_xy(supply_df_clean["x"], supply_df_clean["y"]),
    crs="EPSG:4326"
)

# Recycling-Standorte
recycling_gdf = gpd.GeoDataFrame(
    recycling_df,
    geometry=gpd.points_from_xy(recycling_df["x"], recycling_df["y"]),
    crs="EPSG:4326"
)

# Zementwerke (jetzt aggregiert auf NUTS-Ebene)
cement_gdf = gpd.GeoDataFrame(
    zementwerke_df,
    geometry=gpd.points_from_xy(zementwerke_df["x"], zementwerke_df["y"]),
    crs="EPSG:4326"
)

print("✅ GeoDataFrames erstellt.")


# In[ ]:


print("Vorhandene Spalten:", supply_df.columns.tolist())
#print("Gewählte Jahres-Spalten:", jahres_cols)


# In[ ]:


# =============================================================================
# DATEN AUSLESEN UND VARIABLEN SPEICHERN
# =============================================================================

# 1) Anzahl Abfallorte / Recyclingorte / Zementorte
A_count = len(supply_df)          
R_count = len(recycling_df)       
Z_count = len(zementwerke_df)     

# 2) Abfall-Größen definieren
groessen = ["groß", "klein"]
G_count  = len(groessen)

# 3) Industrien definieren (JETZT BEIDE)
industrien = ["Wind", "Auto"]
I_count    = len(industrien)

# 4) ZEIT-DEFINITION (5-Jahres-Schritte)
# Wir definieren T hier direkt, damit es konsistent ist.
T =[2026, 2031, 2036, 2041, 2046] 
T_count = len(T)

# Mapping: Modell-Jahr (int) -> Excel-Spaltenname (str)
year_map = {
    2026: "2026-2030",
    2031: "2031-2035",
    2036: "2036-2040",
    2041: "2041-2045",
    2046: "2046-2050"
}

# Liste der Spaltennamen für den Datenzugriff
jahres_cols = list(year_map.values())

print(f"Anzahl Jahre (Perioden): {T_count}")
print(f"Perioden T: {T}")
print(f"Excel-Spalten: {jahres_cols}")


# --- Parameter aus Excel zuweisen ---

# Kapazität
K_r_groß = faktoren_df.iloc[2,1] # große Kapazität Zerkleinerungswerk
K_r_klein = faktoren_df.iloc[3,1] # kleine Kapazität Zerkleinerungswerk

print(f"Große Kapazität ist: {K_r_groß}")
print(f"kleine Kapazität ist: {K_r_klein}")

# Umwandlungsfaktor Abfall mit Spuckstoff
u = faktoren_df.iloc[0,1] 
print(f"Umwandlungsfaktor u ist: {u}") 

# Speicherpfad Szenario
path_Szenario = "/home/kit/iw6717/Solutions/Ergebnisse"

# --- Angebotsmengen Subset ---
# Erstellt ein DataFrame nur mit den relevanten Jahres-Spalten
# (Hinweis: q_supply_wind ist historisch benannt, enthält jetzt aber alle Daten)
q_supply_wind = supply_df[jahres_cols]

# q_demand_z wird direkt aus dem (aggregierten) zementwerke_df geholt
q_demand_z = zementwerke_df["Demand GFK in kg"]


# --- Koordinaten als GeoDataFrames ---

# Supply-Standorte
supply_gdf = gpd.GeoDataFrame(
    supply_df,
    geometry=gpd.points_from_xy(supply_df["x"], supply_df["y"]),
    crs="EPSG:4326"
)

# Recycling-Standorte
recycling_gdf = gpd.GeoDataFrame(
    recycling_df,
    geometry=gpd.points_from_xy(recycling_df["x"], recycling_df["y"]),
    crs="EPSG:4326"
)

# Zementwerke 
# (WICHTIG: Da zementwerke_df jetzt aggregiert ist (NUTS-Ebene), 
# stellen Sie sicher, dass es 'x' und 'y' Spalten hat (z.B. NUTS-Zentroid), 
# falls Sie die Punkte plotten wollen. Für die Kostenmatrix brauchen wir die Koordinaten nicht mehr zwingend.)
# Falls zementwerke_df keine x/y hat, wird dieser Teil fehlschlagen. 
# Haben Sie x/y im aggregierten DF? Wenn nicht, kommentieren wir das hier aus.
if 'x' in zementwerke_df.columns and 'y' in zementwerke_df.columns:
    cement_gdf = gpd.GeoDataFrame(
        zementwerke_df,
        geometry=gpd.points_from_xy(zementwerke_df["x"], zementwerke_df["y"]),
        crs="EPSG:4326"
    )
else:
    print("Info: Zementwerke haben keine direkten Koordinaten (werden über NUTS-ID verknüpft).")
    # Leeres GDF oder Platzhalter, um Fehler später zu vermeiden
    cement_gdf = gpd.GeoDataFrame(zementwerke_df, geometry=[Point(0,0)]*len(zementwerke_df), crs="EPSG:4326")


# In[ ]:


print(supply_gdf.head())


# In[ ]:


print(recycling_gdf.head())


# In[ ]:


print(cement_gdf.head())


# ## Kosten Dicts erstellen

# In[ ]:


# ---------------------------------------------------------------
# A) ISO-Liste und kleine Helfer (KORRIGIERT)
# ---------------------------------------------------------------

# 1. Definieren, was KEINE Länder sind
metadata_cols = ["Einheit", "Aspekt", "Parameter"]

# 2. iso_cols nur mit echten Ländercodes füllen
#    Wir nehmen alle Spalten aus kostendaten, außer den Metadaten
iso_cols = [c for c in kostendaten.columns if c not in metadata_cols]

print(f"Anzahl erkannter Länder-Spalten: {len(iso_cols)}")
# print(f"Erkannte Länder (Auszug): {iso_cols[:5]}") 

def pick(prefix):
    """gibt DF mit allen Zeilen zurück, deren Index mit prefix beginnt"""
    return kostendaten.loc[kostendaten.index.str.startswith(prefix)]

def to_dict(prefix, drop_prefix=True):
    """
    Zeilen, deren Parameter mit `prefix` beginnen, werden zu
    dict[suffix][iso]  oder  dict[iso]  (falls nur 1 Zeile)
    """
    df = pick(prefix)
    if df.empty:
        return {}

    # Fall 1: Nur eine Zeile (z.B. cb) -> Flaches Dictionary
    if len(df) == 1:
        # Wir nutzen nur die iso_cols, um Aspekt/Einheit rauszufiltern
        return df.iloc[0][iso_cols].dropna().to_dict()

    # Fall 2: Mehrere Zeilen (z.B. cz_groß, cz_klein) -> Verschachteltes Dict
    out = {}
    for param, row in df.iterrows(): # Iteration über den Index (Parameter)
        suffix = param.replace(prefix, "") if drop_prefix else param
        suffix = suffix.strip("_")
        # Auch hier: Nur iso_cols nehmen
        out[suffix] = row[iso_cols].dropna().to_dict()
    return out

# ---------------------------------------------------------------
# B)   Länder-spezifische Parameter-Dicts
# ---------------------------------------------------------------
# 1) Demontagekosten (cd)
cd = {
    ("Wind", "groß"): to_dict("cd_wind_groß"),
    ("Wind", "klein"): to_dict("cd_wind_klein"),
    ("Auto", "klein"): to_dict("cd_auto_klein"),
}

# 2) Variable Kosten je Werkgröße
cz = {
    "groß": to_dict("cz_groß"),
    "klein": to_dict("cz_klein"),
}

# 3) Werk-Fixkosten
cr_fix = {
    "groß": to_dict("cr_fix_groß"),
    "klein": to_dict("cr_fix_klein"),
}

# 4) Werk-laufende Kosten
lk = {
    "groß": to_dict("lK_gr"),
    "klein": to_dict("lK_kl"),
    "GT"   : to_dict("lK_GT"),        # Großteil-Linie
}

# 5) Großteil-Einrichtung: einmalig + laufend
cb   = to_dict("cb")       # Invest pro Land
lkGT = lk["GT"]            # Laufende Kosten schon oben eingefangen

# 6) Annahmegebühren
fee_gfk = {
    "groß": to_dict("Annahmegebühr_GFK_groß"),   # große Stücke
    "klein": to_dict("Annahmegebühr_GFK_klein"),   # kleine Stücke
}
fee_spu = to_dict("Annahmegebühr_Spu")

# 7) Entsorgungskosten im Zementwerk
# Auch hier nur iso_cols nutzen
czw = kostendaten.loc["czw", iso_cols].dropna().to_dict()

# ---------------------------------------------------------------
# C)   Inflationsreihe
# ---------------------------------------------------------------
# Unabhängig von 'jahre'-Variable, sicher bis 2055
infl = {yr: 1.02 ** (yr - 2025) for yr in range(2025, 2056)}

print("✔ Alle Kosten-/Erlös-Dicts erstellt & bereinigt")
print("   Beispiele:")
# Hier sollte jetzt kein Fehler mehr kommen, da 'Aspekt' weg ist
print("      Zerkleinern groß DE :", cz['groß'].get('DE'))
print("      Fixkosten klein FR  :", cr_fix['klein'].get('FR'))
print("      Inflation 2030      :", f"{infl[2030]:.4f}")


# In[ ]:


import random, pandas as pd

# ------------------------------------------------------------
# A) Helper – zufällige Matrixwerte
# ------------------------------------------------------------
def sample_matrix(df: pd.DataFrame, n=10, title="Matrix"):
    valid = df.stack().dropna()
    picks = valid.sample(min(n, len(valid)))
    print(f"\n{title}: {len(picks)} zufällige Werte")
    for (i, j), v in picks.items():
        print(f"  {i:>8} → {j:<8}  = {v:,.4f}")

# ------------------------------------------------------------
# B) Helper – zufällige Dict-Werte (beliebig tief: dict[...][...])
# ------------------------------------------------------------
def flatten(d, parent=""):
    """
    wandelt beliebig verschachtelte Dicts in Liste [(key_chain,value), …]
    key_chain: 'groß/DE'  bzw. 'Wind/groß/DE' etc.
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent}/{k}" if parent else str(k)
        if isinstance(v, dict):
            items.extend(flatten(v, new_key))
        else:
            items.append((new_key, v))
    return items

def sample_dict(name, d, n=10, fmt="{k}: {v}"):
    flat = flatten(d)
    if not flat:
        print(f"\n{name}: Dict leer")
        return
    picks = random.sample(flat, min(n, len(flat)))
    print(f"\n{name}: {len(picks)} Stichproben")
    for k, v in picks:
        print(" ", fmt.format(k=k, v=v))

# ------------------------------------------------------------
# C) Aufrufe
# ------------------------------------------------------------
# ---- Transportkosten --------------------------------------------------------
#sample_matrix(cost_matrix_sup_rec, 10, "Supply → Recycling  (€/t)")
#sample_matrix(cost_matrix_rec_cem, 10, "Recycling → Zement (€/t)")

# ---- Kosten & Erlöse --------------------------------------------------------
sample_dict("cz        (Zerkleinerung €/t)",        cz,      10, "{k}: {v:.4f}")
sample_dict("cr_fix    (Fixkosten €/a)",            cr_fix,  10, "{k}: {v:,.0f}")
sample_dict("lk        (laufende Kosten €/a)",      lk,      10, "{k}: {v:,.0f}")
sample_dict("cb        (GT-Invest €/a)",            cb,      10, "{k}: {v:,.0f}")
sample_dict("lkGT      (GT-laufend €/a)",           lkGT,    10, "{k}: {v:,.0f}")
sample_dict("fee_gfk   (GFK-Gebühr €/t)",           fee_gfk, 10, "{k}: {v:.4f}")
sample_dict("fee_spu   (Spuckstoffe €/t)",          fee_spu, 10, "{k}: {v:.4f}")
sample_dict("cd        (Demontage €/t)",            cd,      10, "{k}: {v:.4f}")
sample_dict("czw       (Entsorgung €/t)",           czw,     10, "{k}: {v:.4f}")
sample_dict("infl      (Inflationsfaktor)",         infl,    10, "{k}: {v:.5f}")


# # Länderpolygone laden

# In[ ]:


import sys
print(sys.executable)
import fiona
print("fiona version:", fiona.__version__)


# In[ ]:


# ------------------------------------------------------------------------------
# A) NUTS-0 (Länderpolygone) aus NUTS-2-Shapefile erstellen und Index ausgeben
# ------------------------------------------------------------------------------

# 1. Pfad zum NUTS-2-Shapefile (Europa 2024, EPSG:3035)
#    Bitte hier anpassen, falls Ihre Datei woanders liegt
nuts2_shp = "/home/kit/iw6717/NUTS/NUTS_RG_20M_2016_3035.shp"

print("1. Lade NUTS-2-Shapefile …")
nuts2 = gpd.read_file(nuts2_shp, engine="fiona")
print(f"   → NUTS-2 geladen: {len(nuts2)} Regionen")

# 2. Aggregiere auf CNTR_CODE, um NUTS-0-Ländergrenzen zu erhalten
print("2. Erzeuge NUTS-0 durch Dissolve nach CNTR_CODE …")
nuts0 = nuts2.dissolve(by="CNTR_CODE")

# 3. Optional: Geometrien vereinfachen, um Komplexität zu verringern
print("3. Vereinfachen der Geometrien (Toleranz = 100 m) …")
nuts0["geometry"] = nuts0["geometry"].simplify(tolerance=100)

# 4. Spatial Index für spätere Punkt-in-Polygon-Abfragen
print("4. Erstelle Spatial Index für NUTS-0 …")
nuts0_sindex = nuts0.sindex

# 5. ISO-Länderkürzel (Index von nuts0) ausgeben
print("5. Verfügbare ISO-Länderkürzel (NUTS-0-Index):")
iso_codes = nuts0.index.tolist()
print(iso_codes)

# ------------------------------------------------------------------------------
# Ergebnis dieses Abschnitts:
# - nuts0: GeoDataFrame mit Länderpolygone (NUTS-0), CRS ist EPSG:3035
# - nuts0_sindex: Spatial Index für schnelles Matching
# - iso_codes: Liste aller ISO-Alpha-2-Codes (z. B. 'DE', 'FR', 'TR', …)
# ------------------------------------------------------------------------------


# # Matrizeninitialisierung vorbereiten

# In[ ]:


# ------------------------------------------------------------------------------
# 1. Kosten-Matrizen initialisieren
# ------------------------------------------------------------------------------

# ---------------------------------------------------------------
# D-1  GeoDataFrames in EPSG:3035 projizieren
# ---------------------------------------------------------------
print("Start: Projektion → EPSG:3035")
supply_gdf_proj    = supply_gdf.to_crs("EPSG:3035")
recycling_gdf_proj = recycling_gdf.to_crs("EPSG:3035")
cement_gdf_proj    = cement_gdf.to_crs("EPSG:3035")


# In[ ]:


# Punkte + nuts0 liegen bereits in EPSG:3035
supply_gdf_proj["iso"]    = gpd.sjoin(supply_gdf_proj,    nuts0, how="left")["CNTR_CODE"]
recycling_gdf_proj["iso"] = gpd.sjoin(recycling_gdf_proj, nuts0, how="left")["CNTR_CODE"]
cement_gdf_proj["iso"]    = gpd.sjoin(cement_gdf_proj,    nuts0, how="left")["CNTR_CODE"]

print(supply_gdf_proj.head(10))
#print(recycling_gdf_proj.head(30))
#print(cement_gdf_proj["iso"].head(30))


# In[ ]:


# Kontroll-Snippet: Anzahl fehlender ISO-Codes (NaN) pro Tabelle
for label, gdf in [("Supply",    supply_gdf_proj),
                   ("Recycling", recycling_gdf_proj),
                   ("Cement",    cement_gdf_proj)]:
    fehlende_iso = gdf["iso"].isna().sum()
    print(f"{label:9s}: {fehlende_iso} / {len(gdf)} Einträge ohne ISO-Zuordnung")


supply_gdf_proj[supply_gdf_proj["iso"].isna()]


# In[ ]:


# ------------------------------------------------------------------
# Fehlende ISO-Codes manuell ergänzen
# ------------------------------------------------------------------
# Achtung: 0-basierter Integer-Index (DataFrame.index) wird benutzt.
# Falls Ihre DataFrames einen anderen Index haben (z. B. Werk_ID als
# Label), passen Sie die .loc-Zugriffe entsprechend an.

# 1) Supply-Standorte
#supply_gdf_proj.loc[250, "iso"] = "BA"     # Bosnien-Herzegowina
#supply_gdf_proj.loc[274, "iso"] = "XK"   # Kosovo

# 2) Recycling-Standorte
#recycling_gdf_proj.loc[[12, 13, 14], "iso"] = "BA"
#recycling_gdf_proj.loc[331, "iso"] = "XK"           # Kosovo


# 3) Zementwerke
cement_gdf_proj.loc["FI1D",  "iso"] = "FI"
cement_gdf_proj.loc["UKJ4",       "iso"] = "UK"

# ------------------------------------------------------------------
# Kontrolle: fehlende ISO-Einträge nach Korrektur
# ------------------------------------------------------------------
for name, gdf in [("Supply", supply_gdf_proj),
                  ("Recycling", recycling_gdf_proj),
                  ("Cement", cement_gdf_proj)]:
    fehl = gdf["iso"].isna().sum()
    print(f"{name:9s}: {fehl} fehlende ISO-Codes")


# # Transportkostenmatrix vorbereiten

# In[ ]:


# =============================================================================
# SCHRITT 4: TRANSPORTKOSTEN-MATRIZEN (LOOKUP STATT BERECHNUNG)
# =============================================================================

print("Erstelle Kostenmatrizen aus Persyn-Daten...")

# 1. Sets definieren (zur Sicherheit hier explizit)
#    Diese Listen dienen als Zeilen- und Spaltenfilter für die Matrix
A = supply_df["NUTS_ID"].unique().tolist()
R = recycling_df["NUTS_ID"].unique().tolist()
Z = zementwerke_df.index.tolist() # Das sind die NUTS-IDs der Zementwerke

print(f"Anzahl Angebotsorte (A): {len(A)}")
print(f"Anzahl Recyclingorte (R): {len(R)}")
print(f"Anzahl Zement-Regionen (Z): {len(Z)}")

# 2. Matrix A -> R (Supply -> Recycling) ausschneiden
#    Wir nutzen .reindex(), das ist fehlertolerant:
#    Wenn eine NUTS-ID in der Persyn-Matrix fehlt, wird dort NaN (Not a Number) eingetragen.
cost_matrix_sup_rec = cost_matrix_persyn.reindex(index=A, columns=R)

# 3. Matrix R -> Z (Recycling -> Zement) ausschneiden
cost_matrix_rec_cem = cost_matrix_persyn.reindex(index=R, columns=Z)

# -----------------------------------------------------------------------------
# CHECK AUF LÜCKEN (Missing Data Handling)
# -----------------------------------------------------------------------------
# Da wir nicht wissen, ob Persyn alle Ihre NUTS-Regionen abdeckt, prüfen wir auf NaNs.

missing_ar = cost_matrix_sup_rec.isna().sum().sum()
missing_rz = cost_matrix_rec_cem.isna().sum().sum()

if missing_ar > 0 or missing_rz > 0:
    print(f"\n⚠️ ACHTUNG: Lücken in den Kostendaten gefunden!")
    print(f"   Fehlende Verbindungen A->R: {missing_ar}")
    print(f"   Fehlende Verbindungen R->Z: {missing_rz}")

    # Fallback-Strategie für den Moment:
    # Wir füllen Lücken mit einem sehr hohen Wert (Penalty), damit das Modell läuft,
    # diese Routen aber vermeidet. (Später: Regression/Imputation hier einfügen)
    penalty_cost = 1000.0 # Annahme: Prohibitiv teuer

    cost_matrix_sup_rec = cost_matrix_sup_rec.fillna(penalty_cost)
    cost_matrix_rec_cem = cost_matrix_rec_cem.fillna(penalty_cost)
    print(f"   -> Lücken wurden mit Penalty-Kosten ({penalty_cost}) gefüllt.")
else:
    print("\n✅ Perfekt! Alle benötigten Verbindungen sind in der Persyn-Matrix enthalten.")

# -----------------------------------------------------------------------------
# VORSCHAU
# -----------------------------------------------------------------------------
print("\nKostenmatrix A -> R (Ausschnitt):")
print(cost_matrix_sup_rec.iloc[:5, :5])


# # Kapazitäten der Zementwerke definieren

# In[ ]:


# --------------------------- Parameter ---------------------------------
# 1) Kapazitäts-Dict  {Z_ID: max_tonnes_per_year}
cap_z = zementwerke_df['Demand GFK in kg'].to_dict()


# # Modell initialisieren

# In[ ]:


#CPLEX initialisieren - Modell erzeugen
from docplex.mp.model import Model
mip = Model("Cplex")
print("CPLEX-Modell initialisiert.")


# In[ ]:


# --- Sets definieren ---
# Wir leiten die Sets direkt aus den Indizes/Spalten der fertigen Kostenmatrizen ab.
# Das garantiert, dass es keinen "KeyError" gibt, wenn wir später darauf zugreifen.

A = cost_matrix_sup_rec.index.tolist()    # Angebotsorte
R = cost_matrix_sup_rec.columns.tolist()  # Recyclingorte
Z = cost_matrix_rec_cem.columns.tolist()  # Zement-Regionen

# T (Zeit) haben wir ganz am Anfang schon definiert (2025, 2030...),
# wir nutzen hier das globale T.

# G (Größe) und I (Industrie) - Wir haben gesagt "Nur Wind"
G = ["groß", "klein"] 
I = ["Wind", "Auto"]
K = ["groß", "klein"] # Kapazitäten der Recyclingwerke

print(f"Anzahl Angebotsorte (A): {len(A)}")
print(f"Anzahl Recyclingorte (R): {len(R)}")
print(f"Anzahl Zement-Regionen (Z): {len(Z)}")
print(f"Betrachtete Jahre (T): {T}")


# # Entscheidungsvariablen anlegen

# In[ ]:


# 1. Daten für Constraints vorbereiten (Spaltennamen anpassen)
# ------------------------------------------------------------
# Wir machen aus den Spalten "2025-2029" etc. einfache Jahreszahlen (2025),
# damit der Zugriff in der Schleife `for t in T` funktioniert.

# supply_df hat Spalten "NUTS_ID", "Industrie" und die Zeiträume
q_supply_data = supply_df.set_index(["NUTS_ID", "Industrie"])[jahres_cols].copy()

# Umbenennen der Spalten: "2025-2029" -> 2025
# Wir nutzen das Mapping, das wir am Anfang definiert haben, aber "rückwärts"
col_to_year = {v: k for k, v in year_map.items()} 
q_supply_data.rename(columns=col_to_year, inplace=True)

# Fehlende Werte mit 0 füllen
q_supply_data = q_supply_data.fillna(0.0)

print("✅ Angebotsdaten vorbereitet. Spalten:", q_supply_data.columns.tolist())


# In[ ]:


# ============================================================
# Entscheidungsvariablen  (inkl. “Eröffnungs-Delta”)
# ============================================================

# --- Menge aller Tupel für kontinuierliche / binäre Variablen -------
XARG = [(a, r, g, t) for a in A for r in R for g in G for t in T]   # A → R  (groß/klein)
XRZ  = [(r, z, t)    for r in R for z in Z for t in T]              # R → Z
YRK  = [(r, k, t)    for r in R for k in K for t in T]              # Werk r,k offen?
BRT  = [(r, t)       for r in R for t in T]                         # Großteile-Linie aktiv?
SAIGT = [(a, i, g, t) for a in A for i in I for g in G for t in T]


XRKT = [(r, k, t) for r in R for k in K for t in T]                 # Hilfsvariable für Linearisierung
# --- ❶ Transport- und Betriebsmengen --------------------------------
x_ar_g = mip.continuous_var_dict(XARG, name="x_ar_g")   # [t]
x_rz   = mip.continuous_var_dict(XRZ,  name="x_rz")     # [t]

s_aig  = mip.continuous_var_dict(SAIGT,name="s_aig",  lb=0)
# --- ❷ Binäre Statusvariablen ---------------------------------------
y_r_k  = mip.binary_var_dict(YRK, name="y_r_k")         # 1↔Werk r mit Kapazität k offen
b_rt   = mip.binary_var_dict(BRT, name="b_rt")          # 1↔Großteil-Linie aktiv

# --- ❸ Delta-Variablen (1 NUR im Eröffnungsjahr) ---------------------
dy_r_k = mip.binary_var_dict(YRK, name="dy_r_k")        # 1↔Werk r,k wird neu eröffnet
db_rt  = mip.binary_var_dict(BRT, name="db_rt")         # 1↔Großteil-Linie wird neu installiert

# --- HILFSVARIABLEN zur Linearisierung der variablen Kosten ----
# flow_rk_t: Fluss, der im Jahr t Werk r mit Kapazität k zugerechnet wird
flow_rk = mip.continuous_var_dict(XRKT, name="flow_rk", lb=0) # lb=0 stellt Nicht-Negativität sicher

print("Entscheidungsvariablen inkl. Delta-Variablen angelegt.")


# In[ ]:


print( ("gross" in G), ("groß" in G) )
# und
print( any("gross"  in k for k in x_ar_g), 
       any("groß"   in k for k in x_ar_g) )


# # Nebenbedingungen anlegen

# In[ ]:


# =============================================================================
# DATEN-VORBEREITUNG FÜR NEBENBEDINGUNGEN (KOMBINIERT)
# =============================================================================

# 1. Multi-Index erstellen: Wir müssen nach Ort UND Industrie unterscheiden
#    supply_df enthält jetzt Wind und Auto Daten
q_supply_data = supply_df.set_index(["NUTS_ID", "Industrie"])[jahres_cols].copy()

# 2. Spaltennamen anpassen (Strings "2025-2029" -> Integers 2025)
#    Damit die Schleife `for t in T` (wobei t=2025 ist) die Spalte findet.
col_to_year = {v: k for k, v in year_map.items()} 
q_supply_data.rename(columns=col_to_year, inplace=True)

# 3. Clean up
q_supply_data = q_supply_data.astype(float).fillna(0.0)

print("✅ q_supply_data vorbereitet.")
print("   Index:", q_supply_data.index.names) # Sollte ['NUTS_ID', 'Industrie'] sein
print("   Spalten:", q_supply_data.columns.tolist()) # Sollte [2025, 2030, ...] sein


# In[ ]:


# =============================================================================
# SCHRITT 7: NEBENBEDINGUNGEN (KOMBINIERT: WIND + AUTO)
# =============================================================================

print("Erstelle Nebenbedingungen...")

# ---------------------------------------------------------------------------
# 1. ANGEBOTS-BILANZ & INDUSTRIE-LOGIK (Der Kern des kombinierten Szenarios)
# ---------------------------------------------------------------------------

# A) Daten in s_aig laden
for a in A:
    for i in I:
        for t in T:
            # Wir holen die Menge aus dem Multi-Index DataFrame
            try:
                qty = q_supply_data.loc[(a, i), t]
            except KeyError:
                qty = 0.0 # Keine Daten für diese Kombi an diesem Ort

            # Die Summe der Größen für eine Industrie muss dem Input entsprechen
            mip.add_constraint(
                mip.sum(s_aig[a, i, g, t] for g in G) == qty,
                ctname=f"supply_input_a{a}_i{i}_t{t}"
            )

# B) Logik: Welche Industrie produziert welche Größe?
for a in A:
    for t in T:
        # Wind produziert NUR Großteile -> Kleinteile auf 0 setzen
        mip.add_constraint(s_aig[a, "Wind", "klein", t] == 0, ctname=f"wind_is_large_a{a}_t{t}")

        # Auto produziert NUR Kleinteile -> Großteile auf 0 setzen
        mip.add_constraint(s_aig[a, "Auto", "groß", t] == 0, ctname=f"auto_is_small_a{a}_t{t}")

# C) Link: s_aig (Entstehung) -> x_ar_g (Transport)
for a in A:
    for g in G:
        for t in T:
            # Alles was entsteht (Summe über Industrien), muss abtransportiert werden (Summe über Ziele)
            mip.add_constraint(
                mip.sum(s_aig[a, i, g, t] for i in I) == mip.sum(x_ar_g[a, r, g, t] for r in R),
                ctname=f"link_s_x_a{a}_g{g}_t{t}"
            )

# ---------------------------------------------------------------------------
# 2. KAPAZITÄTEN & FLUSS-LOGIK (Standard)
# ---------------------------------------------------------------------------

# Globale Kapazität der Recyclingwerke
for r in R:
    for t in T:
        inflow = mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)
        capacity = K_r_groß * y_r_k[r, "groß", t] + K_r_klein * y_r_k[r, "klein", t]

        mip.add_constraint(inflow <= capacity, ctname=f"cap_r{r}_t{t}")

# Nur eine Größe pro Werk
for r in R:
    for t in T:
        mip.add_constraint(
            y_r_k[r, "groß", t] + y_r_k[r, "klein", t] <= 1,
            ctname=f"one_size_r{r}_t{t}"
        )

# Großteillinie (b_rt) wird benötigt, wenn Großteile ankommen
for r in R:
    for t in T:
        # Summe aller Großteile (egal woher) <= Kapazität * b_rt
        # (Wir nutzen hier die große Kapazität als "Big-M", das reicht)
        mip.add_constraint(
            mip.sum(x_ar_g[a, r, "groß", t] for a in A) <= b_rt[r, t] * K_r_groß,
            ctname=f"bulk_line_needed_r{r}_t{t}"
        )

# NEU: Großteillinie nur in großen Werken (Logische Verknüpfung)
for r in R:
    for t in T:
        mip.add_constraint(b_rt[r, t] <= y_r_k[r, "groß", t], 
                           ctname=f"bulk_only_in_large_r{r}_t{t}")

# ---------------------------------------------------------------------------
# 3. MASSENBILANZ (Spuckstoffe) & ZEMENTWERKE
# ---------------------------------------------------------------------------

# Massenbilanz im Recyclingwerk (Input * u = Output)
for r in R:
    for t in T:
        mip.add_constraint(
            u * mip.sum(x_ar_g[a, r, g, t] for a in A for g in G) == mip.sum(x_rz[r, z, t] for z in Z),
            ctname=f"mass_balance_r{r}_t{t}"
        )

# Kapazität Zementwerke
# Update der Nachfrage-Variable (sicherstellen, dass es eine Series ist)
q_demand_z = zementwerke_df["Demand GFK in kg"]

for z in Z:
    # Demand für die Region z holen
    N_z = q_demand_z.at[z]
    for t in T:
        mip.add_constraint(
            mip.sum(x_rz[r, z, t] for r in R) <= N_z,
            ctname=f"demand_z{z}_t{t}"
        )

# ---------------------------------------------------------------------------
# 4. ZEITLICHE LOGIK (Irreversibilität & Delta)
# ---------------------------------------------------------------------------

# Werk bleibt offen (Monotonie)
for r in R:
    for k in K:
        for t_idx in range(len(T) - 1):
            t, t_next = T[t_idx], T[t_idx+1]
            mip.add_constraint(y_r_k[r, k, t] <= y_r_k[r, k, t_next], ctname=f"monotone_y_r{r}_{k}_{t}")

# Großteillinie bleibt offen
for r in R:
    for t_idx in range(len(T) - 1):
        t, t_next = T[t_idx], T[t_idx+1]
        mip.add_constraint(b_rt[r, t] <= b_rt[r, t_next], ctname=f"monotone_b_r{r}_{t}")

# Delta-Variablen (Investition nur bei Änderung)
# A) Für y_r_k
for r in R:
    for k in K:
        # Erstes Jahr: Delta = Zustand
        mip.add_constraint(dy_r_k[r, k, T[0]] == y_r_k[r, k, T[0]])
        # Folgejahre: Delta >= Zustand(t) - Zustand(t-1)
        for t_idx in range(1, len(T)):
            t, t_prev = T[t_idx], T[t_idx-1]
            mip.add_constraint(dy_r_k[r, k, t] >= y_r_k[r, k, t] - y_r_k[r, k, t_prev])

# B) Für b_rt
for r in R:
    mip.add_constraint(db_rt[r, T[0]] == b_rt[r, T[0]])
    for t_idx in range(1, len(T)):
        t, t_prev = T[t_idx], T[t_idx-1]
        mip.add_constraint(db_rt[r, t] >= b_rt[r, t] - b_rt[r, t_prev])

# ---------------------------------------------------------------------------
# 5. LINEARISIERUNG (flow_rk)
# ---------------------------------------------------------------------------
for r in R:
    for t in T:
        total_inflow = mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)

        # Split auf flow_variables
        mip.add_constraint(
            total_inflow == flow_rk[r, "groß", t] + flow_rk[r, "klein", t],
            ctname=f"flow_split_r{r}_t{t}"
        )

        # Big-M Constraints (Nur Fluss wenn Werk offen)
        mip.add_constraint(flow_rk[r, "groß", t] <= K_r_groß * y_r_k[r, "groß", t])
        mip.add_constraint(flow_rk[r, "klein", t] <= K_r_klein * y_r_k[r, "klein", t])

print("✅ Alle Nebenbedingungen für 'Wind + Auto' erstellt.")


# In[ ]:


# ============================================================
# Zusatz-Nebenbedingungen für Delta-Variablen
# ============================================================

# ------------------------------------------------------------
# (A) Zusammenhang  dy_r_k  ↔  y_r_k       (Werk-Eröffnung)
#       dy_rk_t = y_rk_t  –  y_rk_{t-1}
# ------------------------------------------------------------
for r in R:
    for k in K:
        first_t = T[0]                      # 2025
        # 1) erstes Jahr: Delta = Status
        mip.add_constraint(dy_r_k[r, k, first_t] == y_r_k[r, k, first_t],
                           ctname=f"deltaY_init_r{r}_k{k}")

        # 2) Folgejahre: 0/1-Logik wie beschrieben
        for t in T[1:]:
            prev = T[T.index(t) - 1]
            mip.add_constraint(dy_r_k[r, k, t] >= y_r_k[r, k, t] - y_r_k[r, k, prev],
                               ctname=f"deltaY_lb_r{r}_k{k}_t{t}")
            mip.add_constraint(dy_r_k[r, k, t] <= y_r_k[r, k, t],
                               ctname=f"deltaY_ub_r{r}_k{k}_t{t}")

# ------------------------------------------------------------
# (B) Zusammenhang  db_rt  ↔  b_rt        (Großteil-Linie)
#       analog zu (A)
# ------------------------------------------------------------
for r in R:
    first_t = T[0]
    mip.add_constraint(db_rt[r, first_t] == b_rt[r, first_t],
                       ctname=f"deltaB_init_r{r}")

    for t in T[1:]:
        prev = T[T.index(t) - 1]
        mip.add_constraint(db_rt[r, t] >= b_rt[r, t] - b_rt[r, prev],
                           ctname=f"deltaB_lb_r{r}_t{t}")
        mip.add_constraint(db_rt[r, t] <= b_rt[r, t],
                           ctname=f"deltaB_ub_r{r}_t{t}")


# In[ ]:


# ============================================================
# Linearisierungs-Nebenbedingungen für variable Kosten
# ============================================================
for r in R:
    for t in T:
        # 1. Der gesamte ankommende Fluss an Werk r wird auf die potenziellen 
        #    Werksgrößen "groß" und "klein" aufgeteilt.
        total_inflow_r_t = mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)
        mip.add_constraint(
            total_inflow_r_t == flow_rk[r, "groß", t] + flow_rk[r, "klein", t],
            ctname=f"flow_split_r{r}_t{t}"
        )

        # 2. Ein Fluss kann einer Werksgröße nur zugerechnet werden, wenn das Werk
        #    diese Größe auch hat. Wir nutzen die Werkskapazität als obere Schranke (Big-M).
        mip.add_constraint(
            flow_rk[r, "groß", t] <= K_r_groß * y_r_k[r, "groß", t],
            ctname=f"link_flow_large_r{r}_t{t}"
        )
        mip.add_constraint(
            flow_rk[r, "klein", t] <= K_r_klein * y_r_k[r, "klein", t],
            ctname=f"link_flow_small_r{r}_t{t}"
        )

print("✅ Linearisierungs-Nebenbedingungen hinzugefügt.")


# In[ ]:


# Anzahl aller Constraints
print(f"Gesamtzahl Nebenbedingungen: {mip.number_of_constraints}")

# (optional) kompaktes Modell-Summary
mip.print_information()


# In[ ]:


from docplex.mp.model import Model
import docplex, sys, inspect, cplex

print("docplex-Version :", docplex.__version__)
print("docplex-Pfad    :", inspect.getfile(docplex))
print("cplex-Version   :", cplex.__version__)
print("Model hat refine_conflict:", hasattr(Model, "refine_conflict"))


# # Zielfunktion Kostenminimierung

# In[ ]:


# =============================================================================
# SCHRITT: ISO-CODES (Der schnelle Weg über NUTS-IDs)
# =============================================================================

# Wir holen uns einfach die ersten 2 Buchstaben der NUTS-ID als Ländercode
# Das ersetzt den komplexen Geo-Berechnungsteil komplett.

# 1. ISO-Dictionary für Angebotsorte (A)
# Wir nehmen die Spalte NUTS_ID, entfernen Duplikate und schneiden die ersten 2 Zeichen ab
iso_A = pd.Series(
    supply_df["NUTS_ID"].str[:2].values, 
    index=supply_df["NUTS_ID"]
).to_dict()

# 2. ISO-Dictionary für Recyclingorte (R)
iso_R = pd.Series(
    recycling_df["NUTS_ID"].str[:2].values, 
    index=recycling_df["NUTS_ID"]
).to_dict()

# 3. ISO-Dictionary für Zementwerke (Z)
# zementwerke_df hat die NUTS_ID ja als Index (Z_ID)
iso_Z = pd.Series(
    zementwerke_df.index.str[:2], 
    index=zementwerke_df.index
).to_dict()

# --- Manuelle Korrektur für UK (falls nötig) ---
# Falls in deinen Kostendaten "UK" steht, aber die NUTS-IDs "UK..." heißen, passt das.
# Falls NUTS "UK" ist, aber Kostendaten "GB" erwarten (oder umgekehrt), müssten wir das hier mappen.
# Meistens passt es aber (UK = UK).

print("✅ ISO-Codes aus NUTS-IDs extrahiert.")
print(f"Beispiel A: {list(iso_A.items())[:3]}")


# In[ ]:


# =============================================================================
# SCHRITT 2: INFLATIONSREIHEN (Basisjahr 2026)
# =============================================================================
rate = 0.02

# 1. Jährliche Basis-Inflation (2026 = 1.0)
infl_yearly = {yr: (1 + rate) ** (yr - 2026) for yr in range(2026, 2056)}

# 2. Für Investitionen: Faktor des jeweiligen Startjahres (2026, 2031...)
infl_start = {t: infl_yearly[t] for t in T}

# 3. Für laufende Kosten: Durchschnitt der 5 Jahre der Periode
infl_avg = {}
for t in T:
    # Berechnet den Durchschnitt von t bis t+4 (also exakt 5 Jahre)
    infl_avg[t] = sum(infl_yearly[yr] for yr in range(t, t + 5)) / 5.0


# In[ ]:


# =============================================================================
# SCHRITT 8 (Kombiniert): ZIELFUNKTION (Kostenminimierung)
# =============================================================================

expr = mip.linear_expr()

print("Erstelle Zielfunktion (Szenario: Wind + Auto)...")

# --- VOLUMENFAKTOR FÜR LOGISTIK ---
VOL_FACTOR_GROSS = 3.33 

for t in T:

    period_weight = 5.0 # Jede Periode ist jetzt exakt 5 Jahre lang!
    infl_inv = infl_start[t]                 # Faktor für einmalige Investitionen
    cost_factor = infl_avg[t] * period_weight # Faktor für laufende Kosten

    # --------------------------------------------------------------
    # 1) DEMONTAGEKOSTEN (€/t)
    #    WICHTIG: Hier nutzen wir s_aig, um nach Industrie zu unterscheiden
    # --------------------------------------------------------------
    for (a, i, g, tt) in SAIGT:
        if tt != t: continue

        iso_a = iso_A.get(a)

        # Kosten lookup: (Industrie, Größe) -> Land
        # i ist hier "Wind" oder "Auto", g ist "groß" oder "klein"
        dem_cost = cd.get((i, g), {}).get(iso_a, 0)

        expr += cost_factor * dem_cost * s_aig[a, i, g, t]

    # --------------------------------------------------------------
    # 2) TRANSPORT A -> R (€/t)
    # --------------------------------------------------------------
    for (a, r, g, tt) in XARG:
        if tt != t: continue

        tk_ar = cost_matrix_sup_rec.at[a, r]

        # Volumenfaktor anwenden, wenn es ein Großteil ist
        if g == "groß":
            tk_ar = tk_ar * VOL_FACTOR_GROSS

        expr += cost_factor * tk_ar * x_ar_g[a, r, g, t]

    # --------------------------------------------------------------
    # 3) TRANSPORT R -> Z (€/t)
    # --------------------------------------------------------------
    for (r, z, tt) in XRZ:
        if tt != t: continue

        tk_rz = cost_matrix_rec_cem.at[r, z]

        expr += cost_factor * tk_rz * x_rz[r, z, t]

    # --------------------------------------------------------------
    # 4) RECYCLING-WERK (Invest + laufend + variabel)
    # --------------------------------------------------------------
    for r in R:
        iso_r = iso_R.get(r)

        # Fallback-Logik für ISO-Codes
        if iso_r is None: iso_r = r[:2]
        if iso_r not in cr_fix["groß"]: iso_r = "DE"

        for k in K:
            # 4a) Investition (EINMALIG -> nur infl_t)
            expr += infl_inv * cr_fix[k][iso_r] * dy_r_k[r, k, t]

            # 4b) Laufende Fixkosten (JÄHRLICH -> cost_factor)
            expr += cost_factor * lk[k][iso_r] * y_r_k[r, k, t]

            # 4c) Variable Zerkleinerung (JÄHRLICH -> cost_factor) --- WIEDER EINGEFÜGT!
            # Wir nutzen hier die Hilfsvariable flow_rk aus der Linearisierung
            expr += cost_factor * cz[k][iso_r] * flow_rk[r, k, t]

        # 4d) Großteillinie
        # Investition (Einmalig -> nur infl_inv)
        expr += infl_inv * cb[iso_r] * db_rt[r, t]

        # Betrieb (JÄHRLICH -> cost_factor)
        expr += cost_factor * lkGT[iso_r] * b_rt[r, t]

    # --------------------------------------------------------------
    # 5) ENTSORGUNG ZEMENTWERK (€/t)
    # --------------------------------------------------------------
    for (r, z, tt) in XRZ:
        if tt != t: continue

        iso_z = iso_Z.get(z)
        expr += cost_factor * czw[iso_z] * x_rz[r, z, t]

# --------------------------------------------------------------
# Zielfunktion setzen
# --------------------------------------------------------------
mip.minimize(expr)
print("✅ Zielfunktion (Kombiniert: Wind+Auto, Faktor 5, Persyn) gesetzt.")


# # Start des Optimierungsmodells

# In[ ]:


import os
from pathlib import Path
import threading
import time

# ----------------- VORBEREITUNG (AUTOMATISIERT) -----------------
SOLUTION_DIR = OUTPUT_BASE / "cplex_solutions"
os.makedirs(SOLUTION_DIR, exist_ok=True)
print(f"Lösungen werden in folgendem Verzeichnis gespeichert: {SOLUTION_DIR}")

# Der Dateipfad für die Lösungsspeicherung
solution_filepath = SOLUTION_DIR / "best_solution_Wind.sol"

# ----------------- PARAMETER TUNING (FINALE ROBUSTE STRATEGIE) -----------------
# Wir arbeiten nur mit dem High-Level-Modell 'mip'
p = mip.parameters

# --- 1) Laufzeit-Grenze ---
# Das CPLEX-Zeitlimit dient nur als Fallback. Unser Thread wird früher zuschlagen.
#p.timelimit.set(340000) # ca. 96 Stunden

# --- 2) MIP Gap Ziel ---
p.mip.tolerances.mipgap.set(0.0045)

# --- 3) RECHENRESSOURCEN & SPEICHER (AGGRESSIV) ---
#p.threads.set(0)
#p.parallel.set(1) # Deterministisch, um alle Kerne zu nutzen
#p.emphasis.memory.set(0)
#p.workmem.set(60000)
#p.mip.limits.treememory.set(60000)

# --- 4) ALGORITHMUS-TUNING ---
p.lpmethod.set(4) # Barrier-Algorithmus


# ----------------- SOLVE (NUR MIT DER HIGH-LEVEL DOCPLEX API) -----------------
print("Starte CPLEX-Lösungsprozess mit 4-Tage-Timer...")
solution = mip.solve(log_output=True)

mip.report()


# In[ ]:


from pathlib import Path
import pandas as pd

def backup_solution_tidy(solution, var_dicts, output_dir, filename="solution_tidy.csv"):
    """
    Schreibt eine ‚tidy‘ CSV aller Solution‐Variablen.

    Parameters
    ----------
    solution : CplexSolution‐Objekt
    var_dicts : dict
        Mapping von Variablen‐Namen (z.B. "x_ar_g") auf den zugehörigen dict 
        der Entscheidungsvariablen (z.B. x_ar_g[(a,r,g,t)] = var).
    output_dir : Path‐Objekt oder str
        Verzeichnis, in das die CSV geschrieben wird.
    filename : str
        Name der Ausgabedatei.

    Returns
    -------
    df_tidy : pandas.DataFrame
        DataFrame im Tidy‐Format.
    """
    # 1) Alle Variablen in ein flaches Mapping nehmen
    flat = {}
    for var_name, vdict in var_dicts.items():
        for idx, var_ref in vdict.items():
            # idx kann ein Tupel oder Skalar sein
            if isinstance(idx, tuple):
                key = (var_name, *idx)
            else:
                key = (var_name, idx)
            flat[key] = var_ref

    # 2) Werte aus der Lösung holen
    sol_dict = solution.get_value_dict(flat)

    # 3) In Records umwandeln
    records = []
    for key_tuple, value in sol_dict.items():
        rec = {"variable_type": key_tuple[0], "value": value}
        for i, idx in enumerate(key_tuple[1:]):
            rec[f"idx_{i}"] = idx
        records.append(rec)
    df_tidy = pd.DataFrame.from_records(records)

    # 4) CSV schreiben
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    csv_path = out_path / filename
    df_tidy.to_csv(csv_path, index=False)
    print(f"→ Tidy‐Backup geschrieben nach: {csv_path}")

    return df_tidy


# -------------------------------
#      Anwendung nach Solve
# -------------------------------
if solution:
    print("✅ Zulässige Lösung gefunden")
    print("   Kosten       =", solution.objective_value)
    print("   Solver-Status:", solution.solve_details.status)

    # Variablendicts bereitstellen
    var_dicts = {
        "x_ar_g": x_ar_g,
        "x_rz": x_rz,
        "s_aig": s_aig,   # <--- WICHTIG: Wieder da!
        "y_r_k": y_r_k,
        "b_rt": b_rt,
        "dy_r_k": dy_r_k,
        "db_rt": db_rt,
        "flow_rk": flow_rk
    }

    # Tidy‐Backup aufrufen
    SOLUTION_DIR = OUTPUT_BASE / "cplex_solutions"
    df_sol_tidy = backup_solution_tidy(solution, var_dicts, SOLUTION_DIR)

else:
    print("⚠️ Keine zulässige Lösung gefunden (infeasible oder Time-Limit).")


# In[ ]:


df_sol_tidy.head()
#df_sol.head()


# # Auswertung

# In[ ]:


idx_names = {
    "x_ar_g": ["angebot","recycling","größe","periode"],
    "x_rz"  : ["recycling","zementwerk","periode"],
    "s_aig" : ["angebot", "industrie", "größe", "periode"], # <--- WICHTIG: Wieder da!
    "y_r_k" : ["recycling","kapazität","periode"],
    "b_rt"  : ["recycling","periode"],
    "dy_r_k": ["recycling","kapazität","periode"],
    "db_rt" : ["recycling","periode"],
    "flow_rk":["recycling","kapazität","periode"]
}

def split_named_indices(df_tidy, mapping):
    """
    Zerlegt df_tidy in ein Dict von DataFrames pro variable_type,
    jeweils mit nur den relevanten Spalten: ['variable_type','value'] + mapping[var].
    """
    result = {}
    for var, names in mapping.items():
        sub = df_tidy.loc[df_tidy.variable_type == var].copy()
        if sub.empty:
            continue
        # idx-Spalten umbenennen
        sub = sub.rename(columns={f"idx_{i}": names[i] for i in range(len(names))})
        # nur relevante Spalten behalten
        cols = ["variable_type", "value"] + names
        result[var] = sub[cols].reset_index(drop=True)
    return result

# Anwendung nach Backup
dfs = split_named_indices(df_sol_tidy, idx_names)

# Beispiel: Zugriff auf das DataFrame für Transport x_ar_g
df_x_ar_g = dfs["x_ar_g"]
print(df_x_ar_g.head())
print("Spalten in df_x_ar_g:", df_x_ar_g.columns.tolist())


# In[ ]:


#Testlauf des Dataframes

for var, df in dfs.items():
    # Nur Einträge mit Wert ungleich 0
    df_nonzero = df[df["value"] != 0]
    print(f"\n=== Variable: {var} (Spalten: {df_nonzero.columns.tolist()}) ===")
    # Maximal 5 zufällige Zeilen anzeigen
    print(df_nonzero.sample(n=min(5, len(df_nonzero)), random_state=42))


# In[ ]:


# =========================================================================
# Konsistenz-Check: Indizes in Lösung vs. Geo-Daten
# =========================================================================

# 1) Angebot
idx_var_angebot   = set(dfs["x_ar_g"]["angebot"].unique())
# FIX: Explizit die Spalte NUTS_ID nutzen, nicht den Index (der könnte numerisch sein)
idx_geo_angebot   = set(supply_gdf["NUTS_ID"]) 

print("Angebot – exakte Übereinstimmung:", idx_var_angebot == idx_geo_angebot)
if idx_var_angebot != idx_geo_angebot:
    print("In Variablen, nicht im GeoDF:", idx_var_angebot - idx_geo_angebot)
    print("Im GeoDF, nicht in Variablen:", idx_geo_angebot - idx_var_angebot)

# 2) Recycling
idx_var_recycling = set(dfs["x_rz"]["recycling"].unique()) | set(dfs["y_r_k"]["recycling"].unique())
# FIX: Explizit Spalte NUTS_ID nutzen
idx_geo_recycling = set(recycling_gdf["NUTS_ID"])

print("\nRecycling – exakte Übereinstimmung:", idx_var_recycling == idx_geo_recycling)
if idx_var_recycling != idx_geo_recycling:
    print("In Variablen, nicht im GeoDF:", idx_var_recycling - idx_geo_recycling)
    print("Im GeoDF, nicht in Variablen:", idx_geo_recycling - idx_var_recycling)

# 3) Zementwerk
# Hier ist .index korrekt, da wir zementwerke_df so aggregiert haben
idx_var_zement    = set(dfs["x_rz"]["zementwerk"].unique())
idx_geo_zement    = set(zementwerke_df.index)

print("\nZementwerk – exakte Übereinstimmung:", idx_var_zement == idx_geo_zement)
if idx_var_zement != idx_geo_zement:
    print("In Variablen, nicht im GeoDF:", idx_var_zement - idx_geo_zement)
    print("Im GeoDF, nicht in Variablen:", idx_geo_zement - idx_var_zement)


# In[ ]:


import geopandas as gpd

# Kopien anlegen, damit dfs selbst unverändert bleibt
dfs_geom = {}

# WICHTIG: Vorbereitung der Geo-Daten
# Wir stellen sicher, dass wir jede NUTS-ID nur einmal haben, um Duplikate beim Merge zu vermeiden.
# Wir nutzen die Spalte "NUTS_ID" als Schlüssel.
sup_geo_unique = supply_gdf.drop_duplicates(subset=["NUTS_ID"])[["geometry", "NUTS_ID"]]
rec_geo_unique = recycling_gdf.drop_duplicates(subset=["NUTS_ID"])[["geometry", "NUTS_ID"]]

# 1) x_ar_g  →  Geometrie Angebot + Recycling
df = dfs["x_ar_g"].copy()

# Merge Angebot: Wir matchen 'angebot' (Lösung) mit 'NUTS_ID' (Geo-Daten)
df = df.merge(
        sup_geo_unique,
        left_on="angebot", right_on="NUTS_ID", how="left"
     ).rename(columns={"geometry":"geom_offer"})

# Merge Recycling: Wir matchen 'recycling' (Lösung) mit 'NUTS_ID' (Geo-Daten)
df = df.merge(
        rec_geo_unique,
        left_on="recycling", right_on="NUTS_ID", how="left"
     ).rename(columns={"geometry":"geom_recycling"})

dfs_geom["x_ar_g"] = gpd.GeoDataFrame(df, geometry="geom_offer", crs=supply_gdf.crs)


# 2) x_rz  →  Geometrie Recycling + Zementwerk
df = dfs["x_rz"].copy()

# Merge Recycling (wieder über NUTS_ID Spalte)
df = df.merge(
        rec_geo_unique,
        left_on="recycling", right_on="NUTS_ID", how="left"
     ).rename(columns={"geometry":"geom_recycling"})

# Merge Zementwerk (HIER ist right_index=True korrekt, da Z_ID der Index ist!)
df = df.merge(
        cement_gdf[["geometry"]],
        left_on="zementwerk", right_index=True, how="left"
     ).rename(columns={"geometry":"geom_zement"})

dfs_geom["x_rz"] = gpd.GeoDataFrame(df, geometry="geom_recycling", crs=recycling_gdf.crs)


# 3) y_r_k  →  Geometrie Recycling
df = dfs["y_r_k"].copy()
df = df.merge(
        rec_geo_unique,
        left_on="recycling", right_on="NUTS_ID", how="left"
     ).rename(columns={"geometry":"geom_recycling"})

dfs_geom["y_r_k"] = gpd.GeoDataFrame(df, geometry="geom_recycling", crs=recycling_gdf.crs)


# -------------------------------------------
# Ergänzung: Geometrien für b_rt, dy_r_k, db_rt
# -------------------------------------------
for var in ["b_rt", "dy_r_k", "db_rt"]:
    if var in dfs:
        df = dfs[var].copy()
        df = df.merge(
            rec_geo_unique,
            left_on="recycling", right_on="NUTS_ID", how="left"
        ).rename(columns={"geometry": "geom_recycling"})

        dfs_geom[var] = gpd.GeoDataFrame(
            df, geometry="geom_recycling", crs=recycling_gdf.crs
        )

        # Kurzer Check
        missing = dfs_geom[var].geometry.isna().sum()
        if missing > 0:
            print(f"⚠️ {var}: {missing} Einträge haben keine Geometrie gefunden!")
    else:
        # print(f"Info: Variable {var} nicht im Ergebnis enthalten.")
        pass

print("✅ Geometrien erfolgreich zugewiesen.")


# In[ ]:


for var, df in dfs_geom.items():
    # Nur Einträge mit Wert ungleich 0
    df_nonzero = df[df["value"] != 0]
    print(f"\n=== Variable: {var} (Spalten: {df_nonzero.columns.tolist()}) ===")
    # Maximal 5 zufällige Zeilen anzeigen
    print(df_nonzero.sample(n=min(5, len(df_nonzero)), random_state=42))


# In[ ]:


# Kopie des Dataframes ohne die Variablen die 0 sind
dfs_geom_nz = {name: gdf[gdf["value"] != 0].copy()
               for name, gdf in dfs_geom.items()}


# In[ ]:


# =========================================================================
# Polygone mit Abfallmengen verknüpfen (Robust & Umbenannt)
# =========================================================================

# 1. Abfallmengen pro NUTS_ID aggregieren (Sicherheitsschritt)
#    Wir summieren die Spalten "2025-2029" etc. auf
supply_agg = (
    supply_gdf_proj
    .groupby("NUTS_ID")[jahres_cols]
    .sum()
    .reset_index()
)

# 2. Spalten umbenennen: "2025-2029" -> "2025"
#    Damit die Plot-Schleife gleich die Spalten "2025", "2030" findet.
#    Wir nutzen das year_map von vorhin rückwärts.
col_map_rev = {v: str(k) for k, v in year_map.items()} 
supply_agg.rename(columns=col_map_rev, inplace=True)

# 3. NUTS-Shapefile auf Level 2 filtern (WICHTIG!)
#    Verhindert, dass wir Ländergrenzen (Level 0) statt Regionen bekommen.
nuts_level2 = nuts2[nuts2['LEVL_CODE'] == 2].copy()

# 4. Merge: Polygone mit Daten
polys = (
    nuts_level2.merge(
        supply_agg,
        on="NUTS_ID",
        how="left"
    )
    .fillna(0) # Regionen ohne Windkraft bekommen 0
)

# CRS beibehalten
polys.crs = nuts2.crs

# --- Kontrolle ---
print("✅ Polygone verknüpft.")
# Jetzt sollten hier echte NUTS-IDs (DE21, AT11) stehen und Spalten wie '2025', '2030':
print(polys[['NUTS_ID', 'CNTR_CODE', '2026', '2031']].head())


# In[ ]:


# alle Jahresspalten automatisch identifizieren
year_cols = [c for c in polys.columns if c.isdigit()]

polys["sum_all_years"] = polys[year_cols].sum(axis=1)   # t im gesamten Zeitraum


# ## Kartendarstellung der Abfallmengen

# In[ ]:


# =========================================================================
# Berechnung der absoluten Gesamtmenge (Gewichtet)
# =========================================================================

# 1. Identifiziere die Jahres-Spalten (z.B. '2025', '2030'...)
year_cols = [c for c in polys.columns if c.isdigit()]

# 2. Definiere die Gewichtung (Dauer der Perioden)
#    Standard: 5 Jahre. Ausnahme: 2045 (deckt 2045-2050 ab) -> 6 Jahre.
#    Wir nutzen Strings als Schlüssel, da die Spaltennamen Strings sind.
weights = {
    '2026': 5,
    '2031': 5,
    '2036': 5,
    '2041': 5,
    '2046': 5
}

# 3. Berechne die gewichtete Summe
#    Wir starten mit 0 und addieren für jedes Jahr (Wert * Gewicht)
polys["sum_all_years"] = 0.0

for col in year_cols:
    # Gewicht holen (Fallback auf 5, falls ein Jahr nicht im Dict steht)
    w = weights.get(col, 5)

    # Aufsummieren
    polys["sum_all_years"] += polys[col] * w

print("✅ 'sum_all_years' wurde gewichtet berechnet (Faktoren 5 bzw. 6).")
print(f"Maximales Gesamtaufkommen in einer Region: {polys['sum_all_years'].max():,.0f} kg")


# In[ ]:


import os
import re
import gc
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.collections import LineCollection
from matplotlib.patches import Patch
from shapely.geometry import LineString, box
import numpy as np
import pandas as pd

# reduziert Speicherlast beim Rendern komplexer Geometrien
plt.rcParams["agg.path.chunksize"] = 10_000

# ============================================================
# CUMULATIVE GFRP-WASTE NETWORK MAPS 2026-2050
# Source of truth for NUTS-2 waste:
#   polys["sum_all_years"]
#
# 4 figures:
#   1) Blue class map with A->R and R->Z
#   2) Hotspot map with A->R and R->Z
#   3) Blue class map with R->Z only
#   4) Hotspot map with R->Z only
#
# Ranking / cement tables:
#   - NOT embedded in the plot anymore
#   - exported separately to the same output folder
#
# Exports:
#   /home/kit/nl8336/Homelaufwerk/Python/Plot/<NotebookName>/
# ============================================================

# ------------------------------------------------------------
# 0) Required objects
# ------------------------------------------------------------
required = ["polys", "dfs_geom_nz"]
missing = [name for name in required if name not in globals()]
if missing:
    raise NameError(f"Missing notebook objects: {missing}")

if "cement_gdf_proj" in globals():
    cem_gdf_base = cement_gdf_proj.copy()
elif "cem_gdf" in globals():
    cem_gdf_base = cem_gdf.copy()
else:
    raise NameError("Neither `cement_gdf_proj` nor `cem_gdf` was found.")

# ------------------------------------------------------------
# 1) Settings
# ------------------------------------------------------------
SOURCE_WASTE_COL = "sum_all_years"

# Continental EU view in EPSG:3035
xlim = (2.5e6, 6.1e6)
ylim = (1.3e6, 5.5e6)
visible_bbox = box(xlim[0], ylim[0], xlim[1], ylim[1])

# Off-Map-IDs dynamisch aus den aktuellen Geodaten bestimmen.
# Damit werden für die jeweilige Excel-Datei genau die Regionen entfernt,
# deren Punkte außerhalb des kontinentalen Kartenausschnitts liegen.
def detect_offmap_ids(visible_bbox):
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
            & tmp.geometry.intersects(visible_bbox)
        )
        exclude.update(
            tmp.loc[~mask_visible, id_col]
               .astype(str)
               .str.strip()
               .tolist()
        )

    _collect(supply_gdf_proj, "NUTS_ID")
    _collect(recycling_gdf_proj, "NUTS_ID")
    _collect(cem_gdf_base, "zementwerk")

    return sorted(i for i in exclude if i)

exclude_ids = detect_offmap_ids(visible_bbox)
print(f"Automatisch erkannte Off-Map-IDs: {exclude_ids}")

period_starts = [2026, 2031, 2036, 2041, 2046]
period_col_map = {}
for y in period_starts:
    if str(y) in polys.columns:
        period_col_map[y] = str(y)
    elif y in polys.columns:
        period_col_map[y] = y

valid_periods = sorted(period_col_map.keys())
period_cols = [period_col_map[y] for y in valid_periods]
if not valid_periods:
    raise ValueError(
        "No valid model period columns found in `polys`. "
        "Expected columns like 2026, 2031, 2036, 2041, 2046."
    )
horizon_label = f"{min(valid_periods)}-{max(valid_periods) + 4}"

FLOW_LW_MIN = 0.25
FLOW_LW_MAX = 4.20
FLOW_PLOT_TOL_KG = 1.0   # nur Visualisierung: entfernt numerisches Solver-Rauschen

# Speicher-/Render-Einstellungen
SHOW_PLOTS = False          # bei Bedarf auf True setzen
SAVE_DPI = 180
FIGSIZE = (18.5, 13.0)
LINE_CHUNK_SIZE = 4000

# Flow colours
PLOT1_AR_COLOR = "darkorange"
PLOT2_AR_COLOR = "#7f7f7f"
RZ_COLOR = "purple"

# Cement plants
CEMENT_FACE = "#f1c40f"
CEMENT_EDGE_BLUE = "#4b0082"
CEMENT_EDGE_HOTSPOT = "#6a3d9a"
CEMENT_MARKERSIZE = 120
CEMENT_EDGEWIDTH = 1.8

# Recycling plant opening colours
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

# Recycling plant marker sizes
size_map = {
    "klein": 65,
    "groß": 170
}

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

proj_crs = polys.crs

# ------------------------------------------------------------
# 2) Notebook/export helpers
# ------------------------------------------------------------
def sanitize_filename(name):
    s = str(name).strip()
    s = re.sub(r'[<>:"/\\|?*]+', "_", s)
    s = s.strip().strip(".")
    return s if s else "Notebook_Unknown"


def get_notebook_name():
    session_name = os.environ.get("JPY_SESSION_NAME")
    if session_name:
        return Path(session_name).stem

    try:
        import ipynbname
        return ipynbname.name()
    except Exception:
        pass

    try:
        from ipykernel import get_connection_file
        kernel_id = Path(get_connection_file()).stem.split("-", 1)[1]
    except Exception:
        kernel_id = None

    if kernel_id is not None:
        servers = []
        try:
            from notebook.notebookapp import list_running_servers
            servers.extend(list(list_running_servers()))
        except Exception:
            pass
        try:
            from jupyter_server.serverapp import list_running_servers as list_running_servers_js
            servers.extend(list(list_running_servers_js()))
        except Exception:
            pass

        try:
            import requests
            for srv in servers:
                try:
                    url = srv.get("url", "").rstrip("/")
                    token = srv.get("token", "")
                    if not url:
                        continue
                    params = {"token": token} if token else None
                    r = requests.get(f"{url}/api/sessions", params=params, timeout=2)
                    if not r.ok:
                        continue
                    for sess in r.json():
                        if sess.get("kernel", {}).get("id") == kernel_id:
                            nb_path = (
                                sess.get("notebook", {}).get("path")
                                or sess.get("path")
                                or ""
                            )
                            if nb_path:
                                return Path(nb_path).stem
                except Exception:
                    continue
        except Exception:
            pass

    return "Notebook_Unknown"


def save_figure(fig, out_path, dpi=SAVE_DPI):
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path,
        dpi=dpi,
        format="jpeg",
        facecolor="white"
    )
    return out_path

# ------------------------------------------------------------
# 3) Helper functions
# ------------------------------------------------------------
def normalize_capacity_label(x):
    s = str(x).strip().lower().replace("ß", "ss")
    if any(k in s for k in ["klein", "small"]):
        return "klein"
    if any(k in s for k in ["gross", "large", "big"]):
        return "groß"
    return np.nan


def build_recycling_capacity_lookup(dfs_geom_nz, exclude_ids):
    candidate_keys = [k for k in ["y_r_k", "dy_r_k"] if k in dfs_geom_nz]

    for key in candidate_keys:
        df = dfs_geom_nz[key].copy()

        if ("recycling" not in df.columns) or ("kapazität" not in df.columns):
            continue

        tmp = df[["recycling", "kapazität"]].copy()
        tmp = tmp.loc[~tmp["recycling"].astype(str).isin(exclude_ids)].copy()
        tmp["kapazität_norm"] = tmp["kapazität"].apply(normalize_capacity_label)
        tmp["cap_rank"] = tmp["kapazität_norm"].map({"klein": 1, "groß": 2})
        tmp = tmp.dropna(subset=["cap_rank"])

        if tmp.empty:
            continue

        out = tmp.groupby("recycling", as_index=False)["cap_rank"].max()
        out["kapazität"] = out["cap_rank"].map({1: "klein", 2: "groß"})
        out["markersize"] = out["kapazität"].map(size_map).fillna(100)
        return out, key

    empty = pd.DataFrame(columns=["recycling", "kapazität", "markersize"])
    return empty, None


def make_projected_lines(df, start_geom_col, end_geom_col, target_crs, keep_projected_points=None):
    raw = df.copy().dropna(subset=[start_geom_col, end_geom_col])

    start_proj = gpd.GeoSeries(raw[start_geom_col], crs=4326).to_crs(target_crs)
    end_proj = gpd.GeoSeries(raw[end_geom_col], crs=4326).to_crs(target_crs)

    mask = (~start_proj.is_empty) & (~end_proj.is_empty)
    raw = raw.loc[mask].copy()
    start_proj = start_proj.loc[mask]
    end_proj = end_proj.loc[mask]

    if keep_projected_points in (True, "both"):
        raw["_start_point_proj"] = list(start_proj)
        raw["_end_point_proj"] = list(end_proj)
    elif keep_projected_points == "start":
        raw["_start_point_proj"] = list(start_proj)
    elif keep_projected_points == "end":
        raw["_end_point_proj"] = list(end_proj)

    raw["geometry"] = [LineString([a, b]) for a, b in zip(start_proj, end_proj)]
    return gpd.GeoDataFrame(raw, geometry="geometry", crs=target_crs)


def aggregate_route_flows(gdf, route_cols, valid_periods):
    out = gdf.copy()
    out["periode"] = pd.to_numeric(out["periode"], errors="coerce")
    out["value"] = pd.to_numeric(out["value"], errors="coerce").fillna(0)

    out = out.loc[out["periode"].isin(valid_periods) & (out["value"].abs() > FLOW_PLOT_TOL_KG)].copy()

    if out.empty:
        return gpd.GeoDataFrame(
            columns=route_cols + ["value", "geometry"],
            geometry="geometry",
            crs=gdf.crs
        )

    out = (
        out.groupby(route_cols, as_index=False)
        .agg(value=("value", "sum"), geometry=("geometry", "first"))
    )
    return gpd.GeoDataFrame(out, geometry="geometry", crs=gdf.crs)


def opening_bucket(p):
    if 2026 <= p <= 2030:
        return "2026-2030"
    elif 2031 <= p <= 2035:
        return "2031-2035"
    elif 2036 <= p <= 2040:
        return "2036-2040"
    elif 2041 <= p <= 2045:
        return "2041-2045"
    elif 2046 <= p <= 2050:
        return "2046-2050"
    return "outside horizon"


def classify_waste_tonnes(x):
    if pd.isna(x) or x < 1:
        return NO_DATA_LABEL
    elif x < 10_000:
        return "<10,000 t GFRP-Waste"
    elif x <= 20_000:
        return "10,000-20,000 t GFRP-Waste"
    elif x <= 50_000:
        return "20,000-50,000 t GFRP-Waste"
    elif x <= 80_000:
        return "50,000-80,000 t GFRP-Waste"
    elif x <= 100_000:
        return "80,000-100,000 t GFRP-Waste"
    elif x <= 130_000:
        return "100,000-130,000 t GFRP-Waste"
    else:
        return ">130,000 t GFRP-Waste"


def compute_linewidths(values, reference_values=None, lw_min=0.25, lw_max=4.20):
    vals = pd.to_numeric(pd.Series(values), errors="coerce").fillna(0)

    if reference_values is None:
        ref = vals.copy()
    else:
        ref = pd.to_numeric(pd.Series(reference_values), errors="coerce").fillna(0)

    ref_pos = ref[ref > 0]
    if ref_pos.empty:
        return np.full(len(vals), lw_min)

    floor = ref_pos.min()
    safe_vals = vals.clip(lower=floor)

    log_vals = np.log10(safe_vals)
    log_ref = np.log10(ref_pos)

    if len(log_ref) > 1:
        vmin = np.nanpercentile(log_ref, 5)
        vmax = np.nanpercentile(log_ref, 95)
    else:
        vmin = log_ref.iloc[0]
        vmax = log_ref.iloc[0]

    if np.isclose(vmin, vmax):
        return np.full(len(vals), (lw_min + lw_max) / 2)

    log_vals = np.clip(log_vals, vmin, vmax)
    widths = lw_min + (log_vals - vmin) / (vmax - vmin) * (lw_max - lw_min)
    return widths.to_numpy()


def add_weighted_flow_lines(
    ax,
    gdf,
    value_col,
    color,
    reference_values=None,
    alpha=0.5,
    zorder=1,
    lw_min=0.25,
    lw_max=4.20,
    chunk_size=LINE_CHUNK_SIZE
):
    if gdf.empty:
        return

    g = gdf.loc[gdf.geometry.notnull() & ~gdf.geometry.is_empty].copy()
    if g.empty:
        return

    g["_lw"] = compute_linewidths(
        g[value_col],
        reference_values=reference_values,
        lw_min=lw_min,
        lw_max=lw_max
    )

    g = g.sort_values("_lw")

    seg_buffer = []
    width_buffer = []

    def flush_buffers():
        nonlocal seg_buffer, width_buffer
        if seg_buffer:
            lc = LineCollection(
                seg_buffer,
                colors=color,
                linewidths=width_buffer,
                alpha=alpha,
                zorder=zorder,
                capstyle="round",
                joinstyle="round"
            )
            ax.add_collection(lc)
            seg_buffer = []
            width_buffer = []

    for geom, lw in zip(g.geometry, g["_lw"]):
        if geom.geom_type == "LineString":
            parts = [geom]
        elif geom.geom_type == "MultiLineString":
            parts = list(geom.geoms)
        else:
            continue

        for part in parts:
            coords = np.asarray(part.coords)
            if coords.shape[0] < 2:
                continue
            seg_buffer.append(coords)
            width_buffer.append(float(lw))

            if len(seg_buffer) >= chunk_size:
                flush_buffers()

    flush_buffers()


def prepare_recycling_openings(
    dfs_geom_nz,
    valid_periods,
    proj_crs,
    exclude_ids,
    visible_bbox,
    capacity_lookup
):
    source_keys = [k for k in ["dy_r_k", "y_r_k"] if k in dfs_geom_nz]
    if not source_keys:
        raise KeyError("Neither 'dy_r_k' nor 'y_r_k' exists in dfs_geom_nz.")

    last_error = None

    for key in source_keys:
        try:
            ry_df = dfs_geom_nz[key].copy()
            ry_df["periode"] = pd.to_numeric(ry_df["periode"], errors="coerce")
            ry_df["value"] = pd.to_numeric(ry_df["value"], errors="coerce").fillna(0)

            ry_df = ry_df.loc[
                ry_df["periode"].isin(valid_periods) &
                (ry_df["value"] > 0)
            ].copy()

            if ry_df.empty:
                continue

            ry_df_proj = ry_df.to_crs(proj_crs)
            geom_col = ry_df_proj.geometry.name

            if geom_col not in ry_df_proj.columns:
                ry_df_proj[geom_col] = ry_df_proj.geometry

            ry_df_proj = ry_df_proj.loc[
                ~ry_df_proj["recycling"].astype(str).isin(exclude_ids)
            ].copy()

            if ry_df_proj.empty:
                continue

            if "kapazität" in ry_df_proj.columns:
                ry_df_proj["kap_norm"] = ry_df_proj["kapazität"].apply(normalize_capacity_label)
                ry_df_proj["cap_rank"] = ry_df_proj["kap_norm"].map({"klein": 1, "groß": 2})

                ry_open = (
                    ry_df_proj.sort_values(["recycling", "periode"])
                    .groupby("recycling")
                    .agg({"periode": "min", "cap_rank": "max", geom_col: "first"})
                    .reset_index()
                )
                ry_open = gpd.GeoDataFrame(ry_open, geometry=geom_col, crs=proj_crs)
                ry_open["kapazität"] = ry_open["cap_rank"].map({1: "klein", 2: "groß"})
            else:
                ry_open = (
                    ry_df_proj.sort_values(["recycling", "periode"])
                    .groupby("recycling")
                    .agg({"periode": "min", geom_col: "first"})
                    .reset_index()
                )
                ry_open = gpd.GeoDataFrame(ry_open, geometry=geom_col, crs=proj_crs)
                ry_open["kapazität"] = np.nan

            if capacity_lookup is not None and not capacity_lookup.empty:
                lookup = capacity_lookup[["recycling", "kapazität", "markersize"]].rename(
                    columns={"kapazität": "kap_lookup", "markersize": "ms_lookup"}
                )
                ry_open = ry_open.merge(lookup, on="recycling", how="left")
                ry_open["kapazität"] = ry_open["kapazität"].fillna(ry_open["kap_lookup"])
                ry_open["markersize"] = ry_open["kapazität"].map(size_map)
                ry_open["markersize"] = ry_open["markersize"].fillna(ry_open["ms_lookup"]).fillna(100)
                ry_open = ry_open.drop(columns=["kap_lookup", "ms_lookup"], errors="ignore")
            else:
                ry_open["markersize"] = ry_open["kapazität"].map(size_map).fillna(100)

            ry_open["opening_bucket"] = ry_open["periode"].apply(opening_bucket)
            ry_open = ry_open.loc[ry_open.intersects(visible_bbox)].copy()

            if not ry_open.empty:
                return ry_open, key

        except Exception as e:
            last_error = e
            continue

    if last_error is not None:
        raise last_error

    raise ValueError("No active/open recycling plants found in `dy_r_k` or `y_r_k`.")


def detect_region_columns(df):
    id_candidates = ["NUTS_ID", "nuts_id", "id", "region_id", "angebot"]
    name_candidates = ["NAME_LATN", "NUTS_NAME", "NAME_ENGL", "region", "name", "NAME"]

    id_col = next((c for c in id_candidates if c in df.columns), None)
    name_col = next((c for c in name_candidates if c in df.columns and c != id_col), None)
    return id_col, name_col


def build_all_nuts2_ranking_table(df_source, value_col):
    id_col, name_col = detect_region_columns(df_source)

    work = df_source.copy()
    work[value_col] = pd.to_numeric(work[value_col], errors="coerce")

    if id_col is None:
        work["_nuts2_id_fallback"] = work.index.astype(str)
        id_col = "_nuts2_id_fallback"

    keep_cols = [id_col, value_col]
    if name_col is not None:
        keep_cols.insert(1, name_col)

    ranked = work[keep_cols].copy()
    rename_map = {
        id_col: "NUTS-2",
        value_col: "Cumulative_GFRP_Waste_tonnes"
    }
    if name_col is not None:
        rename_map[name_col] = "Region"

    ranked = ranked.rename(columns=rename_map)
    if "Region" not in ranked.columns:
        ranked["Region"] = ""

    ranked = (
        ranked.groupby(["NUTS-2", "Region"], dropna=False, as_index=False)["Cumulative_GFRP_Waste_tonnes"]
        .max()
    )

    ranked["_sort_val"] = ranked["Cumulative_GFRP_Waste_tonnes"].fillna(-1)
    ranked = ranked.sort_values(
        ["_sort_val", "NUTS-2"],
        ascending=[False, True]
    ).drop(columns="_sort_val").reset_index(drop=True)

    ranked.insert(0, "Rank", np.arange(1, len(ranked) + 1))
    return ranked[["Rank", "NUTS-2", "Region", "Cumulative_GFRP_Waste_tonnes"]]


def assign_points_to_nuts2(points_gdf, polys_ref):
    id_col, name_col = detect_region_columns(polys_ref)

    tmp = polys_ref.copy()
    geom_col_poly = tmp.geometry.name

    if id_col is None:
        tmp["_nuts2_id_fallback"] = tmp.index.astype(str)
        id_col = "_nuts2_id_fallback"

    keep_cols = [id_col]
    if name_col is not None:
        keep_cols.append(name_col)

    if geom_col_poly not in tmp.columns:
        tmp[geom_col_poly] = tmp.geometry

    tmp = gpd.GeoDataFrame(tmp[keep_cols + [geom_col_poly]].copy(), geometry=geom_col_poly, crs=tmp.crs)

    pts = points_gdf.copy()
    geom_col_pts = pts.geometry.name
    if geom_col_pts not in pts.columns:
        pts[geom_col_pts] = pts.geometry

    pts = gpd.GeoDataFrame(pts, geometry=geom_col_pts, crs=pts.crs)

    if pts.crs != tmp.crs:
        pts = pts.to_crs(tmp.crs)

    rows = []
    for _, prow in pts.iterrows():
        pt = prow.geometry

        match = tmp.loc[tmp.geometry.contains(pt)]
        if match.empty:
            match = tmp.loc[tmp.geometry.intersects(pt)]
        if match.empty:
            dists = tmp.geometry.distance(pt)
            if len(dists) == 0:
                rows.append({
                    "zementwerk": prow["zementwerk"],
                    "NUTS-2": None,
                    "Region": ""
                })
                continue
            match = tmp.loc[[dists.idxmin()]]

        m = match.iloc[0]
        rows.append({
            "zementwerk": prow["zementwerk"],
            "NUTS-2": m[id_col],
            "Region": m[name_col] if name_col is not None else ""
        })

    return pd.DataFrame(rows)


def build_cement_nuts2_table(active_cem, polys_ref, total_t_col):
    empty_df = pd.DataFrame(
        columns=[
            "Rank", "NUTS-2", "Region",
            "Number_of_Cement_Plants",
            "Cumulative_GFRP_Waste_tonnes"
        ]
    )

    if active_cem.empty:
        return empty_df

    work_cem = active_cem.copy()
    geom_col_cem = work_cem.geometry.name

    if "zementwerk" not in work_cem.columns:
        work_cem["zementwerk"] = work_cem.index.astype(str)

    if geom_col_cem not in work_cem.columns:
        work_cem[geom_col_cem] = work_cem.geometry

    cement_points = gpd.GeoDataFrame(
        work_cem[["zementwerk", geom_col_cem]].copy(),
        geometry=geom_col_cem,
        crs=work_cem.crs
    )

    assignments = assign_points_to_nuts2(cement_points, polys_ref)

    id_col, name_col = detect_region_columns(polys_ref)
    source = polys_ref.copy()
    if id_col is None:
        source["_nuts2_id_fallback"] = source.index.astype(str)
        id_col = "_nuts2_id_fallback"

    keep_cols = [id_col, total_t_col]
    if name_col is not None:
        keep_cols.insert(1, name_col)

    ref = source[keep_cols].copy()
    rename_map = {id_col: "NUTS-2", total_t_col: "Cumulative_GFRP_Waste_tonnes"}
    if name_col is not None:
        rename_map[name_col] = "Region"
    ref = ref.rename(columns=rename_map)

    if "Region" not in ref.columns:
        ref["Region"] = ""

    ref = (
        ref.groupby(["NUTS-2", "Region"], dropna=False, as_index=False)["Cumulative_GFRP_Waste_tonnes"]
        .max()
    )

    merged = assignments.merge(ref, on=["NUTS-2", "Region"], how="left")
    merged["Cumulative_GFRP_Waste_tonnes"] = merged["Cumulative_GFRP_Waste_tonnes"].fillna(0)

    out = (
        merged.groupby(["NUTS-2", "Region"], dropna=False, as_index=False)
        .agg(
            Number_of_Cement_Plants=("zementwerk", "nunique"),
            Cumulative_GFRP_Waste_tonnes=("Cumulative_GFRP_Waste_tonnes", "max")
        )
        .sort_values(
            ["Cumulative_GFRP_Waste_tonnes", "Number_of_Cement_Plants"],
            ascending=[False, False]
        )
        .reset_index(drop=True)
    )

    out.insert(0, "Rank", np.arange(1, len(out) + 1))
    return out[["Rank", "NUTS-2", "Region", "Number_of_Cement_Plants", "Cumulative_GFRP_Waste_tonnes"]]


def plot_nuts_boundaries(ax, boundaries):
    if boundaries is not None and len(boundaries) > 0:
        boundaries.plot(
            ax=ax,
            color="#111111",
            linewidth=0.55,
            alpha=0.90,
            zorder=0.9
        )


def draw_side_panel(ax_side, class_palette, class_title):
    ax_side.axis("off")

    legend_handles = [
        Patch(facecolor=class_palette[label], edgecolor="black", label=label)
        for label in waste_class_order
    ]

    leg = ax_side.legend(
        handles=legend_handles,
        loc="upper left",
        bbox_to_anchor=(0.00, 1.00),
        borderaxespad=0.0,
        frameon=True,
        title=class_title,
        fontsize=8.8,
        title_fontsize=10.0,
        labelspacing=0.42,
        handlelength=1.3,
        handletextpad=0.6
    )
    ax_side.add_artist(leg)

    ax_side.text(
        0.00, 0.42,
        "Ranking tables are exported\nseparately to the output folder.",
        transform=ax_side.transAxes,
        ha="left",
        va="top",
        fontsize=9.5
    )


def add_bottom_legends(fig, ar_color, opening_color_map, include_ar=True):
    network_handles = []

    if include_ar:
        network_handles.append(
            Line2D(
                [0], [0],
                color=ar_color,
                lw=2.6,
                label="Supply -> recycling GFRP-Waste flow"
            )
        )

    network_handles.extend([
        Line2D(
            [0], [0],
            color=RZ_COLOR,
            lw=2.6,
            label="Recycling -> cement flow"
        ),
        Line2D(
            [0], [0],
            marker="^",
            color="w",
            markerfacecolor=CEMENT_FACE,
            markeredgecolor="black",
            markersize=10,
            linestyle="None",
            label="Cement plant with incoming flow"
        )
    ])

    opening_handles = [
        Line2D(
            [0], [0],
            marker="o",
            color="w",
            markerfacecolor=color,
            markeredgecolor="black",
            markersize=9.5,
            linestyle="None",
            label=label
        )
        for label, color in opening_color_map.items()
    ]

    fig.legend(
        handles=network_handles,
        loc="lower center",
        bbox_to_anchor=(0.43, 0.100),
        ncol=max(2, len(network_handles)),
        frameon=True,
        title="Network overlays",
        fontsize=9.5,
        title_fontsize=10.5
    )

    fig.legend(
        handles=opening_handles,
        loc="lower center",
        bbox_to_anchor=(0.43, 0.030),
        ncol=5,
        frameon=True,
        title="Recycling plant opening period (circle size = plant capacity)",
        fontsize=9.5,
        title_fontsize=10.5
    )


def plot_network_overlays(ax, ar_color, opening_color_map, include_ar=True, cement_edgecolor="black"):
    if include_ar:
        add_weighted_flow_lines(
            ax=ax,
            gdf=flows_ar_agg_plot,
            value_col="tonnes_plot",
            color=ar_color,
            reference_values=flow_reference_t,
            alpha=0.48,
            zorder=1.2,
            lw_min=FLOW_LW_MIN,
            lw_max=FLOW_LW_MAX
        )

    add_weighted_flow_lines(
        ax=ax,
        gdf=flows_rz_agg_plot,
        value_col="tonnes_plot",
        color=RZ_COLOR,
        reference_values=flow_reference_t,
        alpha=0.60,
        zorder=1.3,
        lw_min=FLOW_LW_MIN,
        lw_max=FLOW_LW_MAX
    )

    if not active_cem.empty:
        active_cem.plot(
            ax=ax,
            marker="^",
            markersize=CEMENT_MARKERSIZE,
            color=CEMENT_FACE,
            edgecolor=cement_edgecolor,
            linewidth=CEMENT_EDGEWIDTH,
            zorder=4
        )

    for bucket, color in opening_color_map.items():
        sub = ry_open.loc[ry_open["opening_bucket"] == bucket]
        if not sub.empty:
            sub.plot(
                ax=ax,
                marker="o",
                markersize=sub["markersize"],
                color=color,
                edgecolor="black",
                linewidth=0.95,
                zorder=5
            )


def create_map_figure(
    include_ar,
    ar_color,
    opening_color_map,
    class_palette,
    figure_title,
    export_filename,
    cement_edgecolor="black"
):
    fig = plt.figure(figsize=FIGSIZE)
    gs = fig.add_gridspec(1, 2, width_ratios=[5.8, 0.9], wspace=0.02)

    ax_map = fig.add_subplot(gs[0, 0])
    ax_side = fig.add_subplot(gs[0, 1])

    ax_map.set_aspect("equal")

    map_colors = polys_plot["waste_class"].map(class_palette).fillna("#d9d9d9")
    polys_plot.plot(
        ax=ax_map,
        color=map_colors,
        edgecolor="none",
        linewidth=0,
        zorder=0.1
    )

    plot_nuts_boundaries(ax_map, polys_boundary_plot)

    plot_network_overlays(
        ax=ax_map,
        ar_color=ar_color,
        opening_color_map=opening_color_map,
        include_ar=include_ar,
        cement_edgecolor=cement_edgecolor
    )

    draw_side_panel(
        ax_side=ax_side,
        class_palette=class_palette,
        class_title="Cumulative GFRP-Waste classes (tonnes)"
    )

    add_bottom_legends(
        fig=fig,
        ar_color=ar_color,
        opening_color_map=opening_color_map,
        include_ar=include_ar
    )

    ax_map.set_xlim(*xlim)
    ax_map.set_ylim(*ylim)
    ax_map.axis("off")

    fig.suptitle(
        f"{NOTEBOOK_NAME} | {figure_title}",
        fontsize=15,
        y=0.975
    )

    fig.subplots_adjust(bottom=0.20, top=0.93)
    out_path = save_figure(fig, export_dir / export_filename, dpi=SAVE_DPI)

    if SHOW_PLOTS:
        plt.show()

    plt.close(fig)
    del ax_map, ax_side, fig
    gc.collect()

    return out_path
# ============================================================
# PATCH-ZELLE: kleine Recyclingwerke robust sichtbar machen
# - Binärvariablen nur als aktiv werten, wenn > 0.5
# - "klein" und "groß" getrennt halten
# - kleine Werke über große plotten
# - Diagnose für identische Centroids / doppelte Kapazitäten
# ============================================================

BINARY_ACTIVE_TOL = 0.5  # nur für Binärvariablen wie y_r_k / dy_r_k


def build_recycling_capacity_lookup(dfs_geom_nz, exclude_ids, binary_tol=BINARY_ACTIVE_TOL):
    """
    Baut ein Lookup für Recyclingstandorte und ihre Kapazität.
    Anders als vorher wird NICHT auf Standortebene mit max() verdichtet,
    sondern jede aktive Kapazität pro Standort separat behalten.
    """
    candidate_keys = [k for k in ["dy_r_k", "y_r_k"] if k in dfs_geom_nz]

    for key in candidate_keys:
        df = dfs_geom_nz[key].copy()

        if ("recycling" not in df.columns) or ("kapazität" not in df.columns):
            continue

        df["value"] = pd.to_numeric(df["value"], errors="coerce").fillna(0.0)
        df["recycling"] = df["recycling"].astype(str).str.strip()

        df = df.loc[
            (df["value"] > binary_tol) &
            (~df["recycling"].isin(exclude_ids))
        ].copy()

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


def prepare_recycling_openings(
    dfs_geom_nz,
    valid_periods,
    proj_crs,
    exclude_ids,
    visible_bbox,
    capacity_lookup,
    binary_tol=BINARY_ACTIVE_TOL
):
    """
    Ermittelt die sichtbaren Recyclingwerke mit Öffnungsperiode und Kapazität.
    WICHTIG:
    - Filtert Binärvariablen mit > 0.5
    - Gruppiert bei vorhandener Kapazität nach ['recycling', 'kapazität']
      statt nur nach ['recycling']
    """
    source_keys = [k for k in ["dy_r_k", "y_r_k"] if k in dfs_geom_nz]
    if not source_keys:
        raise KeyError("Neither 'dy_r_k' nor 'y_r_k' exists in dfs_geom_nz.")

    last_error = None

    for key in source_keys:
        try:
            ry_df = dfs_geom_nz[key].copy()
            ry_df["periode"] = pd.to_numeric(ry_df["periode"], errors="coerce")
            ry_df["value"] = pd.to_numeric(ry_df["value"], errors="coerce").fillna(0.0)

            ry_df = ry_df.loc[
                ry_df["periode"].isin(valid_periods) &
                (ry_df["value"] > binary_tol)
            ].copy()

            if ry_df.empty:
                continue

            ry_df["recycling"] = ry_df["recycling"].astype(str).str.strip()
            ry_df = ry_df.loc[~ry_df["recycling"].isin(exclude_ids)].copy()

            if ry_df.empty:
                continue

            ry_df_proj = ry_df.to_crs(proj_crs)
            geom_col = ry_df_proj.geometry.name

            if geom_col not in ry_df_proj.columns:
                ry_df_proj[geom_col] = ry_df_proj.geometry

            ry_df_proj = ry_df_proj.loc[
                ry_df_proj.geometry.notna() & ~ry_df_proj.geometry.is_empty
            ].copy()

            if ry_df_proj.empty:
                continue

            if "kapazität" in ry_df_proj.columns:
                ry_df_proj["kapazität"] = ry_df_proj["kapazität"].apply(normalize_capacity_label)
                ry_df_proj = ry_df_proj.dropna(subset=["kapazität"]).copy()

                if ry_df_proj.empty:
                    continue

                # getrennt nach Standort UND Kapazität
                ry_open = (
                    ry_df_proj
                    .sort_values(["recycling", "kapazität", "periode"])
                    .groupby(["recycling", "kapazität"], as_index=False)
                    .agg({"periode": "min", geom_col: "first"})
                )
                ry_open = gpd.GeoDataFrame(ry_open, geometry=geom_col, crs=proj_crs)

            else:
                ry_open = (
                    ry_df_proj
                    .sort_values(["recycling", "periode"])
                    .groupby(["recycling"], as_index=False)
                    .agg({"periode": "min", geom_col: "first"})
                )
                ry_open = gpd.GeoDataFrame(ry_open, geometry=geom_col, crs=proj_crs)
                ry_open["kapazität"] = np.nan

            # markersize aus Lookup ergänzen
            if capacity_lookup is not None and not capacity_lookup.empty:
                if "kapazität" in ry_open.columns and "kapazität" in capacity_lookup.columns:
                    merge_cols = ["recycling", "kapazität"]
                else:
                    merge_cols = ["recycling"]

                lookup_cols = merge_cols + ["markersize"]
                ry_open = ry_open.merge(
                    capacity_lookup[lookup_cols].drop_duplicates(),
                    on=merge_cols,
                    how="left"
                )

            ms = ry_open["kapazität"].map(size_map) if "kapazität" in ry_open.columns else pd.Series(index=ry_open.index, dtype=float)
            if "markersize" in ry_open.columns:
                ms = ms.fillna(pd.to_numeric(ry_open["markersize"], errors="coerce"))
            ry_open["markersize"] = ms.fillna(100)

            ry_open["opening_bucket"] = ry_open["periode"].apply(opening_bucket)

            ry_open = ry_open.loc[
                ry_open.geometry.notna() &
                ~ry_open.geometry.is_empty &
                ry_open.intersects(visible_bbox)
            ].copy()

            if not ry_open.empty:
                return ry_open, key

        except Exception as e:
            last_error = e
            continue

    if last_error is not None:
        raise last_error

    raise ValueError("No active/open recycling plants found in `dy_r_k` or `y_r_k`.")


def report_recycling_plot_diagnostics(ry_open):
    """
    Kleine Diagnose:
    - Wie viele kleine/große Marker sind im Plot?
    - Gibt es Standorte mit mehr als einer aktiven Kapazität?
    - Gibt es identische Centroids, die sich überdecken könnten?
    """
    print("\n--- Recycling-Plot-Diagnose ---")

    if ry_open is None or ry_open.empty:
        print("Keine Recyclingwerke im Plot vorhanden.")
        return

    cap_counts = ry_open["kapazität"].fillna("unknown").value_counts(dropna=False)
    print("Marker im Plot nach Kapazität:")
    print(cap_counts.to_string())

    multi_cap = (
        ry_open.groupby("recycling")["kapazität"]
        .nunique(dropna=True)
        .reset_index(name="n_active_capacities")
    )
    multi_cap = multi_cap.loc[multi_cap["n_active_capacities"] > 1].copy()

    if not multi_cap.empty:
        print("\nStandorte mit mehr als einer aktiven Kapazität im Plot:")
        print(multi_cap.to_string(index=False))
    else:
        print("\nKeine Standorte mit gleichzeitig aktiver kleiner und großer Kapazität im Plot.")

    tmp = ry_open.copy()
    tmp["_geom_key"] = tmp.geometry.apply(lambda g: g.wkb_hex if g is not None else None)

    same_point = (
        tmp.groupby("_geom_key", as_index=False)
        .agg(
            n_markers=("recycling", "size"),
            recycling_ids=("recycling", lambda s: ", ".join(sorted(map(str, pd.unique(s))))),
            capacities=("kapazität", lambda s: ", ".join(sorted(map(str, pd.unique(s.fillna('unknown'))))))
        )
    )
    same_point = same_point.loc[same_point["n_markers"] > 1].copy()

    if not same_point.empty:
        print("\nMarker auf identischem Centroid (potenzielle Überdeckung):")
        print(same_point.head(15).to_string(index=False))
        if len(same_point) > 15:
            print(f"... weitere {len(same_point) - 15} identische Centroids")
    else:
        print("\nKeine identischen Centroids unter den geplotteten Recyclingmarkern gefunden.")


def plot_recycling_opening_layers(ax, ry_open, opening_color_map):
    """
    Zeichnet Recyclingwerke in Ebenen:
    1) große Werke
    2) unbekannte / sonstige
    3) kleine Werke ganz oben

    Für kleine Werke wird zusätzlich ein weißer Halo gezeichnet,
    damit sie auf gleichen Centroids besser sichtbar bleiben.
    """
    if ry_open is None or ry_open.empty:
        return

    for bucket, color in opening_color_map.items():
        sub = ry_open.loc[ry_open["opening_bucket"] == bucket].copy()
        if sub.empty:
            continue

        sub_large = sub.loc[sub["kapazität"] == "groß"].copy()
        if not sub_large.empty:
            sub_large.plot(
                ax=ax,
                marker="o",
                markersize=sub_large["markersize"],
                color=color,
                edgecolor="black",
                linewidth=0.95,
                zorder=5.0
            )

        sub_other = sub.loc[
            sub["kapazität"].isna() | (~sub["kapazität"].isin(["klein", "groß"]))
        ].copy()
        if not sub_other.empty:
            sub_other.plot(
                ax=ax,
                marker="o",
                markersize=sub_other["markersize"],
                color=color,
                edgecolor="black",
                linewidth=0.95,
                zorder=5.5
            )

        sub_small = sub.loc[sub["kapazität"] == "klein"].copy()
        if not sub_small.empty:
            # weißer Halo, damit kleine Werke auf großen Werken sichtbar bleiben
            sub_small.plot(
                ax=ax,
                marker="o",
                markersize=sub_small["markersize"] * 1.35,
                color="white",
                edgecolor="white",
                linewidth=0.0,
                zorder=5.8
            )
            sub_small.plot(
                ax=ax,
                marker="o",
                markersize=sub_small["markersize"],
                color=color,
                edgecolor="black",
                linewidth=1.10,
                zorder=6.0
            )


def plot_network_overlays(ax, ar_color, opening_color_map, include_ar=True, cement_edgecolor="black"):
    """
    Überschreibt die alte Funktion und nutzt jetzt die Layer-Logik
    für kleine/große Recyclingwerke.
    """
    if include_ar:
        add_weighted_flow_lines(
            ax=ax,
            gdf=flows_ar_agg_plot,
            value_col="tonnes_plot",
            color=ar_color,
            reference_values=flow_reference_t,
            alpha=0.48,
            zorder=1.2,
            lw_min=FLOW_LW_MIN,
            lw_max=FLOW_LW_MAX
        )

    add_weighted_flow_lines(
        ax=ax,
        gdf=flows_rz_agg_plot,
        value_col="tonnes_plot",
        color=RZ_COLOR,
        reference_values=flow_reference_t,
        alpha=0.60,
        zorder=1.3,
        lw_min=FLOW_LW_MIN,
        lw_max=FLOW_LW_MAX
    )

    if not active_cem.empty:
        active_cem.plot(
            ax=ax,
            marker="^",
            markersize=CEMENT_MARKERSIZE,
            color=CEMENT_FACE,
            edgecolor=cement_edgecolor,
            linewidth=CEMENT_EDGEWIDTH,
            zorder=4
        )

    plot_recycling_opening_layers(ax, ry_open, opening_color_map)


# ------------------------------------------------------------
# Neu aufbauen: capacity lookup + sichtbare Recyclingwerke
# ------------------------------------------------------------
capacity_lookup, capacity_source_key = build_recycling_capacity_lookup(
    dfs_geom_nz=dfs_geom_nz,
    exclude_ids=exclude_ids,
    binary_tol=BINARY_ACTIVE_TOL
)

ry_open, opening_source_key = prepare_recycling_openings(
    dfs_geom_nz=dfs_geom_nz,
    valid_periods=valid_periods,
    proj_crs=proj_crs,
    exclude_ids=exclude_ids,
    visible_bbox=visible_bbox,
    capacity_lookup=capacity_lookup,
    binary_tol=BINARY_ACTIVE_TOL
)

report_recycling_plot_diagnostics(ry_open)

print("\nPatch aktiv.")
print(f"Capacity lookup source used: {capacity_source_key}")
print(f"Opening-period source used: {opening_source_key}")
print(f"Binär-Schwelle für aktive Werke: {BINARY_ACTIVE_TOL}")
print(f"Recyclingwerke im Plot: {len(ry_open):,}")
print(ry_open[['recycling', 'kapazität', 'periode', 'opening_bucket', 'markersize']].head(15).to_string(index=False))
# ------------------------------------------------------------
# 4) Notebook name + export folder
# ------------------------------------------------------------
NOTEBOOK_NAME = OUTPUT_BASE.name
NOTEBOOK_FOLDER_NAME = sanitize_filename(NOTEBOOK_NAME)

requested_export_dir = OUTPUT_BASE / "plots"
export_dir = requested_export_dir
export_dir.mkdir(parents=True, exist_ok=True)

# ------------------------------------------------------------
# 5) Prepare polygon data — exact source: polys['sum_all_years']
# ------------------------------------------------------------
polys_all = polys.copy()

if SOURCE_WASTE_COL not in polys_all.columns:
    raise KeyError(
        f"'{SOURCE_WASTE_COL}' not found in `polys`. "
        f"Please run the cell that computes polys['{SOURCE_WASTE_COL}'] first."
    )

source_raw_kg = pd.to_numeric(polys_all[SOURCE_WASTE_COL], errors="coerce")
source_used = SOURCE_WASTE_COL

polys_all["source_waste_kg"] = source_raw_kg
polys_all["Cumulative_GFRP_Waste_tonnes"] = source_raw_kg / 1000.0

# Only true no-data or negligible data are grey
polys_all["is_no_data"] = source_raw_kg.isna() | (polys_all["Cumulative_GFRP_Waste_tonnes"] < 1)

polys_plot = polys_all.loc[polys_all.intersects(visible_bbox)].copy()
polys_plot["waste_class"] = np.where(
    polys_plot["is_no_data"],
    NO_DATA_LABEL,
    polys_plot["Cumulative_GFRP_Waste_tonnes"].apply(classify_waste_tonnes)
)

# Boundary only once
polys_boundary_plot = polys_plot.boundary

# Full ranking for ALL assessed NUTS-2 regions
all_assessment_nuts2_df = build_all_nuts2_ranking_table(
    polys_all,
    "Cumulative_GFRP_Waste_tonnes"
)

# ------------------------------------------------------------
# 6) Prepare flow data
# ------------------------------------------------------------
flows_rz_all = make_projected_lines(
    dfs_geom_nz["x_rz"],
    start_geom_col="geom_recycling",
    end_geom_col="geom_zement",
    target_crs=proj_crs,
    keep_projected_points="both"
)

flows_ar_all = make_projected_lines(
    dfs_geom_nz["x_ar_g"],
    start_geom_col="geom_offer",
    end_geom_col="geom_recycling",
    target_crs=proj_crs,
    keep_projected_points="both"
)

# IDs vereinheitlichen, damit Plot-/Merge-Logik robust bleibt
for _col in ["recycling", "zementwerk"]:
    if _col in flows_rz_all.columns:
        flows_rz_all[_col] = flows_rz_all[_col].astype(str).str.strip()

for _col in ["angebot", "recycling"]:
    if _col in flows_ar_all.columns:
        flows_ar_all[_col] = flows_ar_all[_col].astype(str).str.strip()

# Remove explicitly excluded logistics IDs only from logistics layers
flows_rz_all = flows_rz_all.loc[
    ~flows_rz_all["recycling"].astype(str).isin(exclude_ids) &
    ~flows_rz_all["zementwerk"].astype(str).isin(exclude_ids)
].copy()

flows_ar_all = flows_ar_all.loc[
    ~flows_ar_all["angebot"].astype(str).isin(exclude_ids) &
    ~flows_ar_all["recycling"].astype(str).isin(exclude_ids)
].copy()

# Plot nur Routen, deren Start- UND Endpunkt im sichtbaren Kartenfenster liegen.
# Dadurch erscheinen keine Linien mehr "aus dem Nichts" am Kartenrand.
start_points_ar = gpd.GeoSeries(flows_ar_all["_start_point_proj"], crs=proj_crs)
end_points_ar = gpd.GeoSeries(flows_ar_all["_end_point_proj"], crs=proj_crs)
flows_ar_plot_base = flows_ar_all.loc[
    start_points_ar.intersects(visible_bbox) &
    end_points_ar.intersects(visible_bbox)
].copy()

start_points_rz = gpd.GeoSeries(flows_rz_all["_start_point_proj"], crs=proj_crs)
end_points_rz = gpd.GeoSeries(flows_rz_all["_end_point_proj"], crs=proj_crs)
flows_rz_plot_base = flows_rz_all.loc[
    start_points_rz.intersects(visible_bbox) &
    end_points_rz.intersects(visible_bbox)
].copy()

# A->R full model aggregation (for correct R->Z balancing)
flows_ar_agg_full = aggregate_route_flows(
    flows_ar_all,
    ["angebot", "recycling"],
    valid_periods
)
flows_ar_agg_full["tonnes_model"] = pd.to_numeric(
    flows_ar_agg_full.get("value", 0), errors="coerce"
).fillna(0) / 1000.0

# A->R visible plot aggregation
flows_ar_agg_plot = aggregate_route_flows(
    flows_ar_plot_base,
    ["angebot", "recycling"],
    valid_periods
)
flows_ar_agg_plot = flows_ar_agg_plot.loc[flows_ar_agg_plot.intersects(visible_bbox)].copy()
flows_ar_agg_plot["tonnes_plot"] = pd.to_numeric(
    flows_ar_agg_plot.get("value", 0), errors="coerce"
).fillna(0) / 1000.0

# R->Z full model aggregation
flows_rz_agg_full = aggregate_route_flows(
    flows_rz_all,
    ["recycling", "zementwerk"],
    valid_periods
)
flows_rz_agg_full["tonnes_model"] = pd.to_numeric(
    flows_rz_agg_full.get("value", 0), errors="coerce"
).fillna(0) / 1000.0

# Correct scaling of R->Z to total incoming A->R from full model
ar_inbound_by_recycling = (
    flows_ar_agg_full.groupby("recycling", as_index=False)["tonnes_model"]
    .sum()
    .rename(columns={"tonnes_model": "ar_inbound_total_t"})
)

rz_outbound_by_recycling = (
    flows_rz_agg_full.groupby("recycling", as_index=False)["tonnes_model"]
    .sum()
    .rename(columns={"tonnes_model": "rz_outbound_total_t"})
)

flows_rz_agg_full = flows_rz_agg_full.merge(ar_inbound_by_recycling, on="recycling", how="left")
flows_rz_agg_full = flows_rz_agg_full.merge(rz_outbound_by_recycling, on="recycling", how="left")

flows_rz_agg_full["rz_scale_factor"] = np.where(
    flows_rz_agg_full["ar_inbound_total_t"].notna() & (flows_rz_agg_full["rz_outbound_total_t"] > 0),
    flows_rz_agg_full["ar_inbound_total_t"] / flows_rz_agg_full["rz_outbound_total_t"],
    1.0
)
flows_rz_agg_full["tonnes_plot"] = flows_rz_agg_full["tonnes_model"] * flows_rz_agg_full["rz_scale_factor"]

# Visible R->Z subset for plotting (nur Routen mit beiden sichtbaren Endpunkten)
visible_rz_routes = aggregate_route_flows(
    flows_rz_plot_base,
    ["recycling", "zementwerk"],
    valid_periods
)

if visible_rz_routes.empty:
    flows_rz_agg_plot = gpd.GeoDataFrame(
        columns=list(flows_rz_agg_full.columns),
        geometry="geometry",
        crs=flows_rz_agg_full.crs
    )
else:
    flows_rz_agg_plot = flows_rz_agg_full.merge(
        visible_rz_routes[["recycling", "zementwerk"]].drop_duplicates(),
        on=["recycling", "zementwerk"],
        how="inner"
    )
    flows_rz_agg_plot = gpd.GeoDataFrame(
        flows_rz_agg_plot,
        geometry="geometry",
        crs=flows_rz_agg_full.crs
    )

flow_reference_t = pd.concat(
    [flows_ar_agg_plot["tonnes_plot"], flows_rz_agg_plot["tonnes_plot"]],
    ignore_index=True
)

# ------------------------------------------------------------
# 7) Prepare recycling plants
# ------------------------------------------------------------
capacity_lookup, capacity_source_key = build_recycling_capacity_lookup(
    dfs_geom_nz=dfs_geom_nz,
    exclude_ids=exclude_ids
)

ry_open, opening_source_key = prepare_recycling_openings(
    dfs_geom_nz=dfs_geom_nz,
    valid_periods=valid_periods,
    proj_crs=proj_crs,
    exclude_ids=exclude_ids,
    visible_bbox=visible_bbox,
    capacity_lookup=capacity_lookup
)

# ------------------------------------------------------------
# 8) Prepare cement plants
# ------------------------------------------------------------
if cem_gdf_base.crs is None:
    cem_gdf_proj = cem_gdf_base.set_crs(proj_crs, allow_override=True)
elif cem_gdf_base.crs != proj_crs:
    cem_gdf_proj = cem_gdf_base.to_crs(proj_crs)
else:
    cem_gdf_proj = cem_gdf_base.copy()

# Explizite ID-Spalte anlegen; nicht nur über den Index matchen.
cem_gdf_proj["zementwerk"] = cem_gdf_proj.index.astype(str).str.strip()
geom_col_cem = cem_gdf_proj.geometry.name

cem_gdf_proj = cem_gdf_proj.loc[
    ~cem_gdf_proj["zementwerk"].isin(exclude_ids)
].copy()

active_z_ids = (
    flows_rz_agg_plot["zementwerk"].astype(str).str.strip().unique()
    if not flows_rz_agg_plot.empty else []
)

active_cem = cem_gdf_proj.loc[
    cem_gdf_proj["zementwerk"].isin(active_z_ids)
].copy()

if geom_col_cem not in active_cem.columns:
    active_cem[geom_col_cem] = active_cem.geometry

active_cem = gpd.GeoDataFrame(active_cem, geometry=geom_col_cem, crs=cem_gdf_proj.crs)
active_cem = active_cem.loc[active_cem.intersects(visible_bbox)].copy()

missing_active_z_ids = sorted(set(map(str, active_z_ids)) - set(active_cem["zementwerk"].astype(str)))
if missing_active_z_ids:
    print(f"⚠️ Aktive Zementwerke ohne sichtbaren Plotpunkt: {missing_active_z_ids}")

cement_nuts_df = build_cement_nuts2_table(
    active_cem=active_cem,
    polys_ref=polys_plot,
    total_t_col="Cumulative_GFRP_Waste_tonnes"
)

# ------------------------------------------------------------
# 9) Export tables
# ------------------------------------------------------------
nuts2_ranking_all_csv_path = export_dir / "all_assessment_nuts2_ranked_by_cumulative_gfrp_waste.csv"
cement_nuts2_csv_path = export_dir / "cement_plant_nuts2_cumulative_gfrp_waste.csv"

all_assessment_nuts2_df.to_csv(nuts2_ranking_all_csv_path, index=False, encoding="utf-8-sig")
cement_nuts_df.to_csv(cement_nuts2_csv_path, index=False, encoding="utf-8-sig")

# ------------------------------------------------------------
# 10) Create and export all 4 plots
# ------------------------------------------------------------
exported_files = []

exported_files.append(
    create_map_figure(
        include_ar=True,
        ar_color=PLOT1_AR_COLOR,
        opening_color_map=plot1_opening_color_map,
        class_palette=blue_class_palette,
        figure_title=(
            f"Cumulative GFRP-Waste network {horizon_label}: waste generation, recycling and cement logistics\n"
            f"Blue class background | with supply -> recycling transport | circle colour = opening period | circle size = plant capacity"
        ),
        export_filename="01_blue_classes_with_A_to_R.jpg",
        cement_edgecolor=CEMENT_EDGE_BLUE
    )
)

exported_files.append(
    create_map_figure(
        include_ar=True,
        ar_color=PLOT2_AR_COLOR,
        opening_color_map=plot2_opening_color_map,
        class_palette=hotspot_palette,
        figure_title=(
            f"Cumulative GFRP-Waste hotspot map {horizon_label}: waste generation, recycling and cement logistics\n"
            f"Classified hotspot background | with supply -> recycling transport | circle colour = opening period | circle size = plant capacity"
        ),
        export_filename="02_hotspot_with_A_to_R.jpg",
        cement_edgecolor=CEMENT_EDGE_HOTSPOT
    )
)

exported_files.append(
    create_map_figure(
        include_ar=False,
        ar_color=PLOT1_AR_COLOR,
        opening_color_map=plot1_opening_color_map,
        class_palette=blue_class_palette,
        figure_title=(
            f"Cumulative GFRP-Waste network {horizon_label}: recycling and cement logistics\n"
            f"Blue class background | without supply -> recycling transport | circle colour = opening period | circle size = plant capacity"
        ),
        export_filename="03_blue_classes_R_to_Z_only.jpg",
        cement_edgecolor=CEMENT_EDGE_BLUE
    )
)

exported_files.append(
    create_map_figure(
        include_ar=False,
        ar_color=PLOT2_AR_COLOR,
        opening_color_map=plot2_opening_color_map,
        class_palette=hotspot_palette,
        figure_title=(
            f"Cumulative GFRP-Waste hotspot map {horizon_label}: recycling and cement logistics\n"
            f"Classified hotspot background | without supply -> recycling transport | circle colour = opening period | circle size = plant capacity"
        ),
        export_filename="04_hotspot_R_to_Z_only.jpg",
        cement_edgecolor=CEMENT_EDGE_HOTSPOT
    )
)

# ------------------------------------------------------------
# 11) Summary
# ------------------------------------------------------------
summary_opening = (
    ry_open.groupby("opening_bucket")["recycling"]
    .nunique()
    .reindex(list(plot2_opening_color_map.keys()), fill_value=0)
    .rename("Number_of_recycling_plants")
    .to_frame()
)

capacity_counts = (
    ry_open["kapazität"]
    .fillna("unknown")
    .value_counts(dropna=False)
)

check_balance = (
    flows_rz_agg_full.groupby("recycling", as_index=False)["tonnes_plot"]
    .sum()
    .rename(columns={"tonnes_plot": "rz_plotted_total_t"})
    .merge(ar_inbound_by_recycling, on="recycling", how="outer")
)

assessed_nuts2_count = len(all_assessment_nuts2_df)
positive_nuts2_count = int(
    pd.to_numeric(all_assessment_nuts2_df["Cumulative_GFRP_Waste_tonnes"], errors="coerce")
    .fillna(0)
    .ge(1)
    .sum()
)

print(f"\nNotebook name used: {NOTEBOOK_NAME}")
print(f"Export directory: {export_dir}")
print(f"Source waste column used for NUTS-2 background and tables: {source_used}")
print(f"Full NUTS-2 ranking CSV export: {nuts2_ranking_all_csv_path}")
print(f"Cement NUTS-2 CSV export: {cement_nuts2_csv_path}")
print(f"Opening-period source used: {opening_source_key}")
print(f"Capacity lookup source used: {capacity_source_key}")
print("NUTS-2 ranking and cement tables were removed from the map layout and exported separately as CSV files.")
print(f"Routen <= {FLOW_PLOT_TOL_KG:g} kg wurden als numerisches Solver-Rauschen aus dem Plot entfernt.")
print("A->R und R->Z werden nur gezeichnet, wenn Start- und Endpunkt im sichtbaren Kartenausschnitt liegen.")
print("R->Z widths were scaled so that each recycling plant's total outgoing width matches its total incoming A->R flow from the full model.")

print("\nGFRP-Waste class scheme:")
for label in waste_class_order:
    print(f" - {label}")

print("\nRecycling plants by opening period:")
print(summary_opening.to_string())

print("\nRecycling plant capacities in plot:")
print(capacity_counts.to_string())

print(f"\nAssessed NUTS-2 rows in full ranking export: {assessed_nuts2_count:,}")
print(f"Assessed NUTS-2 with positive cumulative GFRP-Waste: {positive_nuts2_count:,}")
print(f"Cement-plant NUTS-2 rows: {len(cement_nuts_df):,}")
print(f"Displayed supply -> recycling routes: {len(flows_ar_agg_plot):,}")
print(f"Displayed recycling -> cement routes: {len(flows_rz_agg_plot):,}")
print(f"Recycling plants shown: {ry_open['recycling'].nunique():,}")
print(f"Cement plants shown: {len(active_cem):,}")

print("\nExported JPEG files:")
for p in exported_files:
    print(f" - {p}")

print("\nSanity check of plotted balance (per recycling plant: incoming full-model A->R vs plotted outgoing R->Z):")
print(check_balance.head(10).to_string(index=False))


# In[ ]:


import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

# ---------------------------------------------------------
# Manuelle Grenzen für die Vergleichbarkeit setzen
# ---------------------------------------------------------
# Tipp: Nehmen Sie hier das Minimum und Maximum aller Ihrer Szenarien.
# Da es eine Log-Skala ist, darf vmin NICHT 0 sein.
VMIN_FIXED = 1000       # Untergrenze der Farbskala (z.B. 1 Tonne)
VMAX_FIXED = 20000000 # Obergrenze der Farbskala (z.B. 500 Mio kg)

year = "sum_all_years"

fig, ax = plt.subplots(figsize=(8, 8), subplot_kw={"aspect": "equal"})

# 1) Null-Mengen hellgrau zeichnen (bleibt unverändert)
polys[polys[year] == 0].plot(
    ax=ax, color="lightgrey", edgecolor="white", linewidth=0.2)

# 2) Nicht-Null-Regionen mit FIXIERTER Log-Skala einfärben
pos = polys[polys[year] > 0]

# Nur plotten, wenn Daten vorhanden sind
if not pos.empty:
    pos.plot(
        column=year,
        ax=ax,
        cmap="YlGnBu",
        # HIER IST DIE ÄNDERUNG: Feste Werte statt pos.min()/max()
        norm=LogNorm(vmin=VMIN_FIXED, vmax=VMAX_FIXED),
        legend=True,
        linewidth=0.2,
        edgecolor="grey",
        # Optional: Legenden-Label anpassen
        legend_kwds={'label': "Abfallaufkommen (kg) - Einheitliche Skala"}
    )

# ► Kontinentale EU-Grenzen (EPSG 3035)
ax.set_xlim(2.5e6, 6.1e6)
ax.set_ylim(1.3e6, 5.5e6)

ax.set_title(f"Abfallaufkommen (Vergleichbar): 2025-2050")
ax.axis("off")
plt.tight_layout()
plt.show()


# ## Kartendarstellungen der Recyclingwerke

# In[ ]:


import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import pandas as pd

# ─────────────────────────────────────────
# 1. Manuelle Skalierung für Vergleichbarkeit
# ─────────────────────────────────────────
# Tragen Sie hier Werte ein, die für ALLE Ihre Szenarien sinnvoll sind.
# Tipp: Nehmen Sie das Maximum aus dem "Wind + Auto"-Szenario als VMAX.
VMIN_FIXED = 1000       # Untergrenze (z.B. 1 Tonne), darf nicht 0 sein!
VMAX_FIXED = 20000000 # Obergrenze (z.B. 500 Mio kg)

print(f"Farbskala fixiert auf: Min={VMIN_FIXED:,.0f}, Max={VMAX_FIXED:,.0f}")

# ─────────────────────────────────────────
# Basisdaten
# ─────────────────────────────────────────
proj_crs = polys.crs

# Recycling-Werke ins projizierte CRS
ry_df = dfs_geom_nz["y_r_k"].to_crs(proj_crs)

# Jahre definieren (5-Jahres-Schritte)
years = [i for i in range(2025, 2051, 5)]
xlim  = (2.5e6, 6.1e6)
ylim  = (1.3e6, 5.5e6)

# ─────────────────────────────────────────
# Schleife: je Jahr eine Karte anzeigen
# ─────────────────────────────────────────
for yr in years:
    col = str(yr)

    # Sicherheitscheck, falls Spalte nicht existiert
    if col not in polys.columns:
        continue

    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw={"aspect": "equal"})

    # 1. Null-Regionen grau
    polys.loc[polys[col] == 0].plot(
        ax=ax, color="lightgrey", edgecolor="white", linewidth=0.2)

    # 2. >0-Regionen log-skaliert mit FIXEN Grenzen
    pos = polys.loc[polys[col] > 0]
    if not pos.empty:
        pos.plot(
            column=col, ax=ax, cmap="YlGnBu",
            # HIER DIE ÄNDERUNG: Nutzung der festen Werte
            norm=LogNorm(vmin=VMIN_FIXED, vmax=VMAX_FIXED),
            legend=True, linewidth=0.2, edgecolor="grey",
            legend_kwds={'label': "Abfallaufkommen (kg)"}
        )

    # 3. Recyclingwerke dieses Jahres
    pts = ry_df.loc[ry_df["periode"] == yr]

    if not pts.empty:
        pts.plot(ax=ax,
                 markersize=pts["kapazität"].map({"klein": 30, "groß": 80}),
                 marker="o", color="red", edgecolor="black", zorder=3)

    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_title(f"Szenario-Vergleich: Jahr {yr}")
    ax.axis("off")
    plt.tight_layout()
    plt.show()


# In[ ]:


# Diagnose 1: Daten-Check
total_supply_model = q_supply_data.sum().sum()
print(f"Gesamter Abfall im Modell (über alle Jahre/Orte): {total_supply_model:,.0f} kg")

# Vergleich mit Kapazität eines Werks (pro Jahr)
print(f"Kapazität eines großen Werks pro Jahr: {K_r_groß:,.0f} kg")

if total_supply_model == 0:
    print("❌ FEHLER: Das Modell sieht keinen Abfall! Prüfen Sie den Import.")
elif total_supply_model < K_r_groß:
    print("⚠️ WARNUNG: Der gesamte Abfall über 25 Jahre passt in ein einziges Werk. Ist die Einheit (Tonne vs kg) falsch?")
else:
    print("✅ Daten scheinen da zu sein. Das Problem liegt im Modell (Constraints).")


# In[ ]:


# aktives Recyclingwerk = value > 0
active = ry_df[ry_df["value"] > 0]

counts = (
    active.groupby("periode")["recycling"]
    .nunique()
    .reset_index()
    .rename(columns={"periode": "Jahr", "recycling": "Anzahl_Recyclingwerke"})
    .sort_values("Jahr")
)

print(counts)


# In[ ]:


new = dfs_geom_nz["dy_r_k"].groupby("periode").recycling.nunique()
print(new)


# In[ ]:


print("Keys in dfs_geom:", dfs_geom.keys())
print("Keys in dfs_geom_nz:", dfs_geom_nz.keys())


# In[ ]:


# 1) alle Werke, die 2040 überhaupt geöffnet sind
open_2040 = (
    dfs["y_r_k"]          # oder dfs_geom_nz, wenn dort value≠0
      .query("periode == 2040 & value > 0")      # nur geöffnete
      .loc[:, ["recycling", "kapazität"]]
)

# 2) Zufluss pro Werk & Kapazitätsklasse
flow_2040 = (
    dfs["flow_rk"]
      .query("periode == 2040")
      .groupby(["recycling", "kapazität"])["value"].sum()
      .reset_index()
)

# 3) nur die Zeilen behalten, die wirklich offen sind
df_util = flow_2040.merge(open_2040, on=["recycling", "kapazität"], how="inner")

# 4) Kapazitäten hinterlegen und Auslastung berechnen
cap_map = {"groß": K_r_groß, "klein": K_r_klein}
df_util["cap"] = df_util["kapazität"].map(cap_map)
df_util["util"] = df_util["value"] / df_util["cap"]

print(df_util["util"].describe())


# ## Kartendarstellungen der Abfallströme zwischen Recyclingwerken und Zementwerken

# In[ ]:


import geopandas as gpd
from shapely.geometry import LineString
import numpy as np
from matplotlib.colors import LogNorm

proj_crs = polys.crs                     # z. B. EPSG 3035

# 1) Recycling-Werke ins projizierte CRS
ry_df = dfs_geom_nz["y_r_k"].to_crs(proj_crs)

# 2) Ströme: beide Punktspalten separat umprojizieren,
#    danach gleich LineStrings vorhalten (spart Zeit im Loop)
flows_raw = dfs_geom_nz["x_rz"].copy()

for col in ["geom_recycling", "geom_zement"]:
    flows_raw[col] = (
        gpd.GeoSeries(flows_raw[col], crs=4326)    # ursprüngl. WGS 84
        .to_crs(proj_crs)
    )

# LineStrings erzeugen
flows_raw["geom_line"] = flows_raw.apply(
    lambda r: LineString([r.geom_recycling, r.geom_zement]), axis=1
)

# GeoDataFrame mit projizierter Linie
flows_gdf = gpd.GeoDataFrame(flows_raw, geometry="geom_line", crs=proj_crs)

# 3) Zementwerke sind bereits projiziert (cement_gdf_proj)
cem_gdf = cement_gdf_proj               # unverändert


# In[ ]:


import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from shapely.geometry import box
import numpy as np

# ─────────────────────────────────────────
# 1. Manuelle Skalierung für Vergleichbarkeit
# ─────────────────────────────────────────
# Nutzen Sie hier DIESELBEN Werte wie im vorherigen Skript!
VMIN_FIXED = 1_000       # Untergrenze (z.B. 1 Tonne)
VMAX_FIXED = 20_000_000 # Obergrenze (z.B. 500 Mio kg)

print(f"Farbskala fixiert auf: Min={VMIN_FIXED:,.0f}, Max={VMAX_FIXED:,.0f}")

# Jahre und Grenzen
years = [i for i in range(2026, 2051, 5)] # 5-Jahres-Schritte
xlim  = (2.5e6, 6.1e6)
ylim  = (1.3e6, 5.5e6)

# ─────────────────────────────────────────
# 2. Räumlicher Filter (Bounding Box)
#    Verhindert Fehler durch Überseegebiete
# ─────────────────────────────────────────
visible_bbox = box(xlim[0], ylim[0], xlim[1], ylim[1])

# Wir filtern die GeoDataFrames VOR der Schleife
# .to_crs() sicherstellen
ry_df_proj = dfs_geom_nz["y_r_k"].to_crs(polys.crs)
flows_gdf_proj = flows_gdf.to_crs(polys.crs) 
cem_gdf_proj = cem_gdf.to_crs(polys.crs)

# Filtern auf Sichtbarkeit (Intersects Bounding Box)
ry_visible = ry_df_proj[ry_df_proj.intersects(visible_bbox)]
flows_visible = flows_gdf_proj[flows_gdf_proj.intersects(visible_bbox)]
cem_visible = cem_gdf_proj[cem_gdf_proj.intersects(visible_bbox)]

# ─────────────────────────────────────────
# 3. Plot-Schleife
# ─────────────────────────────────────────
for yr in years:
    col = str(yr)
    if col not in polys.columns:
        continue

    fig, ax = plt.subplots(figsize=(10, 10), subplot_kw={"aspect": "equal"})

    # A) Hintergrundkarte
    # Null-Werte grau
    polys.loc[polys[col] == 0].plot(
        ax=ax, color="#f0f0f0", edgecolor="white", linewidth=0.2
    )
    # Positive Werte (FIXE SKALA)
    pos = polys.loc[polys[col] > 0]
    if not pos.empty:
        pos.plot(
            column=col, ax=ax, cmap="YlGnBu",
            # HIER DIE ÄNDERUNG: Feste Werte nutzen
            norm=LogNorm(vmin=VMIN_FIXED, vmax=VMAX_FIXED),
            legend=False, linewidth=0.2, edgecolor="#d0d0d0"
        )

    # B) Recyclingwerke (gefiltert)
    r_pts = ry_visible.loc[ry_visible["periode"] == yr]
    if not r_pts.empty:
        r_pts.plot(
            ax=ax,
            markersize=r_pts["kapazität"].map({"klein": 40, "groß": 100}).fillna(40),
            marker="o", color="#e74c3c", edgecolor="black", linewidth=0.8, zorder=3,
            label="Recyclingwerk"
        )

    # C) Transportflüsse (gefiltert)
    current_flows = flows_visible.loc[flows_visible["periode"] == yr]

    if not current_flows.empty:
        # Linienstärke basierend auf Menge
        w = current_flows["value"].to_numpy()
        w_max = w.max() if w.max() > 0 else 1
        lw = 0.5 + 3.0 * (w / w_max)

        current_flows.plot(
            ax=ax, linewidth=lw, color="purple", alpha=0.6, zorder=2,
            label="Transport R->Z"
        )

        # D) Aktive Zementwerke (gefiltert)
        active_z_ids = current_flows["zementwerk"].unique()
        active_cem = cem_visible.loc[cem_visible.index.isin(active_z_ids)]

        if not active_cem.empty:
            active_cem.plot(
                ax=ax, marker="^", markersize=60,
                color="#f1c40f", edgecolor="black", linewidth=0.5, zorder=4,
                label="Zementwerk"
            )

    # Layout
    ax.set_xlim(*xlim)
    ax.set_ylim(*ylim)
    ax.set_title(f"Jahr {yr}: Abfallaufkommen, Recycling & Zement-Logistik", fontsize=14)
    ax.axis("off")

    plt.tight_layout()
    plt.show()


# ## Daten und KPIs

# In[ ]:


print("flows_gdf-Spalten:", flows_gdf.columns.tolist(), "\n")
print("erste 3 Zeilen flows_gdf:")
print(flows_gdf.head(3))

print("Index/Spalten cost_matrix_sup_rec:",
      cost_matrix_sup_rec.index.name, " / ",
      cost_matrix_sup_rec.columns.name)
print("erste 3 Labels (Zeile, Spalte):",
      list(cost_matrix_sup_rec.index[:3]),
      list(cost_matrix_sup_rec.columns[:3]))


# In[ ]:


# ============================================================
# A) Grundlagen & Datentransformation für Kostenanalyse
# ============================================================

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

# ------------------------------------------------------------
# 1. Gemeinsames CRS (projiziert) festlegen
# ------------------------------------------------------------
proj_crs = polys.crs

# ------------------------------------------------------------
# 3. Kapazitätskonstanten
# ------------------------------------------------------------
cap_map = {"groß": K_r_groß, "klein": K_r_klein}

# ------------------------------------------------------------
# 4. Inflationsfaktoren (Jahr → Faktor)
# ------------------------------------------------------------
rate = 0.02

# 1. Jährliche Basis-Inflation (2026 = 1.0)
# Wir rechnen sicherheitshalber bis 2055, damit der letzte Block (2046-2050) komplett abgedeckt ist.
infl_yearly = {yr: (1 + rate) ** (yr - 2026) for yr in range(2026, 2056)}

# 2. Für Investitionen (CAPEX): Faktor des jeweiligen Startjahres (2026, 2031...)
infl_start = {t: infl_yearly[t] for t in T}

# 3. Für laufende Kosten (OPEX): Durchschnitt der 5 Jahre der Periode
infl_avg = {}
for t in T:
    # Berechnet den Durchschnitt von t bis t+4 (also exakt 5 Jahre, z.B. 2026-2030)
    infl_avg[t] = sum(infl_yearly[yr] for yr in range(t, t + 5)) / 5.0

print("✔ Inflationsfaktoren berechnet (Basis 2026).")
print(f"   Beispiel Periode 2026-2030: Start-Faktor = {infl_start[2026]:.4f}, Durchschnitt = {infl_avg[2026]:.4f}")
print(f"   Beispiel Periode 2031-2035: Start-Faktor = {infl_start[2031]:.4f}, Durchschnitt = {infl_avg[2031]:.4f}")

# ------------------------------------------------------------
# 5. Transportkosten in flows_gdf ergänzen
# ------------------------------------------------------------
# GeoDF für A → R  (x_ar_g)
flows_ar = dfs_geom_nz["x_ar_g"].copy()
flows_rz = dfs_geom_nz["x_rz"].copy()

VOL_FACTOR_GROSS = 3.33

def get_ar_costs(row):
    base_cost = cost_matrix_sup_rec.at[row["angebot"], row["recycling"]]
    # Volumenfaktor anwenden, wenn es ein Großteil ist
    if row["größe"] == "groß":
        return base_cost * VOL_FACTOR_GROSS
    else:
        return base_cost

flows_ar["tk_ar"] = flows_ar.apply(get_ar_costs, axis=1)

flows_rz["tk_rz"] = flows_rz.apply(
    lambda r: cost_matrix_rec_cem.at[r["recycling"], r["zementwerk"]],
    axis=1
)


# In[ ]:


# ============================================================
# B) KPI-Berechnungen (HYBRID: Alt-bewährt + Neue Faktoren)
# ============================================================

import pandas as pd
import numpy as np

# 1. Hilfsbelegungen
dy_df   = dfs_geom_nz.get("dy_r_k", pd.DataFrame()) 
y_df    = dfs_geom_nz.get("y_r_k", pd.DataFrame())
db_df   = dfs_geom_nz.get("db_rt", pd.DataFrame())
b_df    = dfs_geom_nz.get("b_rt", pd.DataFrame())
ar_df   = flows_ar.copy() 
rz_df   = flows_rz.copy()
flow_rk = dfs.get("flow_rk", pd.DataFrame())
if not flow_rk.empty: flow_rk = flow_rk[flow_rk["value"] > 0].copy()

# S-Variable (nur für kombiniertes Szenario)
s_df = pd.DataFrame()
if "s_aig" in dfs:
    s_df = dfs["s_aig"][dfs["s_aig"]["value"] > 0].copy()

if not y_df.empty and "iso" not in y_df.columns:
    y_df["iso"] = y_df["recycling"].map(iso_R)

# 2. Standort-KPIs (Ihr alter Code, leicht angepasst für leere DFs)
def calc_site_kpis(year):
    yr = int(year)
    works = y_df.query("periode == @yr")
    if works.empty:
        return {"Werke_gesamt": 0, "Werke_klein": 0, "Werke_groß": 0, "Werke_GT": 0,
                "Länder_mit_Werk": 0, "Ø_Auslastung": 0, "Median_Auslastung": 0}

    inflows = ar_df.query("periode == @yr").groupby("recycling")["value"].sum().reset_index()
    cap_map_kg = {"groß": K_r_groß, "klein": K_r_klein}
    caps = works[["recycling", "kapazität"]].drop_duplicates().assign(cap_kg=lambda df: df["kapazität"].map(cap_map_kg))

    util = pd.DataFrame()
    if not inflows.empty:
        util = inflows.merge(caps, on="recycling", how="left").assign(util=lambda df: df["value"] / df["cap_kg"])

    n_gt = 0
    if not b_df.empty: n_gt = b_df.query("periode == @yr & value > 0")["recycling"].nunique()

    return {
        "Werke_gesamt": works["recycling"].nunique(), 
        "Werke_klein": works.groupby("kapazität")["recycling"].nunique().get("klein", 0),
        "Werke_groß": works.groupby("kapazität")["recycling"].nunique().get("groß", 0), 
        "Werke_GT": n_gt,
        "Länder_mit_Werk": works["iso"].nunique(), 
        "Ø_Auslastung": util["util"].mean() if not util.empty else 0,
        "Median_Auslastung": util["util"].median() if not util.empty else 0,
    }

# 3. Kosten-KPIs (Der entscheidende Teil)
def calc_cost_kpis(year):
    yr = int(year)

    # --- FAKTOREN ---
    period_weight = 5.0  # Konstant 5 Jahre

    fact_inv = infl_start[yr]
    fact_run = infl_avg[yr] * period_weight

    def safe_sum(series): return series.sum() if not series.empty else 0.0

    # DataFrames filtern
    s_y = s_df.query("periode == @yr").copy() if not s_df.empty else pd.DataFrame()
    ar_y = ar_df.query("periode == @yr")
    rz_y = rz_df.query("periode == @yr")
    dy_y = dy_df.query("periode == @yr")
    y_y = y_df.query("periode == @yr")
    db_y = db_df.query("periode == @yr")
    b_y = b_df.query("periode == @yr")
    flow_y = flow_rk.query("periode == @yr") if not flow_rk.empty else pd.DataFrame()

    # --- 1. Transport (Einfach) ---
    trans_ar = safe_sum(ar_y["value"] * ar_y["tk_ar"])
    trans_rz = safe_sum(rz_y["value"] * rz_y["tk_rz"])

    # --- 2. Demontage (Robust mit Fallback) ---
    demontage = 0
    if not s_y.empty:
        s_y['cost_key'] = list(zip(s_y['industrie'], s_y['größe']))
        # Hier nutzen wir .get() mit Fallback auf DE, wie im funktionierenden Diagnose-Skript
        def get_dem_cost(r):
            iso = iso_A.get(r['angebot']) or r['angebot'][:2]
            costs = cd.get(r['cost_key'], {})
            return r['value'] * (costs.get(iso) or costs.get("DE", 0))
        demontage = safe_sum(s_y.apply(get_dem_cost, axis=1))

    # --- 3. Entsorgung ---
    entsorgung = 0
    if not rz_y.empty:
        # Hier drop() nutzen, da rz_y Geometrie haben KANN
        clean_rz = rz_y.drop(columns=['geom_recycling', 'geom_zement', 'geom_line'], errors='ignore')
        def get_ents(r):
            iso = iso_Z.get(r['zementwerk']) or "DE"
            return r['value'] * (czw.get(iso) or czw.get("DE", 0))
        entsorgung = safe_sum(clean_rz.apply(get_ents, axis=1))

    # --- 4. Werkskosten (Explizite Helper-Funktion wie im alten Code, aber mit Fallback) ---
    def calc_fix(df_source, cost_dict, is_gt=False):
        if df_source.empty: return 0.0
        # drop() für Sicherheit
        df_clean = df_source.drop(columns=['geom_recycling'], errors='ignore')

        def get_cost(r):
            iso = iso_R.get(r['recycling']) or r['recycling'][:2]
            if is_gt:
                val = cost_dict.get(iso) or cost_dict.get("DE", 0)
            else:
                cap = r['kapazität']
                val = cost_dict.get(cap, {}).get(iso) or cost_dict.get(cap, {}).get("DE", 0)
            return r['value'] * val
        return safe_sum(df_clean.apply(get_cost, axis=1))

    invest_fix = calc_fix(dy_y, cr_fix)
    lauf_fix   = calc_fix(y_y, lk)
    invest_gt  = calc_fix(db_y, cb, is_gt=True)
    lauf_gt    = calc_fix(b_y, lkGT, is_gt=True)

    # --- 5. Variable Zerkleinerung ---
    var_cz = 0
    if not flow_y.empty:
        def get_cz(r):
            iso = iso_R.get(r['recycling']) or r['recycling'][:2]
            cap = r['kapazität']
            val = cz.get(cap, {}).get(iso) or cz.get(cap, {}).get("DE", 0)
            return r['value'] * val
        var_cz = safe_sum(flow_y.apply(get_cz, axis=1))

    # --- GESAMTKOSTEN (Mit Faktoren!) ---
    # Alles laufende * fact_run
    # Alles einmalige * fact_inv

    total = (demontage + trans_ar + trans_rz + lauf_fix + var_cz + lauf_gt + entsorgung) * fact_run + \
            (invest_fix + invest_gt) * fact_inv

    return {
        "Demontage":      demontage * fact_run,
        "Transport_AR":   trans_ar * fact_run,
        "Transport_RZ":   trans_rz * fact_run,
        "Invest_Werk":    invest_fix * fact_inv,
        "Fixkosten_Werk": lauf_fix * fact_run,
        "Zerkleinerung":  var_cz * fact_run,
        "Invest_GT":      invest_gt * fact_inv,
        "Fixkosten_GT":   lauf_gt * fact_run,
        "Entsorgung":     entsorgung * fact_run,
        "Gesamtkosten":   total,
    }

def aggregate_kpis(years):
    rows = []
    for yr in years:
        row = {"Jahr": yr}
        row.update(calc_site_kpis(yr))
        row.update(calc_cost_kpis(yr))
        rows.append(row)
    return (pd.DataFrame(rows).set_index("Jahr").sort_index())

# ============================================================
# Beispielaufruf
# ============================================================
years = range(2026, 2051, 5) 
kpi_tbl = aggregate_kpis(years)
print(kpi_tbl)


# In[ ]:


# 1. Summe aus der KPI-Tabelle (Ihre Berechnung)
kpi_sum = kpi_tbl["Gesamtkosten"].sum()

# 2. Wert aus dem Solver (Optimierung)
solver_sum = solution.objective_value

print(f"KPI-Berechnung:  {kpi_sum:,.2f} €")
print(f"Solver-Lösung:   {solver_sum:,.2f} €")

diff = abs(kpi_sum - solver_sum)
print(f"Differenz:       {diff:,.2f} €")

if diff < 100: # Toleranz für Rundungsfehler
    print("✅ MATCH: Die KPI-Berechnung bildet exakt ab, was der Solver getan hat.")
else:
    print("⚠️ MISMATCH: Die KPI-Berechnung weicht ab. Wir müssen suchen, wo.")


# In[ ]:


# ------------------------------------------------------------
# KPI-Tabelle als Excel speichern
# ------------------------------------------------------------
excel_path = OUTPUT_BASE / "kpi_summary.xlsx"
kpi_tbl.to_excel(excel_path, sheet_name="KPIs")

print(f"✅  KPI-Datei geschrieben nach: {excel_path}")


# In[ ]:





# In[ ]:





# In[ ]:





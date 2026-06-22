#!/usr/bin/env python
# coding: utf-8

# # Installationen

# In[27]:


# Notebook-Zelle (oder Anaconda Prompt)
get_ipython().system('conda install -y --override-channels -c conda-forge "osmnx>=2.1"')


# In[19]:


# in einer Notebook-Zelle oder in der Anaconda Prompt ausführen
get_ipython().system('conda install -y -c conda-forge fiona')


# In[223]:


#Import notwendiger Pakete
get_ipython().run_line_magic('pip', 'install geopy')
get_ipython().run_line_magic('pip', 'install geopandas')
get_ipython().run_line_magic('pip', 'install osmnx')

# in einer leeren Notebook-Zelle ausführen (Shell-Aufruf mit "!"):
get_ipython().system('conda install -y -c conda-forge osmnx pyrosm')





# # Importe

# In[3]:


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


# In[5]:


import sys
print(sys.version)
import cplex, docplex
print(cplex.__version__, docplex.__version__)


# In[7]:


import cplex
print(cplex.Cplex().get_version())


# In[9]:


#Einlesen Excel-Datei ---------HIER DIE DATENBASIS ÄNDERN----------------------
import argparse

_parser = argparse.ArgumentParser(description="Run the GFRP-Waste network optimization for one input scenario.")
_parser.add_argument("input_excel", help="Path to the input Excel scenario file (e.g. Nur_Auto.xlsx)")
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
        "supply",
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

# --- C) Supply (Angebot) ---
supply_wind = clean_cols(xls["supply"].copy())
supply_wind["Industrie"] = "Auto"

supply_df = supply_wind
# Sicherstellen, dass x und y numerisch sind (manchmal sind sie Strings in Excel)
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


print("✅ Daten importiert und Spalten bereinigt.")
print(f"Supply Spalten: {supply_df.columns.tolist()}") # Zur Kontrolle

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


# In[11]:


print("Vorhandene Spalten:", supply_df.columns.tolist())
#print("Gewählte Jahres-Spalten:", jahres_cols)


# In[13]:


# =============================================================================
# DATEN AUSLESEN UND VARIABLEN SPEICHERN
# =============================================================================

# 1) Anzahl Abfallorte / Recyclingorte / Zementorte
A_count = len(supply_df)          
R_count = len(recycling_df)       
Z_count = len(zementwerke_df)     

# 2) Abfall-Größen definieren
groessen = ["klein"]
G_count  = len(groessen)

# 3) Industrien definieren (JETZT BEIDE)
industrien = ["Auto"]
I_count    = len(industrien)

# 4) ZEIT-DEFINITION (5-Jahres-Schritte)
# Wir definieren T hier direkt, damit es konsistent ist.
T = [2025, 2030, 2035, 2040, 2045] 
T_count = len(T)

# Mapping: Modell-Jahr (int) -> Excel-Spaltenname (str)
year_map = {
    2025: "2025-2029",
    2030: "2030-2034",
    2035: "2035-2039",
    2040: "2040-2044",
    2045: "2045-2050"
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


# In[15]:


print(supply_gdf.head())


# In[17]:


print(recycling_gdf.head())


# In[19]:


print(cement_gdf.head())


# ## Kosten Dicts erstellen

# In[22]:


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


# In[24]:


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

# In[27]:


import sys
print(sys.executable)
import fiona
print("fiona version:", fiona.__version__)


# In[29]:


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

# In[32]:


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


# In[34]:


# Punkte + nuts0 liegen bereits in EPSG:3035
supply_gdf_proj["iso"]    = gpd.sjoin(supply_gdf_proj,    nuts0, how="left")["CNTR_CODE"]
recycling_gdf_proj["iso"] = gpd.sjoin(recycling_gdf_proj, nuts0, how="left")["CNTR_CODE"]
cement_gdf_proj["iso"]    = gpd.sjoin(cement_gdf_proj,    nuts0, how="left")["CNTR_CODE"]

print(supply_gdf_proj.head(10))
#print(recycling_gdf_proj.head(30))
#print(cement_gdf_proj["iso"].head(30))


# In[36]:


# Kontroll-Snippet: Anzahl fehlender ISO-Codes (NaN) pro Tabelle
for label, gdf in [("Supply",    supply_gdf_proj),
                   ("Recycling", recycling_gdf_proj),
                   ("Cement",    cement_gdf_proj)]:
    fehlende_iso = gdf["iso"].isna().sum()
    print(f"{label:9s}: {fehlende_iso} / {len(gdf)} Einträge ohne ISO-Zuordnung")


supply_gdf_proj[supply_gdf_proj["iso"].isna()]


# In[38]:


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

# In[41]:


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

# In[44]:


# --------------------------- Parameter ---------------------------------
# 1) Kapazitäts-Dict  {Z_ID: max_tonnes_per_year}
cap_z = zementwerke_df['Demand GFK in kg'].to_dict()


# # Modell initialisieren

# In[47]:


#CPLEX initialisieren - Modell erzeugen
from docplex.mp.model import Model
mip = Model("Cplex")
print("CPLEX-Modell initialisiert.")


# In[49]:


# --- Sets definieren ---
# Wir leiten die Sets direkt aus den Indizes/Spalten der fertigen Kostenmatrizen ab.
# Das garantiert, dass es keinen "KeyError" gibt, wenn wir später darauf zugreifen.

A = cost_matrix_sup_rec.index.tolist()    # Angebotsorte
R = cost_matrix_sup_rec.columns.tolist()  # Recyclingorte
Z = cost_matrix_rec_cem.columns.tolist()  # Zement-Regionen

# T (Zeit) haben wir ganz am Anfang schon definiert (2025, 2030...),
# wir nutzen hier das globale T.

# G (Größe) und I (Industrie) - Wir haben gesagt "Nur Wind"
G = ["klein"] 
I = ["Auto"]
K = ["groß", "klein"] # Kapazitäten der Recyclingwerke

print(f"Anzahl Angebotsorte (A): {len(A)}")
print(f"Anzahl Recyclingorte (R): {len(R)}")
print(f"Anzahl Zement-Regionen (Z): {len(Z)}")
print(f"Betrachtete Jahre (T): {T}")


# # Entscheidungsvariablen anlegen

# In[52]:


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


# In[54]:


# ============================================================
# Entscheidungsvariablen  (inkl. “Eröffnungs-Delta”)
# ============================================================

# --- Menge aller Tupel für kontinuierliche / binäre Variablen -------
XARG = [(a, r, g, t) for a in A for r in R for g in G for t in T]   # A → R  (groß/klein)
XRZ  = [(r, z, t)    for r in R for z in Z for t in T]              # R → Z
YRK  = [(r, k, t)    for r in R for k in K for t in T]              # Werk r,k offen?
BRT  = [(r, t)       for r in R for t in T]                         # Großteile-Linie aktiv?

XRKT = [(r, k, t) for r in R for k in K for t in T]                 # Hilfsvariable für Linearisierung
# --- ❶ Transport- und Betriebsmengen --------------------------------
x_ar_g = mip.continuous_var_dict(XARG, name="x_ar_g")   # [t]
x_rz   = mip.continuous_var_dict(XRZ,  name="x_rz")     # [t]

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


# In[56]:


print( ("gross" in G), ("groß" in G) )
# und
print( any("gross"  in k for k in x_ar_g), 
       any("groß"   in k for k in x_ar_g) )


# # Nebenbedingungen anlegen

# In[59]:


# Hilfsfunktionen und Anpassungen:

# Hilfsfunktion: Jahr -> String-Label der Spalte
year_col = lambda t: str(t)           # z. B. 2025 -> "2025"

# ---------- Supply-Matrix in float konvertieren und NaN→0 -----------------
q_supply_wind = q_supply_wind.astype(float).fillna(0.0)

# Setze die NUTS_ID als Index, damit der Zugriff über 'AT11' etc. funktioniert
q_supply_wind = supply_df.set_index("NUTS_ID")[jahres_cols]


# In[61]:


# ----- Nebenbedingung 3-2  Angebotsbilanz ---------------------------------------------

for a in A:
    for t in T:
        qty = q_supply_wind.at[a, year_map[t]] 

        mip.add_constraint(
            mip.sum(x_ar_g[a, r, g, t] for r in R for g in G) == qty,
            ctname=f"supply_bal_a{a}_t{t}"
        )

# ---------------- Nebenbedingung 3-3 (globale Kapazitäten) ----------------
for r in R:
    for t in T:

        # Gesamtzufluss im Jahr t an Werk r
        inflow = mip.sum(
            x_ar_g[a, r, g, t]
            for a in A
            for g in G          # { "groß" } oder { "groß","klein" }
        )

        # Kapazität, falls das Werk im Jahr t mit einem Typ aktiv ist
        capacity = (
            K_r_groß * y_r_k[r, "groß", t] +
            K_r_klein * y_r_k[r, "klein", t]
        )

        mip.add_constraint(
            inflow <= capacity,
            ctname=f"cap_r{r}_t{t}"
        )

# ---------------- NB 3-4  Exklusivwahl der Werkgröße ----------------
for r in R:           # alle Recycling-Standorte
    for t in T:       # alle Jahre
        mip.add_constraint(
            y_r_k[r, "groß", t] + y_r_k[r, "klein", t] <= 1,
            ctname=f"only_one_size_r{r}_t{t}"
        )

# Sicherstellen, dass Z_IDs auch richtig indiziert sind
q_demand_z = zementwerke_df["Demand GFK in kg"]
#    (kein weiteres set_index nötig, der Index ist ja bereits Z_ID)

# ---------------- NB 3-5  Nachfrageobergrenze je Zementwerk und Jahr ----------------
for z in Z:                     # Z_IDs aus dem Index
    N_z = q_demand_z.at[z]      # kg/Jahr
    for t in T:                 # alle Jahre
        mip.add_constraint(
            mip.sum(x_rz[r, z, t] for r in R) <= N_z,
            ctname=f"demand_cap_z{z}_t{t}"
        )


# ---------------- NB 3-10  Vorzerschneide-Kapazität (nur GRÖSSE 'groß') ----------------
if "groß" in G:
    # Fall A: Es gibt Großteile (Wind oder Kombiniert) -> Beschränkung aktiv
    for r in R:
        for t in T:
            mip.add_constraint(
                mip.sum(x_ar_g[a, r, "groß", t] for a in A) 
                <= b_rt[r, t] * K_r_groß,
                ctname=f"bulk_line_cap_r{r}_t{t}"
            )
else:
    # Fall B: Es gibt KEINE Großteile (Auto) -> Großteillinie wird nicht benötigt
    # Wir zwingen b_rt auf 0, damit der Solver keine unnötigen Investitionskosten verursacht.
    for r in R:
        for t in T:
            mip.add_constraint(b_rt[r, t] == 0, ctname=f"no_bulk_line_needed_r{r}_t{t}")

print("✅ NB 3-10 (Großteil-Kapazität) angepasst.")


# ---------------- NB 3-11  Massenbilanz Recyclingwerk r in Jahr t ----------------
for r in R:                 # alle Recyclingwerke
    for t in T:             # alle Jahre
        mip.add_constraint(
            # angelieferte Gesamtmasse (groß + klein)
            u * mip.sum(x_ar_g[a, r, g, t] for a in A for g in G)
            ==
            # abgegebene Masse an Zementwerke
            mip.sum(x_rz[r, z, t]        for z in Z),
            ctname=f"mass_balance_r{r}_t{t}"
        )


# ---------------- NB 3-12  Irreversibilität der Werköffnungen ----------------
for r in R:                    # Recycling-Standorte
    for k in K:                # {'groß', 'klein'}
        for t_idx in range(len(T) - 1):          # bis vorletztes Jahr
            t      = T[t_idx]
            t_next = T[t_idx + 1]

            mip.add_constraint(
                y_r_k[r, k, t] <= y_r_k[r, k, t_next],
                ctname=f"open_monotone_r{r}_{k}_t{t}"
            )


# ---------------- NB 3-13  Großteil-Anlage bleibt offen (monoton) ----------------
for r in R:                            # Recycling-Standorte
    for t_idx in range(len(T) - 1):    # bis vorletztes Jahr
        t      = T[t_idx]
        t_next = T[t_idx + 1]

        mip.add_constraint(
            b_rt[r, t] <= b_rt[r, t_next],
            ctname=f"bulk_monotone_r{r}_t{t}"
        )

# ---------------- NB  :  Zementwerks-Kapazität ----------------
#   ∑_r x_rzt  ≤  cap_z[z]          ∀ z ∈ Z,  ∀ t ∈ T
for z in Z:          # alle Zementwerke
    for t in T:      # alle Jahre
        mip.add_constraint(
            mip.sum(x_rz[r, z, t] for r in R) <= cap_z[z],
            ctname=f"cap_Z_{z}_{t}"
        )

print("Alle Nebenbedingungen sind angelegt")


# In[63]:


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


# In[65]:


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


# In[67]:


# Anzahl aller Constraints
print(f"Gesamtzahl Nebenbedingungen: {mip.number_of_constraints}")

# (optional) kompaktes Modell-Summary
mip.print_information()


# In[69]:


from docplex.mp.model import Model
import docplex, sys, inspect, cplex

print("docplex-Version :", docplex.__version__)
print("docplex-Pfad    :", inspect.getfile(docplex))
print("cplex-Version   :", cplex.__version__)
print("Model hat refine_conflict:", hasattr(Model, "refine_conflict"))


# # Zielfunktion Kostenminimierung

# In[72]:


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


# In[74]:


# =============================================================================
# Fix: Inflation für den gesamten Zeitraum sicherstellen
# =============================================================================
# Wir definieren infl neu für den maximalen Zeitraum bis 2050,
# damit garantiert alle Jahre in T abgedeckt sind.
rate = 0.02
infl = {yr: (1 + rate) ** (yr - 2025) for yr in range(2025, 2052)}

print(f"Inflations-Dict aktualisiert. Schlüssel vorhanden für 2035? {2035 in infl}")


# In[76]:


# =============================================================================
# SCHRITT 8 (Nur Wind): ZIELFUNKTION (Kostenminimierung mit Faktor 5 & Persyn)
# =============================================================================

expr = mip.linear_expr()

print("Erstelle Zielfunktion (Szenario: Nur Auto)...")

for t in T:
    # --- A) ZEIT-GEWICHTUNG ---
    if t == 2045:
        period_weight = 6.0
    else:
        period_weight = 5.0

    infl_t = infl[t]
    cost_factor = infl_t * period_weight

    # --------------------------------------------------------------
    # 1) DEMONTAGEKOSTEN (€/t) -> Laufend
    # --------------------------------------------------------------
    for (a, r, g, tt) in XARG:
        if tt != t: continue

        iso_a = iso_A.get(a)
        dem_c = cd.get(("Auto", g), {}).get(iso_a, 0)
        expr += cost_factor * dem_c * x_ar_g[a, r, g, t]

    # --------------------------------------------------------------
    # 2) TRANSPORT A -> R (€/t) -> Laufend
    # --------------------------------------------------------------
    for (a, r, g, tt) in XARG:
        if tt != t: continue

        tk_ar = cost_matrix_sup_rec.at[a, r]       
        expr += cost_factor * tk_ar * x_ar_g[a, r, g, t]

    # --------------------------------------------------------------
    # 3) TRANSPORT R -> Z (€/t) -> Laufend
    # --------------------------------------------------------------
    for (r, z, tt) in XRZ:
        if tt != t: continue

        tk_rz = cost_matrix_rec_cem.at[r, z]       
        expr += cost_factor * tk_rz * x_rz[r, z, t]

    # ------------------------------------------------------------------
    # 4) RECYCLING-WERK
    # ------------------------------------------------------------------
    for r in R:
        iso_r = iso_R.get(r)

        # Fallback-Logik für ISO-Codes
        if iso_r is None: iso_r = r[:2]
        if iso_r not in cr_fix["groß"]: iso_r = "DE"

        for k in K:
            # 4a) Investition (EINMALIG -> nur infl_t)
            expr += infl_t * cr_fix[k][iso_r] * dy_r_k[r, k, t]

            # 4b) Laufende Fixkosten (JÄHRLICH -> cost_factor)
            expr += cost_factor * lk[k][iso_r] * y_r_k[r, k, t]

            # 4c) Variable Zerkleinerung (JÄHRLICH -> cost_factor) --- WIEDER EINGEFÜGT!
            # Wir nutzen hier die Hilfsvariable flow_rk aus der Linearisierung
            expr += cost_factor * cz[k][iso_r] * flow_rk[r, k, t]

        # 4d) Großteillinie
        # Investition (Einmalig -> nur infl_t)
        expr += infl_t * cb[iso_r] * db_rt[r, t]

        # Betrieb (JÄHRLICH -> cost_factor)
        expr += cost_factor * lkGT[iso_r] * b_rt[r, t]

    # --------------------------------------------------------------
    # 5) ENTSORGUNG ZEMENTWERK (€/t) -> Laufend
    # --------------------------------------------------------------
    for (r, z, tt) in XRZ:
        if tt != t: continue

        iso_z = iso_Z.get(z)
        expr += cost_factor * czw[iso_z] * x_rz[r, z, t]

# --------------------------------------------------------------
# Zielfunktion setzen
# --------------------------------------------------------------
mip.minimize(expr)
print("✅ Zielfunktion - Kostenminimierung (Nur Wind, Faktor 5/6, Persyn, inkl. Var. Kosten) gesetzt.")


# # Start des Optimierungsmodells

# In[79]:


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
p.mip.tolerances.mipgap.set(0.001)

# --- 3) RECHENRESSOURCEN & SPEICHER (AGGRESSIV) ---
p.threads.set(0)
p.parallel.set(1) # Deterministisch, um alle Kerne zu nutzen
p.emphasis.memory.set(0)
p.workmem.set(120000)
p.mip.limits.treememory.set(120000)

# --- 4) ALGORITHMUS-TUNING ---
p.lpmethod.set(4) # Barrier-Algorithmus

'''
# --- 6) EXTERNER "HARD-STOP"-TIMER ---
# Diese Funktion läuft in einem separaten Thread und beendet den Solver nach einer festen Zeit.
def external_stopper(delay_sec: float, cplex_engine):
    print(f"[Thread] Externer Stopper startet, Abbruch in {delay_sec/3600:.1f} h …")
    time.sleep(delay_sec)
    print("[Thread] ZEIT ABGELAUFEN – breche CPLEX ab.")
    cplex_engine.abort()

# Thread starten, BEVOR wir .solve() aufrufen
hard_stop_time_sec = 180 # 1 Stunden

cpx = mip.get_cplex()
stopper_thread = threading.Thread(
    target=external_stopper,
    args=(hard_stop_time_sec, cpx),  
    daemon=True
)
stopper_thread.start()
'''
# ----------------- SOLVE (NUR MIT DER HIGH-LEVEL DOCPLEX API) -----------------
print("Starte CPLEX-Lösungsprozess mit 4-Tage-Timer...")
solution = mip.solve(log_output=True)

mip.report()


# In[81]:


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


# In[83]:


df_sol_tidy.head()
#df_sol.head()


# # Auswertung

# In[86]:


idx_names = {
    "x_ar_g": ["angebot","recycling","größe","periode"],
    "x_rz"  : ["recycling","zementwerk","periode"],
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


# In[88]:


#Testlauf des Dataframes

for var, df in dfs.items():
    # Nur Einträge mit Wert ungleich 0
    df_nonzero = df[df["value"] != 0]
    print(f"\n=== Variable: {var} (Spalten: {df_nonzero.columns.tolist()}) ===")
    # Maximal 5 zufällige Zeilen anzeigen
    print(df_nonzero.sample(n=min(5, len(df_nonzero)), random_state=42))


# In[90]:


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


# In[92]:


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


# In[94]:


for var, df in dfs_geom.items():
    # Nur Einträge mit Wert ungleich 0
    df_nonzero = df[df["value"] != 0]
    print(f"\n=== Variable: {var} (Spalten: {df_nonzero.columns.tolist()}) ===")
    # Maximal 5 zufällige Zeilen anzeigen
    print(df_nonzero.sample(n=min(5, len(df_nonzero)), random_state=42))


# In[96]:


# Kopie des Dataframes ohne die Variablen die 0 sind
dfs_geom_nz = {name: gdf[gdf["value"] != 0].copy()
               for name, gdf in dfs_geom.items()}


# In[98]:


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
print(polys[['NUTS_ID', 'CNTR_CODE', '2025', '2030']].head())


# In[100]:


# alle Jahresspalten automatisch identifizieren
year_cols = [c for c in polys.columns if c.isdigit()]

polys["sum_all_years"] = polys[year_cols].sum(axis=1)   # t im gesamten Zeitraum


# ## Kartendarstellung der Abfallmengen

# In[103]:


# =========================================================================
# Berechnung der absoluten Gesamtmenge (Gewichtet)
# =========================================================================

# 1. Identifiziere die Jahres-Spalten (z.B. '2025', '2030'...)
year_cols = [c for c in polys.columns if c.isdigit()]

# 2. Definiere die Gewichtung (Dauer der Perioden)
#    Standard: 5 Jahre. Ausnahme: 2045 (deckt 2045-2050 ab) -> 6 Jahre.
#    Wir nutzen Strings als Schlüssel, da die Spaltennamen Strings sind.
weights = {
    '2025': 5,
    '2030': 5,
    '2035': 5,
    '2040': 5,
    '2045': 6
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


# In[105]:


import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm

# ---------------------------------------------------------
# Manuelle Grenzen für die Vergleichbarkeit setzen
# ---------------------------------------------------------
# Tipp: Nehmen Sie hier das Minimum und Maximum aller Ihrer Szenarien.
# Da es eine Log-Skala ist, darf vmin NICHT 0 sein.
VMIN_FIXED = 1000       # Untergrenze der Farbskala (z.B. 1 Tonne)
VMAX_FIXED = 200000000 # Obergrenze der Farbskala (z.B. 500 Mio kg)

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

# In[108]:


import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
import pandas as pd

# ─────────────────────────────────────────
# 1. Manuelle Skalierung für Vergleichbarkeit
# ─────────────────────────────────────────
# Tragen Sie hier Werte ein, die für ALLE Ihre Szenarien sinnvoll sind.
# Tipp: Nehmen Sie das Maximum aus dem "Wind + Auto"-Szenario als VMAX.
VMIN_FIXED = 1000       # Untergrenze (z.B. 1 Tonne), darf nicht 0 sein!
VMAX_FIXED = 200000000 # Obergrenze (z.B. 500 Mio kg)

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


# In[110]:


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


# In[112]:


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


# In[114]:


new = dfs_geom_nz["dy_r_k"].groupby("periode").recycling.nunique()
print(new)


# In[116]:


print("Keys in dfs_geom:", dfs_geom.keys())
print("Keys in dfs_geom_nz:", dfs_geom_nz.keys())


# In[118]:


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

# In[121]:


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


# In[123]:


import matplotlib.pyplot as plt
from matplotlib.colors import LogNorm
from shapely.geometry import box
import numpy as np

# ─────────────────────────────────────────
# 1. Manuelle Skalierung für Vergleichbarkeit
# ─────────────────────────────────────────
# Nutzen Sie hier DIESELBEN Werte wie im vorherigen Skript!
VMIN_FIXED = 1_000       # Untergrenze (z.B. 1 Tonne)
VMAX_FIXED = 200_000_000 # Obergrenze (z.B. 500 Mio kg) 

print(f"Farbskala fixiert auf: Min={VMIN_FIXED:,.0f}, Max={VMAX_FIXED:,.0f}")

# Jahre und Grenzen
years = [i for i in range(2025, 2051, 5)] # 5-Jahres-Schritte
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

# In[125]:


print("flows_gdf-Spalten:", flows_gdf.columns.tolist(), "\n")
print("erste 3 Zeilen flows_gdf:")
print(flows_gdf.head(3))

print("Index/Spalten cost_matrix_sup_rec:",
      cost_matrix_sup_rec.index.name, " / ",
      cost_matrix_sup_rec.columns.name)
print("erste 3 Labels (Zeile, Spalte):",
      list(cost_matrix_sup_rec.index[:3]),
      list(cost_matrix_sup_rec.columns[:3]))


# In[127]:


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
# Robuste Berechnung für alle Jahre bis 2051
infl = {yr: (1 + rate) ** (yr - 2025) for yr in range(2025, 2052)}

print(f"Inflationsfaktoren (Check): 2025={infl[2025]:.2f}, 2030={infl[2030]:.2f}")

# ------------------------------------------------------------
# 5. Transportkosten in flows_gdf ergänzen
# ------------------------------------------------------------
# GeoDF für A → R  (x_ar_g)
flows_ar = dfs_geom_nz["x_ar_g"].copy()

# GeoDF für R → Z  (x_rz)
flows_rz = dfs_geom_nz["x_rz"].copy()

# Wir nutzen .at[] für schnelleren Zugriff auf die Persyn-Matrizen
flows_ar["tk_ar"] = flows_ar.apply(
    lambda r: cost_matrix_sup_rec.at[r["angebot"], r["recycling"]],
    axis=1
)

flows_rz["tk_rz"] = flows_rz.apply(
    lambda r: cost_matrix_rec_cem.at[r["recycling"], r["zementwerk"]],
    axis=1
)

print("✅ Vorbereitung abgeschlossen – GeoDFs & Kostenfelder sind bereit.")
print(f"   Einträge A->R: {len(flows_ar)}")
print(f"   Einträge R->Z: {len(flows_rz)}")


# In[129]:


# ============================================================
# B) KPI-Berechnungen (ROBUST mit Fallback-Logik)
# ============================================================

import pandas as pd
import numpy as np

# ------------------------------------------------------------
# 1. Hilfsbelegungen
# ------------------------------------------------------------
dy_df   = dfs_geom_nz.get("dy_r_k", pd.DataFrame()) 
y_df    = dfs_geom_nz.get("y_r_k", pd.DataFrame())
db_df   = dfs_geom_nz.get("db_rt", pd.DataFrame())
b_df    = dfs_geom_nz.get("b_rt", pd.DataFrame())

ar_df   = flows_ar.copy() 
rz_df   = flows_rz.copy()

# Flow-Variable für Zerkleinerung
flow_rk = dfs.get("flow_rk", pd.DataFrame())
if not flow_rk.empty:
    flow_rk = flow_rk[flow_rk["value"] > 0].copy()

# ISO-Spalte zu y_df hinzufügen
if not y_df.empty and "iso" not in y_df.columns:
    y_df["iso"] = y_df["recycling"].map(iso_R)

# ============================================================
# HILFSFUNKTIONEN FÜR ROBUSTEN KOSTEN-ZUGRIFF
# ============================================================
def get_iso_robust(nuts_id, iso_dict):
    """Holt ISO aus Dict oder extrahiert aus NUTS-ID."""
    iso = iso_dict.get(nuts_id)
    if iso is None and isinstance(nuts_id, str):
        return nuts_id[:2] # Fallback: Erste 2 Buchstaben (z.B. "DE" aus "DE11")
    return iso

def get_cost_value(iso, cost_structure, capacity=None):
    """
    Holt Kostenwert. Fallback auf 'DE', falls Land nicht gefunden.
    cost_structure kann sein:
      - dict[iso] -> wert (z.B. cb)
      - dict[kapazität][iso] -> wert (z.B. cr_fix)
    """
    try:
        # Fall 1: Struktur hängt von Kapazität ab (dict[kap][iso])
        if capacity is not None:
            if capacity not in cost_structure: return 0.0
            country_dict = cost_structure[capacity]
            # Versuch 1: Echtes Land
            val = country_dict.get(iso)
            # Versuch 2: Fallback auf DE
            if val is None: val = country_dict.get("DE", 0.0)
            return val

        # Fall 2: Struktur ist flach (dict[iso])
        else:
            val = cost_structure.get(iso)
            if val is None: val = cost_structure.get("DE", 0.0)
            return val
    except:
        return 0.0

# ------------------------------------------------------------
# 2. Standort-KPIs
# ------------------------------------------------------------
def calc_site_kpis(year):
    yr = int(year)
    works = y_df.query("periode == @yr")

    if works.empty:
        return {
            "Werke_gesamt": 0, "Werke_klein": 0, "Werke_groß": 0, "Werke_GT": 0,
            "Länder_mit_Werk": 0, "Ø_Auslastung": 0, "Median_Auslastung": 0,
        }

    inflows = ar_df.query("periode == @yr").groupby("recycling")["value"].sum().reset_index()
    cap_map_kg = {"groß": K_r_groß, "klein": K_r_klein}
    caps = works[["recycling", "kapazität"]].drop_duplicates().assign(cap_kg=lambda df: df["kapazität"].map(cap_map_kg))

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
        "Werke_GT": n_gt, "Länder_mit_Werk": works["iso"].nunique(), 
        "Ø_Auslastung": util["util"].mean() if not util.empty else 0,
        "Median_Auslastung": util["util"].median() if not util.empty else 0,
    }

# ------------------------------------------------------------
# 3. Kosten-KPIs (ROBUST)
# ------------------------------------------------------------
def calc_cost_kpis(year):
    yr = int(year)

    # --- FAKTOREN ---
    infl_t = infl[yr]
    if yr == 2045: period_weight = 6.0
    else: period_weight = 5.0
    fact_run = infl_t * period_weight
    fact_inv = infl_t

    # --- DATEN FILTERN ---
    def safe_sum(series): return series.sum() if not series.empty else 0.0

    ar_y = ar_df.query("periode == @yr")
    rz_y = rz_df.query("periode == @yr")
    dy_y = dy_df.query("periode == @yr")
    y_y = y_df.query("periode == @yr")
    db_y = db_df.query("periode == @yr")
    b_y = b_df.query("periode == @yr")
    flow_y = flow_rk.query("periode == @yr")

    # 1. Transport (Werte sind schon da)
    trans_ar = safe_sum(ar_y["value"] * ar_y["tk_ar"])
    trans_rz = safe_sum(rz_y["value"] * rz_y["tk_rz"])

    # 2. Demontage
    base_dem = 0
    if not ar_y.empty:
        # Logik: Iso holen (mit Fallback) -> Kosten holen (mit Fallback)
        def get_dem_cost(r):
            iso = get_iso_robust(r['angebot'], iso_A)
            # cd hat Struktur [("Auto", )][ISO]
            costs = cd.get(("Auto", r['größe']), {})
            val = costs.get(iso)
            if val is None: val = costs.get("DE", 0)
            return r['value'] * val

        base_dem = safe_sum(ar_y.apply(get_dem_cost, axis=1))

    # 3. Entsorgung
    base_ents = 0
    if not rz_y.empty:
        # Drop geom to be safe
        df_clean = rz_y.drop(columns=['geom_recycling', 'geom_zement', 'geom_line'], errors='ignore')
        def get_ents_cost(r):
            iso = get_iso_robust(r['zementwerk'], iso_Z)
            val = czw.get(iso)
            if val is None: val = czw.get("DE", 0)
            return r['value'] * val
        base_ents = safe_sum(df_clean.apply(get_ents_cost, axis=1))

    # 4. Werkskosten (Invest & Laufend)
    def calc_with_helper(df_source, cost_dict, use_cap=True):
        if df_source.empty: return 0.0
        df_clean = df_source.drop(columns=['geom_recycling'], errors='ignore')
        return safe_sum(df_clean.apply(lambda r: r['value'] * get_cost_value(
            get_iso_robust(r['recycling'], iso_R), 
            cost_dict, 
            capacity=r['kapazität'] if use_cap else None
        ), axis=1))

    base_inv_fix  = calc_with_helper(dy_y, cr_fix, use_cap=True)
    base_lauf_fix = calc_with_helper(y_y, lk,     use_cap=True)
    base_inv_gt   = calc_with_helper(db_y, cb,    use_cap=False)
    base_lauf_gt  = calc_with_helper(b_y,  lkGT,  use_cap=False)

    # 5. Variable Zerkleinerung
    base_var_cz = 0
    if not flow_y.empty:
        base_var_cz = safe_sum(flow_y.apply(lambda r: r['value'] * get_cost_value(
            get_iso_robust(r['recycling'], iso_R),
            cz,
            capacity=r['kapazität']
        ), axis=1))

    # --- GESAMTKOSTEN ---
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

# ------------------------------------------------------------
# 4. Aggregation
# ------------------------------------------------------------
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
years = range(2025, 2051, 5) 
kpi_tbl = aggregate_kpis(years)
print(kpi_tbl)


# In[131]:


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


# In[133]:


# ------------------------------------------------------------
# KPI-Tabelle als Excel speichern
# ------------------------------------------------------------
excel_path = OUTPUT_BASE / "kpi_summary.xlsx"
kpi_tbl.to_excel(excel_path, sheet_name="KPIs")

print(f"✅  KPI-Datei geschrieben nach: {excel_path}")


# In[ ]:





# In[ ]:





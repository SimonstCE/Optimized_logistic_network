# GFRP-Waste Network Optimization

Two related Gurobi MILP optimization projects that model the cumulative EU recycling network for GFRP waste (wind-turbine blades, and combined wind + automotive waste) over 2026–2050: which recycling plants to open/expand, and how to route waste from supply regions through recycling plants to cement co-processing plants at minimum cost.

## Repository structure

```
.
├── Wind-const-100_02/                       # Wind-only waste scenario
│   ├── Wind-peak-100_045_final_4.py         # main optimization + visualization script
│   ├── visualize_scenarios.py               # regenerate the 4 cumulative maps for an already-solved scenario
│   └── *.xlsx                               # input scenario files
├── Wind_peak_car_100_45_final6_Large/       # Combined wind + automotive waste scenario
│   ├── Wind_peak_car_100_45_final6_Large.py # main optimization + visualization script
│   ├── build_kpi_summary.py                 # rebuild the KPI summary Excel from a saved solution
│   └── *.xlsx                               # input scenario files
├── requirements.txt
└── README.md
```

Each `*.xlsx` file is one input scenario (supply locations, recycling plant candidates, cement plants, cost parameters, transport cost matrix). Pick which one to run via `dateipfad` (see below).

## What each main script does

1. Reads one input `.xlsx` scenario file
2. Builds and solves a MILP with Gurobi: which recycling plants to open/expand each period, and how to route waste from supply → recycling → cement plants
3. Exports the solution, a KPI summary, and cumulative network maps to an output folder named after the input file (see **Output structure** below)

## Setup

```bash
# Create and activate the environment (change the Python version if 3.12 isn't available)
conda create -n gurobi python=3.12 -y
conda activate gurobi

# Conda dependencies
conda install -c conda-forge pandas numpy matplotlib geopandas shapely tqdm osmnx networkx fiona -y

# Gurobi
python -m pip install gurobipy
# — or, if you prefer installing Gurobi through conda instead of pip:
# conda install -c gurobi gurobipy

# Remaining Python dependencies
pip install -r requirements.txt

# Verify Gurobi is importable and licensed
python -c "import gurobipy as gp; print('Gurobi version:', gp.gurobi.version())"
```

### Gurobi license

Both scripts require a valid Gurobi license (academic licenses are free for university use). Point Gurobi at your license file with the `GRB_LICENSE_FILE` environment variable, or place it at the default location (`~/gurobi.lic` on Linux/macOS):

```bash
export GRB_LICENSE_FILE=/path/to/your/gurobi.lic
```

See [Gurobi's license documentation](https://www.gurobi.com/documentation/current/quickstart_linux/retrieving_and_setting_up_.html) for obtaining one.

### NUTS-2 shapefile

Both scripts use the Eurostat NUTS-2 region boundaries (`NUTS_RG_20M_2016_3035.shp` and its sidecar files `.dbf`/`.shx`/`.prj`/`.cpg`) to assign country codes and compute region centroids. This shapefile is **not included in this repo** — download it from [Eurostat GISCO](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics) (NUTS 2016, 1:20 Million, EPSG:3035), then update the hardcoded path in each script to wherever you place it:

```python
# Wind-const-100_02/Wind-peak-100_045_final_4.py            (line 132)
# Wind_peak_car_100_45_final6_Large/Wind_peak_car_100_45_final6_Large.py  (line 449)
nuts2_shp = "/path/to/your/NUTS_RG_20M_2016_3035.shp"
```

## Configuring the input scenario

Each script processes one scenario at a time, selected by editing `dateipfad` near the top of the file:

```python
# Wind-const-100_02/Wind-peak-100_045_final_4.py
dateipfad = "./Nur_Wind_Lichtenegger_294.xlsx"

# Wind_peak_car_100_45_final6_Large/Wind_peak_car_100_45_final6_Large.py
dateipfad = "/absolute/path/to/Kombiniert_Lichtenegger_294.xlsx"
```

A relative path is resolved against your current working directory when the script runs — use an absolute path if you plan to run it with `nohup`/`tmux`/`cron` from a different directory.

## Running

### Option 1 — nohup (simplest, survives logout)

```bash
nohup python Wind-peak-100_045_final_4.py > run.log 2>&1 &
echo "PID: $!"
```

- All output (stdout + stderr) is written to `run.log`
- The process keeps running after you close the terminal
- `$!` prints the process ID so you can track or kill it later

**Monitor progress:**
```bash
tail -f run.log
```

**Check if still running:**
```bash
ps -p <PID>
```

**Stop the process:**
```bash
kill <PID>
```

### Option 2 — nohup + disown (extra safety)

```bash
nohup python Wind-peak-100_045_final_4.py > run.log 2>&1 &
disown
```

`disown` removes the job from the shell's job table so it won't be killed even if the shell exits abnormally.

### Option 3 — tmux (recommended for long Gurobi runs)

```bash
tmux new -s gurobi
python Wind-peak-100_045_final_4.py
```

Detach (keep running): `Ctrl+B`, then `D`

Reattach later:
```bash
tmux attach -t gurobi
```

The same options apply to `Wind_peak_car_100_45_final6_Large.py` — just substitute the filename.

## Output structure

Each run creates a folder named after the input `.xlsx` file's stem, next to the script:

```
<xlsx-stem>/
├── gurobi_solutions/     # .sol / .lp files, solution_tidy.csv
├── plots/                # 01-04 cumulative network/hotspot maps (+ yearly flow maps for the Wind-const script)
└── kpi_summary.xlsx      # per-period and cumulative cost/site KPIs
```

For example, running with `dateipfad = "./Nur_Wind_Lichtenegger_294.xlsx"` creates `Nur_Wind_Lichtenegger_294/` in the script's directory.

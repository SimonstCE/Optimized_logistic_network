# GFRP-Waste Network Optimization

MILP optimization projects that model the cumulative EU recycling network for GFRP waste (wind-turbine blades and automotive composite waste) over 2026–2050: which recycling plants to open/expand, and how to route waste from supply regions through recycling plants to cement co-processing plants at minimum cost.

Three scenario types, each with one or more solver implementations:

| Directory | Scenario | Scripts | Solver |
|---|---|---|---|
| `windconst/` | Wind-only waste | `Wind-peak-100_045_final_4.py`, `Wind-const-100_045_final4.py` | Gurobi, CPLEX |
| `windpeak/` | Combined wind + automotive waste | `Wind_peak_car_100_45_final6_Large.py`, `Wind_const_car_100_45_final4.py` | Gurobi, CPLEX |
| `car/` | Automotive-only waste | `Car_100_02.py` | CPLEX |

## Repository structure

```
.
├── windconst/                              # Wind-only waste scenario
│   ├── Wind-peak-100_045_final_4.py        # main optimization + visualization script (Gurobi)
│   ├── Wind-const-100_045_final4.py        # main optimization + visualization script (CPLEX)
│   └── visualize_scenarios.py              # regenerate the 4 cumulative maps for an already-solved scenario
├── windpeak/                                # Combined wind + automotive waste scenario
│   ├── Wind_peak_car_100_45_final6_Large.py # main optimization + visualization script (Gurobi)
│   ├── Wind_const_car_100_45_final4.py     # main optimization + visualization script (CPLEX)
│   └── build_kpi_summary.py                # rebuild the KPI summary Excel from a saved solution
├── car/                                     # Automotive-only waste scenario
│   └── Car_100_02.py                       # main optimization script (CPLEX)
├── inputs/                                  # All input Excel scenario files (shared by all 5 scripts)
│   └── *.xlsx
├── NUTS/                                    # Eurostat NUTS-2 shapefile (not included — see Setup)
├── requirements.txt
└── README.md
```

Each `.xlsx` file in `inputs/` is one input scenario (supply locations, recycling plant candidates, cement plants, cost parameters, transport cost matrix). Pick which one to run by passing its path as a command-line argument (see **Running** below).

## What each main script does

1. Reads one input `.xlsx` scenario file, given as a command-line argument
2. Builds and solves a MILP (with Gurobi or CPLEX, depending on the script): which recycling plants to open/expand each period, and how to route waste from supply → recycling → cement plants
3. Exports the solution, a KPI summary, and cumulative network maps to an output folder named after the input file, created next to the script (see **Output structure** below)

## Setup

```bash
# Create and activate the environment (change the Python version if 3.12 isn't available)
conda create -n optimization python=3.12 -y
conda activate optimization

# Conda dependencies
conda install -c conda-forge pandas numpy matplotlib geopandas shapely tqdm osmnx networkx fiona -y

# Remaining Python dependencies
pip install -r requirements.txt
```

### Gurobi (windconst/Wind-peak-100_045_final_4.py, windpeak/Wind_peak_car_100_45_final6_Large.py)

```bash
python -m pip install gurobipy
# — or, if you prefer installing Gurobi through conda instead of pip:
# conda install -c gurobi gurobipy

# Verify Gurobi is importable and licensed
python -c "import gurobipy as gp; print('Gurobi version:', gp.gurobi.version())"
```

Requires a valid Gurobi license (academic licenses are free for university use). Point Gurobi at your license file with the `GRB_LICENSE_FILE` environment variable, or place it at the default location (`~/gurobi.lic` on Linux/macOS):

```bash
export GRB_LICENSE_FILE=/path/to/your/gurobi.lic
```

See [Gurobi's license documentation](https://www.gurobi.com/documentation/current/quickstart_linux/retrieving_and_setting_up_.html) for obtaining one.

### CPLEX (windconst/Wind-const-100_045_final4.py, windpeak/Wind_const_car_100_45_final4.py, car/Car_100_02.py)

```bash
python -m pip install cplex docplex
```

Requires a valid IBM CPLEX license (a free academic license is available through IBM Academic Initiative). See [IBM's CPLEX documentation](https://www.ibm.com/docs/en/icos) for installation and licensing.

### NUTS-2 shapefile

All scripts use the Eurostat NUTS-2 region boundaries (`NUTS_RG_20M_2016_3035.shp` and its sidecar files `.dbf`/`.shx`/`.prj`/`.cpg`) to assign country codes and compute region centroids. This shapefile is **not included in this repo** — download it from [Eurostat GISCO](https://ec.europa.eu/eurostat/web/gisco/geodata/statistical-units/territorial-units-statistics) (NUTS 2016, 1:20 Million, EPSG:3035), place it in `NUTS/`, and update the hardcoded `nuts2_shp` path near the top of each script if you place it elsewhere.

## Running

Each script takes the path to one input Excel file as its only required argument:

```bash
python windconst/Wind-peak-100_045_final_4.py   inputs/Nur_Wind_Lichtenegger_294.xlsx
python windconst/Wind-const-100_045_final4.py   inputs/Nur_Wind_Lichtenegger_294.xlsx
python windpeak/Wind_peak_car_100_45_final6_Large.py  inputs/Kombiniert_Lichtenegger_294.xlsx
python windpeak/Wind_const_car_100_45_final4.py       inputs/Kombiniert_Lichtenegger_294.xlsx
python car/Car_100_02.py                        inputs/Nur_Auto.xlsx
```

Run any script with `-h` to see its usage. Use a path relative to where you launch the command, or an absolute path — both work, but use an **absolute** path if you plan to run it with `nohup`/`tmux`/`cron` from a different working directory.

> `car/Car_100_02.py` needs `inputs/Nur_Auto.xlsx`, which is not yet provided — supply this file before running it.

### Background execution

#### Option 1 — nohup (simplest, survives logout)

```bash
nohup python windconst/Wind-peak-100_045_final_4.py inputs/Nur_Wind_Lichtenegger_294.xlsx > run.log 2>&1 &
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

#### Option 2 — nohup + disown (extra safety)

```bash
nohup python windconst/Wind-peak-100_045_final_4.py inputs/Nur_Wind_Lichtenegger_294.xlsx > run.log 2>&1 &
disown
```

`disown` removes the job from the shell's job table so it won't be killed even if the shell exits abnormally.

#### Option 3 — tmux (recommended for long solver runs)

```bash
tmux new -s optimization
python windconst/Wind-peak-100_045_final_4.py inputs/Nur_Wind_Lichtenegger_294.xlsx
```

Detach (keep running): `Ctrl+B`, then `D`

Reattach later:
```bash
tmux attach -t optimization
```

These options apply to any of the 5 main scripts — substitute the script path and input file.

## Output structure

Each run creates a folder named after the input `.xlsx` file's stem, next to the *script* (not next to the input file):

```
windconst/<xlsx-stem>/
├── gurobi_solutions/      # or cplex_solutions/, depending on the script — .sol/.lp files, solution_tidy.csv
├── plots/                 # 01-04 cumulative network/hotspot maps (+ yearly flow maps for some scripts)
└── kpi_summary.xlsx       # per-period and cumulative cost/site KPIs
```

For example, running `python windconst/Wind-peak-100_045_final_4.py inputs/Nur_Wind_Lichtenegger_294.xlsx` creates `windconst/Nur_Wind_Lichtenegger_294/`.

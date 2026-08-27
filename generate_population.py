"""
Wrapper script for the eqasim Docker service.

Reads population size from APP_CONFIG_PATH (same YAML as the controller) or from the
EQASIM_POPULATION_SIZE env var, computes the synpp sampling_rate, writes a temporary
config, and runs the synpp pipeline.

Cache: if a population JSON already exists in /eqasim-output with enough people
(>= the requested population_size), synpp is skipped entirely.

Expected volumes in docker-compose:
  /eqasim-data   → raw input data (INSEE, OSM, GTFS, BAN, BDTOPO)
  /eqasim-cache  → intermediate pipeline cache (warm on re-runs)
  /eqasim-output → output JSON consumed by the controller
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile

import yaml

# Approximate population of Haute-Garonne (dept 31), used as fallback when no bbox is given.
TOULOUSE_DEPT_POPULATION = 1_400_000

# Départements servis par défaut. Le périmètre d'enquête EMC² en couvre SIX (31, 32, 81,
# 82, 09, 11) ; la version Haute-Garonne du ticket 026 n'en sert qu'un, faute des données
# BD TOPO et BAN des cinq autres. La limite est chiffrée dans
# docs/arch/perimetre-population.md (limite n°6) : elle plafonne la 3ᵉ couronne à 10,6 %
# de la population quand l'enquête en compte 15,4 %.
DEPARTMENTS = ["31"]

# Safety margin over the effective zone population to absorb IPF rounding
SAMPLING_MARGIN = 1.15

OUTPUT_DIR = "/eqasim-output"
OUTPUT_PREFIX = "toulouse_"


def _communes_from_bbox(
    bbox: list[float],
    data_path: str = "/eqasim-data",
) -> tuple[list[str], int]:
    """
    Return (commune_ids, total_population) for communes intersecting the given
    WGS84 bbox [min_lon, min_lat, max_lon, max_lat].

    Uses the IRIS shapefile and INSEE population CSV already present in the
    eqasim data volume — no network call required.
    """
    import geopandas as gpd
    import pandas as pd
    import py7zr
    from pyproj import Transformer
    from shapely.geometry import box

    min_lon, min_lat, max_lon, max_lat = bbox

    # Convert bbox to Lambert-93 (EPSG:2154), the CRS of IRIS shapes.
    t = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    x_min, y_min = t.transform(min_lon, min_lat)
    x_max, y_max = t.transform(max_lon, max_lat)
    bbox_geom = box(x_min, y_min, x_max, y_max)

    # Extract IRIS shapes from the 7z archive into a temp dir.
    iris_dir = os.path.join(data_path, "iris_2024")
    candidates = sorted(glob.glob(os.path.join(iris_dir, "*.7z")))
    if not candidates:
        print(f"[eqasim] Warning: no IRIS archive in {iris_dir}, skipping bbox filter")
        return [], 0

    with tempfile.TemporaryDirectory() as tmp:
        with py7zr.SevenZipFile(candidates[0]) as archive:
            names = [n for n in archive.getnames() if "LAMB93" in n]
            archive.extract(tmp, names)

        gpkg_files = [n for n in names if n.endswith(".gpkg")]
        if not gpkg_files:
            print("[eqasim] Warning: no .gpkg inside IRIS archive, skipping bbox filter")
            return [], 0

        df_iris = gpd.read_file(
            os.path.join(tmp, gpkg_files[0]),
            dtype={"code_iris": str, "code_insee": str},
        )[["code_insee", "geometry"]].rename(columns={"code_insee": "commune_id"})

    # Dissolve to commune level then spatial-join with the bbox polygon.
    df_communes = df_iris.dissolve("commune_id").reset_index()
    bbox_gdf = gpd.GeoDataFrame(geometry=[bbox_geom], crs="EPSG:2154")
    joined = gpd.sjoin(df_communes, bbox_gdf, how="inner", predicate="intersects")
    communes = sorted(joined["commune_id"].unique().tolist())

    # Sum population for those communes from the INSEE population CSV.
    pop_path = os.path.join(data_path, "rp_2022", "base-ic-evol-struct-pop-2022_csv.zip")
    total_pop = 0
    if os.path.exists(pop_path):
        with zipfile.ZipFile(pop_path) as z:
            with z.open("base-ic-evol-struct-pop-2022.CSV") as f:
                df_pop = pd.read_csv(f, sep=";", usecols=["COM", "P22_POP"], dtype={"COM": str})
        total_pop = int(df_pop[df_pop["COM"].isin(communes)]["P22_POP"].sum())

    print(f"[eqasim] bbox filter: {len(communes)} communes, effective population={total_pop}")
    return communes, total_pop


def _perimeter_communes(departments: list[str] | None) -> list[str]:
    """Cadre de tirage : les communes du périmètre d'enquête EMC² (ticket 026).

    La liste vient de `llm_module/data/commune_couronne.json`, produite par
    `make communes-couronnes` depuis la couche SIG de l'enquête — 453 communes sur six
    départements. `departments` la restreint : la version Haute-Garonne du ticket 026
    passe `["31"]` et obtient 346 communes.

    ⚠ **Le cadre n'est pas le périmètre.** Restreindre aux communes du 31 est un choix de
    découpage du travail, chiffré et publié (perimetre-population.md, limite n°6) : il
    plafonne la 3ᵉ couronne à 10,6 % de la population quand l'enquête en compte 15,4 %.
    Le filtre d'admission au chargement, lui, reste sur les 453.

    Lève si la liste est vide : sans ce garde-fou, une faute de frappe ferait retomber en
    silence sur le département entier, et on croirait avoir un cadre conforme.
    """
    from llm_module.core.residence_zone import CommuneTable

    table = CommuneTable.load()
    communes = table.communes(departments)
    counts = table.counts(departments)
    print(f"[eqasim] cadre de tirage : périmètre EMC² restreint à "
          f"{departments or 'tous les départements'} → {len(communes)} communes "
          f"({', '.join(f'{k} {v}' for k, v in counts.items())})")
    return communes


def _population_of(communes: list[str], data_path: str = "/eqasim-data") -> int:
    """Population RP 2022 des communes retenues, pour le `sampling_rate`."""
    import pandas as pd

    pop_path = os.path.join(data_path, "rp_2022", "base-ic-evol-struct-pop-2022_csv.zip")
    if not os.path.exists(pop_path):
        print(f"[eqasim] Warning: {pop_path} absent — population effective inconnue")
        return 0
    with zipfile.ZipFile(pop_path) as z:
        with z.open("base-ic-evol-struct-pop-2022.CSV") as f:
            df = pd.read_csv(f, sep=";", usecols=["COM", "P22_POP"], dtype={"COM": str})
    return int(df[df["COM"].isin(communes)]["P22_POP"].sum())


def _communes_cache_prefix() -> str:
    """Return the standard output prefix regardless of commune subset."""
    return OUTPUT_PREFIX


def _read_population_size_from_config(config_path: str) -> int | None:
    """Extract population_size from the controller YAML config if present."""
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f) or {}
        data = cfg.get("data", {})
        return int(data["population_size"]) if "population_size" in data else None
    except Exception:
        return None


def _find_cached_file(population_size: int, prefix: str = OUTPUT_PREFIX) -> str | None:
    """Return the exact population JSON for the requested size, or None."""
    path = os.path.join(OUTPUT_DIR, f"{prefix}population_{population_size}.json")
    return path if os.path.isfile(path) else None


def run(
    population_size: int | None = None,
    generate_personality: bool = False,
    force: bool = False,
    bbox: list[float] | None = None,
    perimeter: bool | None = None,
    departments: list[str] | None = None,
) -> str | None:
    """
    Generate the population JSON.  Returns the output file path on cache-hit or after
    successful generation.  Raises SystemExit on synpp failure (non-zero exit code).

    bbox: optional [min_lon, min_lat, max_lon, max_lat] in WGS84.  When provided,
    synpp is restricted to the communes that intersect the bbox, and the sampling_rate
    is derived from their actual population instead of the full département.

    perimeter: when true (or EQASIM_PERIMETER=true), the sampling frame is the EMC² 2023
    survey perimeter itself — a LIST OF COMMUNES, not a rectangle (ticket 026).  Takes
    precedence over bbox: a rectangle cannot express "the survey perimeter, no more no
    less".  departments restricts that frame; it defaults to EQASIM_DEPARTMENTS or the
    départements listed in the synpp config (today ["31"], the Haute-Garonne version).
    """
    # ── Resolve population size ────────────────────────────────────────────────
    if population_size is None:
        env_size = os.environ.get("EQASIM_POPULATION_SIZE")
        if env_size:
            population_size = int(env_size)

    if population_size is None:
        app_config_path = os.environ.get("APP_CONFIG_PATH")
        if app_config_path:
            population_size = _read_population_size_from_config(app_config_path)

    if population_size is None:
        population_size = 1000
        print(
            f"[eqasim] EQASIM_POPULATION_SIZE not set and APP_CONFIG_PATH not found "
            f"— defaulting to {population_size} agents."
        )

    if not force:
        force = os.environ.get("EQASIM_FORCE_REGENERATE", "false").lower() == "true"

    print(f"[eqasim] population_size={population_size}  generate_personality={generate_personality}  bbox={bbox}")

    # ── Resolve sampling frame ─────────────────────────────────────────────────
    communes: list[str] = []
    effective_population = TOULOUSE_DEPT_POPULATION
    output_prefix = OUTPUT_PREFIX

    if perimeter is None:
        perimeter = os.environ.get("EQASIM_PERIMETER", "false").lower() == "true"
    if departments is None:
        env_dep = os.environ.get("EQASIM_DEPARTMENTS", "")
        departments = [d.strip() for d in env_dep.split(",") if d.strip()] or DEPARTMENTS

    if perimeter:
        # Le périmètre d'enquête est une LISTE DE COMMUNES, pas un rectangle : c'est la
        # seule façon de dire « ni plus ni moins » (ticket 026). Il prime donc sur bbox.
        if bbox is not None:
            print("[eqasim] perimeter=true — la bbox est ignorée : un rectangle ne peut "
                  "pas exprimer le périmètre d'enquête")
        communes = _perimeter_communes(departments)
        effective_population = _population_of(communes)
        print(f"[eqasim] population effective du cadre : {effective_population:,} hab "
              f"(RP 2022)")
        output_prefix = _communes_cache_prefix()
    elif bbox is not None:
        communes, effective_population = _communes_from_bbox(bbox)
        if communes:
            output_prefix = _communes_cache_prefix()
        else:
            print("[eqasim] bbox produced no communes — falling back to full département")

    # ── Cache check ────────────────────────────────────────────────────────────
    cached = _find_cached_file(population_size, prefix=output_prefix)
    if cached and not force:
        print(f"[eqasim] Cache hit — using existing file: {cached}  (skipping synpp)")
        return cached
    if cached and force:
        print(f"[eqasim] EQASIM_FORCE_REGENERATE=true — ignoring cached file: {cached}")

    # ── Build synpp config ─────────────────────────────────────────────────────
    if effective_population <= 0:
        effective_population = TOULOUSE_DEPT_POPULATION
    sampling_rate = min(1.0, (population_size * SAMPLING_MARGIN) / effective_population)
    print(
        f"[eqasim] Cache miss — running synpp "
        f"(sampling_rate={sampling_rate:.6f}, effective_population={effective_population})"
    )

    config = {
        "working_directory": "/eqasim-cache",
        "run": [
            "synthesis.population.llm_agents",
        ],
        "config": {
            "processes": int(os.environ.get("EQASIM_PROCESSES", "4")),
            "hts": "entd",
            "sampling_rate": round(sampling_rate, 8),
            "random_seed": int(os.environ.get("EQASIM_RANDOM_SEED", "1234")),
            "data_path": "/eqasim-data",
            "output_path": OUTPUT_DIR,
            "output_prefix": output_prefix,
            "java_memory": "4G",
            "mode_choice": False,
            "regions": [],
            "departments": departments,
            "communes": communes,
            "gtfs_path": "gtfs_toulouse",
            "osm_path": "osm_toulouse",
            "ban_path": "ban_toulouse",
            "bdtopo_path": "bdtopo_toulouse",
            "generate_personality_traits": generate_personality,
        },
    }

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="eqasim_config_"
    ) as tmp:
        yaml.dump(config, tmp, default_flow_style=False, allow_unicode=True)
        tmp_path = tmp.name

    # Snapshot existing files so we can identify what synpp newly creates.
    pattern = re.compile(rf"^{re.escape(output_prefix)}population_(\d+)\.json$")
    files_before = set(os.listdir(OUTPUT_DIR))

    print(f"[eqasim] Running synpp with config: {tmp_path}")
    result = subprocess.run(
        ["uv", "run", "-m", "synpp", tmp_path],
        cwd="/eqasim",
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    # synpp writes the file with the actual agent count (e.g. population_1021.json).
    # Rename it to the exact requested size so downstream code can find it by name.
    # Only consider files that did not exist before the run to avoid picking up stale
    # outputs from earlier runs with a higher number in their name.
    target_path = os.path.join(OUTPUT_DIR, f"{output_prefix}population_{population_size}.json")
    if not os.path.isfile(target_path):
        new_files = [
            (int(m.group(1)), os.path.join(OUTPUT_DIR, name))
            for name in os.listdir(OUTPUT_DIR)
            if name not in files_before and (m := pattern.match(name))
        ]
        if new_files:
            _, src = max(new_files)
            if src != target_path:
                os.rename(src, target_path)
                print(f"[eqasim] Renamed {os.path.basename(src)} → {os.path.basename(target_path)}")

    return _find_cached_file(population_size, prefix=output_prefix)


def main() -> None:
    """Point d'entrée CLI : lance run() avec les paramètres par défaut."""
    run()


if __name__ == "__main__":
    main()

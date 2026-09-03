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
import time
import zipfile

import yaml

# Approximate population of Haute-Garonne (dept 31), used as fallback when no bbox is given.
TOULOUSE_DEPT_POPULATION = 1_400_000

# Départements du périmètre d'enquête EMC² 2023 : SIX (ticket 031, option A). C'est le défaut
# du code ; le déploiement peut le restreindre par `EQASIM_DEPARTMENTS` (docker-compose.yml)
# ou par le corps de la requête. La version Haute-Garonne du ticket 026 passe ["31"], faute des
# données BD TOPO et BAN des cinq autres départements — limite chiffrée dans
# docs/arch/perimetre-population.md (limite n°6) : la 3ᵉ couronne plafonne à 10,6 % de la
# population quand l'enquête en compte 15,4 %.
DEPARTMENTS = ["31", "32", "81", "82", "09", "11"]

# Données départementales attendues par synpp pour CHAQUE département demandé. Le pipeline
# les vérifie avant de lancer synpp : sans cela, l'assertion de `data/bdtopo/raw.py` tombe
# après dix minutes de traitement et sans dire quel fichier manque.
BDTOPO_URL = "https://geoservices.ign.fr/bdtopo"
BAN_URL = "https://adresse.data.gouv.fr/data/ban/adresses/latest/csv/adresses-{dep}.csv.gz"

# Safety margin over the effective zone population to absorb IPF rounding
SAMPLING_MARGIN = 1.15

OUTPUT_DIR = "/eqasim-output"
OUTPUT_PREFIX = "toulouse_"

# Configuration de BASE du pipeline : `config_toulouse.yml`, monté dans le conteneur. C'est la
# seule source des réglages scientifiques (appariement HTS : `filter_hts`, `matching_attributes`,
# `matching_minimum_observations` ; journées donneuses : `hts_school_days_only`,
# `hts_exclude_wednesday_under_age`). Jusqu'au 2026-09-03 le wrapper construisait sa propre
# config SANS ces clés : synpp retombait sur ses défauts — `filter_hts: True`, soit 308 donneurs
# ENTD résidents de Haute-Garonne pour 12 000 personnes à apparier, et une dégradation qui
# abandonnait la classe d'âge (`matching_minimum_observations` 20) — pendant que
# `config_toulouse.yml` (ticket 008, A1.a) disait le contraire. Le fichier absent est une erreur,
# pas un retour aux défauts.
BASE_CONFIG_PATH = os.environ.get("EQASIM_BASE_CONFIG", "/eqasim/config_toulouse.yml")
# Réglages de la base que le wrapper REMPLACE (chemins et paramètres d'exécution du conteneur).
RUNTIME_OVERRIDDEN_KEYS = (
    "processes", "sampling_rate", "random_seed", "data_path", "output_path", "output_prefix",
    "java_memory", "regions", "departments", "communes", "communes_file", "gtfs_path", "osm_path",
    "ban_path", "bdtopo_path", "generate_personality_traits",
)
# Réglages scientifiques relus dans la base et journalisés à chaque génération.
SCIENTIFIC_KEYS = (
    "hts", "filter_hts", "matching_attributes", "matching_minimum_observations",
    "matching_age_boundaries", "hts_school_days_only", "hts_exclude_wednesday_under_age",
    "census_undefined_reweighting", "mode_choice",
)


def load_base_config(path: str = BASE_CONFIG_PATH) -> dict:
    """Section `config:` de `config_toulouse.yml`. Lève si le fichier manque : sans lui, synpp
    apparierait sur ses défauts (308 donneurs) en silence."""
    if not os.path.isfile(path):
        print(f"[eqasim] ERROR [ALARME] configuration de base introuvable : {path} — monter "
              "eqasim-toulouse/config_toulouse.yml dans le conteneur (docker-compose.yml, service "
              "eqasim) ou pointer EQASIM_BASE_CONFIG. Rien n'est généré.")
        raise SystemExit(5)
    with open(path, encoding="utf-8") as f:
        base = (yaml.safe_load(f) or {}).get("config") or {}
    missing = [k for k in SCIENTIFIC_KEYS if k not in base]
    if missing:
        print(f"[eqasim] ERROR [ALARME] {path} ne fixe pas {missing} : ces réglages ne doivent pas "
              "retomber sur les défauts de synpp. Rien n'est généré.")
        raise SystemExit(5)
    return base


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


def check_department_data(departments: list[str], data_path: str = "/eqasim-data",
                          bdtopo_path: str = "bdtopo_toulouse", ban_path: str = "ban_toulouse") -> list[str]:
    """Rend la liste des données départementales MANQUANTES (vide = tout est là).

    BD TOPO : une archive `.7z` ou un dossier `BDTOPO_*_D0<dep>_*` (livraison IGN décompressée)
    dans `bdtopo_path` ; BAN : `adresses-<dep>.csv.gz` dans `ban_path`. Aucun téléchargement
    ici — la décision d'obtenir 1 à 2 Go de BD TOPO par département appartient à l'auteur du
    dépôt (ticket 031, § 1.0).
    """
    missing: list[str] = []
    bdtopo_dir = os.path.join(data_path, bdtopo_path)
    ban_dir = os.path.join(data_path, ban_path)
    for dep in departments:
        code = str(dep).zfill(2)
        tag = f"D0{code}" if len(code) == 2 else f"D{code}"
        has_bdtopo = any(tag in name for name in os.listdir(bdtopo_dir)) if os.path.isdir(bdtopo_dir) else False
        if not has_bdtopo:
            missing.append(f"BD TOPO {tag} attendue dans {bdtopo_dir} (édition alignée sur D031 "
                           f"2024-09-15 ; {BDTOPO_URL})")
        ban_file = os.path.join(ban_dir, f"adresses-{code}.csv.gz")
        if not os.path.isfile(ban_file):
            missing.append(f"BAN {ban_file} ({BAN_URL.format(dep=code)})")
    return missing


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

    # ── Données départementales : tout ou rien ─────────────────────────────────
    # Un département sans BD TOPO ni BAN ne se « saute » pas : le cadre serait amputé sans
    # que la population le dise. On s'arrête AVANT synpp, avec la liste de ce qui manque.
    missing_data = check_department_data(departments)
    if missing_data:
        print(f"[eqasim] ERROR [ALARME] données départementales manquantes pour "
              f"{departments} — génération refusée :")
        for item in missing_data:
            print(f"[eqasim]   - {item}")
        raise SystemExit(3)

    # ── Build synpp config ─────────────────────────────────────────────────────
    if effective_population <= 0:
        effective_population = TOULOUSE_DEPT_POPULATION
    sampling_rate = min(1.0, (population_size * SAMPLING_MARGIN) / effective_population)
    print(
        f"[eqasim] Cache miss — running synpp "
        f"(sampling_rate={sampling_rate:.6f}, effective_population={effective_population})"
    )

    base_config = load_base_config()
    runtime_config = {
        "processes": int(os.environ.get("EQASIM_PROCESSES", "4")),
        "sampling_rate": round(sampling_rate, 8),
        "random_seed": int(os.environ.get("EQASIM_RANDOM_SEED", "1234")),
        "data_path": "/eqasim-data",
        "output_path": OUTPUT_DIR,
        "output_prefix": output_prefix,
        "java_memory": "4G",
        "regions": [],
        "departments": departments,
        "communes": communes,
        "communes_file": "",   # la liste est passée en clair ci-dessus ; le chemin hôte n'existe pas ici
        "gtfs_path": "gtfs_toulouse",
        "osm_path": "osm_toulouse",
        "ban_path": "ban_toulouse",
        "bdtopo_path": "bdtopo_toulouse",
        "generate_personality_traits": generate_personality,
    }
    assert set(runtime_config) == set(RUNTIME_OVERRIDDEN_KEYS)
    config = {
        "working_directory": "/eqasim-cache",
        "run": [
            "synthesis.population.llm_agents",
        ],
        "config": {**base_config, **runtime_config},
    }
    print("[eqasim] réglages scientifiques (config_toulouse.yml) : "
          + ", ".join(f"{k}={config['config'][k]}" for k in SCIENTIFIC_KEYS))

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yml", delete=False, prefix="eqasim_config_"
    ) as tmp:
        yaml.dump(config, tmp, default_flow_style=False, allow_unicode=True)
        tmp_path = tmp.name

    # Le fichier écrit par synpp se reconnaît à sa DATE (écrit après le lancement), pas à la
    # nouveauté de son nom : deux générations qui livrent le même effectif écrivent le même
    # nom, et « nom inconnu avant le run » le manquait (constaté le 2026-09-03 : le vivier frais
    # restait sous son nom d'effectif réel, le fichier cible périmé était rendu à l'appelant).
    pattern = re.compile(rf"^{re.escape(output_prefix)}population_(\d+)\.json$")
    t_start = time.time()

    print(f"[eqasim] Running synpp with config: {tmp_path}")
    result = subprocess.run(
        ["uv", "run", "-m", "synpp", tmp_path],
        cwd="/eqasim",
    )
    if result.returncode != 0:
        sys.exit(result.returncode)

    # synpp writes the file with the actual agent count (e.g. population_1021.json).
    # Rename it to the exact requested size so downstream code can find it by name.
    target_path = os.path.join(OUTPUT_DIR, f"{output_prefix}population_{population_size}.json")
    new_files = [
        (os.path.getmtime(os.path.join(OUTPUT_DIR, name)), int(m.group(1)), os.path.join(OUTPUT_DIR, name))
        for name in os.listdir(OUTPUT_DIR)
        if (m := pattern.match(name)) and os.path.getmtime(os.path.join(OUTPUT_DIR, name)) >= t_start - 1
    ]
    if new_files:
        _, n_written, src = max(new_files)
        if src != target_path:
            # Le fichier cible peut déjà exister (régénération forcée) : il est REMPLACÉ. Avant le
            # 2026-09-03, il était laissé en place et rendu à l'appelant — la population fraîche
            # restait sous son nom d'effectif réel, et le notebook relisait l'ancienne en silence.
            if os.path.isfile(target_path):
                print(f"[eqasim] {os.path.basename(target_path)} existait (régénération forcée) : remplacé")
            os.replace(src, target_path)
            print(f"[eqasim] Renamed {os.path.basename(src)} ({n_written} personnes) → {os.path.basename(target_path)}")
    elif not os.path.isfile(target_path):
        print(f"[eqasim] ERROR [ALARME] synpp n'a produit aucun fichier {output_prefix}population_*.json "
              f"dans {OUTPUT_DIR}")
        sys.exit(4)

    return _find_cached_file(population_size, prefix=output_prefix)


def main() -> None:
    """Point d'entrée CLI : lance run() avec les paramètres par défaut."""
    run()


if __name__ == "__main__":
    main()

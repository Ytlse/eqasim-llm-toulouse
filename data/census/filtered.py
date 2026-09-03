from tqdm import tqdm
import pandas as pd
import numpy as np
import zipfile

"""
This stage filters out census observations which live or work outside of
Île-de-France.

Fork Toulouse (ticket 031) : quand le cadre de tirage est une LISTE DE COMMUNES, les personnes du
recensement dont la commune est « undefined » — elles vivent dans une commune sans IRIS du
département, que le RP ne nomme pas — ne peuvent pas être filtrées par commune. Le stage
`synthesis.population.spatial.home.zones` leur tire ensuite une commune sans IRIS **du cadre**,
au prorata de la population. Les garder toutes revenait à verser la population de toutes les
communes sans IRIS du département dans les seules communes du cadre : mesuré le 2026-09-03 sur les
six départements du périmètre, 17 986 personnes pour 10 000 demandées, 42,5 % en 3ᵉ couronne
(cible 15,4 %), 1 682 personas pour les dix villages audois du cadre (2 143 habitants). Leur poids
est donc multiplié par la part de la population sans IRIS du département qui vit dans le cadre
(RP 2022 : 86,7 % en Haute-Garonne, 9,4 % dans le Gers, 9,0 % dans le Tarn, 20,1 % en
Tarn-et-Garonne, 4,0 % en Ariège, 1,0 % dans l'Aude).
"""

def configure(context):
    context.stage("data.census.cleaned")
    context.stage("data.spatial.codes")

    # Réglage explicite (config_toulouse.yml) : il entre dans l'empreinte synpp du stage, donc le
    # cache se devalide quand on l'active — un changement de code seul ne le fait pas.
    context.config("census_undefined_reweighting", True)
    context.config("data_path")
    context.config("population_path", "rp_2022/base-ic-evol-struct-pop-2022_csv.zip")
    context.config("population_csv", "base-ic-evol-struct-pop-2022.CSV")
    context.config("population_year", 22)


def undefined_commune_shares(df_codes, data_path, population_path, population_csv, year):
    """Part, par département, de la population des communes SANS IRIS qui est dans le cadre.

    Lit la population agrégée RP par IRIS (une commune sans IRIS a un seul IRIS « COM0000 »).
    Rend ``{departement_id: part}`` ; 1.0 quand le cadre contient toutes les communes sans IRIS.
    """
    with zipfile.ZipFile("{}/{}".format(data_path, population_path)) as archive:
        with archive.open(population_csv) as f:
            df_pop = pd.read_csv(f, sep = ";", usecols = ["IRIS", "COM", "P%s_POP" % year],
                                 dtype = {"IRIS": str, "COM": str}).rename(columns = {"P%s_POP" % year: "population"})
    df_pop = df_pop[df_pop["IRIS"].str.endswith("0000")]           # communes sans IRIS
    df_pop["departement_id"] = df_pop["COM"].str[:2]
    frame_communes = set(df_codes["commune_id"].astype(str))
    frame_departments = set(df_codes["departement_id"].astype(str))
    df_pop = df_pop[df_pop["departement_id"].isin(frame_departments)]
    total = df_pop.groupby("departement_id")["population"].sum()
    in_frame = df_pop[df_pop["COM"].isin(frame_communes)].groupby("departement_id")["population"].sum()
    return {dep: float(in_frame.get(dep, 0.0)) / float(total[dep]) if total.get(dep, 0) > 0 else 1.0
            for dep in frame_departments}


def execute(context):
    df = context.stage("data.census.cleaned")

    # Filter requested codes
    df_codes = context.stage("data.spatial.codes")

    requested_communes = set(df_codes["commune_id"].unique())
    df = df[df["commune_id"].isin(requested_communes) | (df["commune_id"] == "undefined")]

    excess_iris = set(df["iris_id"].unique()) - set(df_codes["iris_id"].unique())
    if not excess_iris == {"undefined"}:
        raise RuntimeError("Found additional IRIS: %s" % excess_iris)

    # Fork Toulouse (ticket 031) : les personnes à commune « undefined » sont pondérées par la part
    # de la population sans IRIS de leur département qui vit dans le cadre (voir l'en-tête).
    shares = undefined_commune_shares(df_codes, context.config("data_path"), context.config("population_path"),
                                      context.config("population_csv"), str(context.config("population_year")))
    f_undefined = df["commune_id"] == "undefined"
    if not context.config("census_undefined_reweighting"):
        print("Commune frame: reweighting of undefined-commune persons DISABLED by config (census_undefined_reweighting = false)")
    elif f_undefined.any() and any(share < 0.999 for share in shares.values()):
        df = df.copy()
        weight_before = float(df.loc[f_undefined, "weight"].sum())
        factors = df.loc[f_undefined, "departement_id"].astype(str).map(shares).fillna(1.0).astype(float)
        df.loc[f_undefined, "weight"] = df.loc[f_undefined, "weight"] * factors.values
        weight_after = float(df.loc[f_undefined, "weight"].sum())
        print("Commune frame: %d census persons with undefined commune reweighted by the in-frame share of their "
              "departement's non-IRIS population (%s) — summed weight %.0f -> %.0f ; %d persons with a known commune" % (
                  int(f_undefined.sum()), ", ".join("%s %.1f%%" % (dep, 100.0 * share) for dep, share in sorted(shares.items())),
                  weight_before, weight_after, int((~f_undefined).sum())))
    else:
        print("Commune frame: no reweighting of undefined-commune persons (%s)" % (
            "no undefined commune" if not f_undefined.any() else "frame covers all non-IRIS communes"))

    return df

import os
import json
import pandas as pd
import zipfile

"""
This stages loads a file containing all spatial codes in France and how
they can be translated into each other. These are mainly IRIS, commune,
departement and région.

Fork Toulouse (tickets 026 et 031) : le cadre de tirage peut être une LISTE DE COMMUNES
(`communes`, ou `communes_file` — un fichier `commune_couronne.json` du dépôt llm-agents-gama),
croisée avec `departments`. Le stage journalise le cadre retenu par département et refuse
une commune demandée que le référentiel IRIS ne connaît pas : une commune fusionnée ou mal
codée disparaîtrait sinon du cadre en silence.
"""

def configure(context):
    context.config("data_path")

    context.config("regions", [11])
    context.config("departments", [])
    context.config("communes", [])
    # Fork Toulouse : chemin d'un `commune_couronne.json` (clé `communes[].insee`) qui sert
    # de liste de communes quand `communes` est vide. Chaîne vide = pas de fichier.
    context.config("communes_file", "")
    context.config("codes_path", "codes_2024/reference_IRIS_geo2024.zip")
    context.config("codes_xlsx", "reference_IRIS_geo2024.xlsx")


def load_communes_file(path):
    """Codes INSEE (5 caractères) d'un `commune_couronne.json` (llm_module/data)."""
    with open(path, encoding = "utf-8") as f:
        payload = json.load(f)
    rows = payload.get("communes") or []
    if not rows:
        raise RuntimeError("communes_file %s ne porte aucune commune" % path)
    return sorted({str(row["insee"]).zfill(5) for row in rows})


def apply_commune_frame(df_codes, requested_communes, requested_departments):
    """Restreint le référentiel aux communes demandées et rend (df, journal).

    Le journal compte les communes retenues par département (attendu pour le périmètre
    EMC² complet : 31 → 346, 32 → 38, 81 → 27, 82 → 22, 09 → 10, 11 → 10) et liste les
    communes demandées absentes du référentiel — après restriction départementale, une
    commune d'un département non demandé n'est pas « absente », elle est hors cadre.
    """
    requested = sorted({str(c).zfill(5) for c in requested_communes})
    known = set(df_codes["commune_id"].astype(str))
    if requested_departments:
        in_scope = [c for c in requested if c[:2] in set(requested_departments)
                    or c[:3] in set(requested_departments)]
        out_of_departments = len(requested) - len(in_scope)
    else:
        in_scope, out_of_departments = requested, 0
    unknown = sorted(c for c in in_scope if c not in known)
    df_codes = df_codes[df_codes["commune_id"].astype(str).isin(set(in_scope))]
    per_department = (df_codes.drop_duplicates("commune_id")["departement_id"].astype(str)
                      .value_counts().sort_index().to_dict())
    journal = {
        "communes_demandees": len(requested),
        "hors_departements_demandes": out_of_departments,
        "communes_retenues": int(df_codes["commune_id"].nunique()),
        "iris_retenus": int(df_codes["iris_id"].nunique()),
        "par_departement": per_department,
        "inconnues": unknown,
    }
    return df_codes, journal

def execute(context):
    # Load IRIS registry
    with zipfile.ZipFile(
        "{}/{}".format(context.config("data_path"), context.config("codes_path"))) as archive:
        with archive.open(context.config("codes_xlsx")) as f:
            df_codes = pd.read_excel(f,
                skiprows = 5, sheet_name = "Emboitements_IRIS",dtype={"CODE_IRIS":str,"DEPCOM":str}
            )[["CODE_IRIS", "DEPCOM", "DEP", "REG"]].rename(columns = {
                "CODE_IRIS": "iris_id",
                "DEPCOM": "commune_id",
                "DEP": "departement_id",
                "REG": "region_id"
            }).fillna('0')

    df_codes["iris_id"] = df_codes["iris_id"].astype("category")
    df_codes["commune_id"] = df_codes["commune_id"].astype("category")
    df_codes["departement_id"] = df_codes["departement_id"].astype("category")
    df_codes["region_id"] = df_codes["region_id"].astype(int)

    # Filter zones
    requested_regions = list(map(int, context.config("regions")))
    requested_departments = list(map(str, context.config("departments")))

    if len(requested_regions) > 0:
        df_codes = df_codes[df_codes["region_id"].isin(requested_regions)]

    if len(requested_departments) > 0:
        df_codes = df_codes[df_codes["departement_id"].isin(requested_departments)]

    # Fork Toulouse : cadre de tirage = liste de communes (ticket 026), journalisé par
    # département et contrôlé (ticket 031).
    requested_communes = list(map(str, context.config("communes")))
    communes_file = context.config("communes_file")
    if len(requested_communes) == 0 and communes_file:
        requested_communes = load_communes_file(communes_file)
        print("Cadre de tirage : %d communes lues dans %s" % (len(requested_communes), communes_file))

    if len(requested_communes) > 0:
        df_codes, journal = apply_commune_frame(df_codes, requested_communes, requested_departments)
        print("Cadre de tirage : %d communes retenues sur %d demandees (%d hors des departements "
              "demandes), %d IRIS ; par departement : %s" % (
                  journal["communes_retenues"], journal["communes_demandees"],
                  journal["hors_departements_demandes"], journal["iris_retenus"],
                  ", ".join("%s %d" % kv for kv in journal["par_departement"].items())))
        if journal["inconnues"]:
            # Une commune demandée que le référentiel ignore n'est pas un détail : elle sort
            # du cadre sans laisser de trace, et la population qui en résulte se croit conforme.
            raise RuntimeError(
                "[ALARME] %d commune(s) demandee(s) absente(s) du referentiel IRIS %s : %s — "
                "codes INSEE perimes (fusion de communes ?) ou referentiel a mettre a jour" % (
                    len(journal["inconnues"]), context.config("codes_path"), journal["inconnues"]))
        if journal["communes_retenues"] == 0:
            raise RuntimeError("[ALARME] cadre de tirage vide : aucune des %d communes demandees "
                               "n'est dans les departements %s" % (
                                   journal["communes_demandees"], requested_departments))

    df_codes["iris_id"] = df_codes["iris_id"].cat.remove_unused_categories()
    df_codes["commune_id"] = df_codes["commune_id"].cat.remove_unused_categories()
    df_codes["departement_id"] = df_codes["departement_id"].cat.remove_unused_categories()

    return df_codes

def validate(context):
    if not os.path.exists("%s/%s" % (context.config("data_path"), context.config("codes_path"))):
        raise RuntimeError("Spatial reference codes are not available")

    return os.path.getsize("%s/%s" % (context.config("data_path"), context.config("codes_path")))

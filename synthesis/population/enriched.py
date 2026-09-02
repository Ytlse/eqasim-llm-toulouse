from tqdm import tqdm
import itertools
import numpy as np
import pandas as pd
import numba

import data.hts.egt.cleaned
import data.hts.entd.cleaned

import multiprocessing as mp

"""
This stage fuses census data with HTS data.
"""

def configure(context):
    context.config("with_motorcycles", False)

    context.stage("synthesis.population.matched")
    context.stage("synthesis.population.sampled")
    context.stage("synthesis.population.income.selected")
    # Ticket 015, lot 4 : l'équipement vélo est appris sur EMC² et conditionné à la
    # ZONE FINE du domicile, il faut donc les coordonnées du logement. Cette étape ne
    # dépend que de `synthesis.population.sampled` (via `spatial.home.zones`) : aucun
    # cycle avec `enriched`.
    context.stage("synthesis.population.spatial.home.locations")
    context.config("extra_enriched_attributes", [])
    context.config("random_seed")

    hts = context.config("hts")
    context.stage("data.hts.selected", alias = "hts")

def execute(context):
    # Select population columns
    df_population = context.stage("synthesis.population.sampled")[[
        "person_id", "household_id",
        "census_person_id", "census_household_id",
        "age", "sex", "employed", "studies",
        "number_of_cars", "number_of_motorcycles", "number_of_vehicles", "use_motorcycle",
        "household_size", "consumption_units",
        "socioprofessional_class", "professional_activity",
        "socioprofessional_class_detail", "employment_sector",
        # Scellement AAMAS (ticket 028 / contrôle de population) : ces trois colonnes
        # existent dans le recensement nettoyé et étaient jetées ici. `iris_id` et
        # `commune_id` rattachent le ménage à sa commune SANS résolveur géométrique ;
        # `commute_mode` (RP `TRANS`) est le mode de navette DÉCLARÉ — une vérité terrain
        # par individu, exportée à la racine de l'enregistrement et JAMAIS dans le prompt.
        "commute_mode", "iris_id", "commune_id",
    ]]

    # Attach matching information
    df_matching = context.stage("synthesis.population.matched")
    df_population = pd.merge(df_population, df_matching, on="person_id", how="left")

    initial_size = len(df_population)
    initial_person_ids = len(df_population["person_id"].unique())
    initial_household_ids = len(df_population["household_id"].unique())

    # Attach person and household attributes from HTS
    df_hts_households, df_hts_persons, _ = context.stage("hts")
    df_hts_persons = df_hts_persons.rename(columns = { "person_id": "hts_id", "household_id": "hts_household_id" })
    df_hts_households = df_hts_households.rename(columns = { "household_id": "hts_household_id" })

    columns = ["hts_id", "hts_household_id", "has_license", "has_pt_subscription", "is_passenger"]
    extra_cols = context.config("extra_enriched_attributes")
    assert isinstance(extra_cols, list), "`extra_enriched_attributes` parameter must be a list"
    columns += extra_cols
    df_population = pd.merge(df_population, df_hts_persons[columns], on="hts_id", how="left")

    df_population = pd.merge(df_population, df_hts_households[[
        "hts_household_id", "number_of_bikes"
    ]], on="hts_household_id", how="left")

    # Attach income
    df_income = context.stage("synthesis.population.income.selected")
    df_population = pd.merge(df_population, df_income[[
        "household_id", "household_income"
    ]], on="household_id", how="left")

    # Check consistency
    final_size = len(df_population)
    final_person_ids = len(df_population["person_id"].unique())
    final_household_ids = len(df_population["household_id"].unique())

    assert initial_size == final_size
    assert initial_person_ids == final_person_ids
    assert initial_household_ids == final_household_ids

    # Add car availability
    df_number_of_cars = df_population[["household_id", "number_of_cars"]].drop_duplicates("household_id")
    # Seuls les majeurs comptent dans le nombre de permis du ménage (ticket 008, A1.a).
    # Les permis hérités d'un donneur adulte par un enfant faisaient basculer des
    # ménages de "all" vers "some" : la voiture y devenait « à partager », alors même
    # que le supposé conducteur supplémentaire a neuf ans.
    df_adult_licenses = df_population[["household_id", "has_license", "age"]].copy()
    df_adult_licenses.loc[df_adult_licenses["age"] < 18, "has_license"] = False
    df_number_of_licenses = df_adult_licenses[["household_id", "has_license"]].groupby("household_id").sum().reset_index().rename(columns = { "has_license": "number_of_licenses" })
    df_car_availability = pd.merge(df_number_of_cars, df_number_of_licenses)

    df_car_availability["car_availability"] = None
    df_car_availability.loc[df_car_availability["number_of_cars"] >= df_car_availability["number_of_licenses"], "car_availability"] = "all"
    df_car_availability.loc[df_car_availability["number_of_cars"] < df_car_availability["number_of_licenses"], "car_availability"] = "some"
    df_car_availability.loc[df_car_availability["number_of_cars"] == 0, "car_availability"] = "none"
    df_car_availability["car_availability"] = df_car_availability["car_availability"].astype("category")

    df_population = pd.merge(df_population, df_car_availability[["household_id", "car_availability"]])

    # Handle motorcycle use if needed (remove use_motorcycle)
    if not context.config("with_motorcycles"):
        df_population.drop(columns=["use_motorcycle"])

    # Add bike availability
    # This is done at the household level and then merged with the persons so that not-matched
    # persons have the same bike availability as their household members.
    df_bike_availability = df_population[["household_id", "number_of_bikes", "household_size"]].drop_duplicates("household_id").dropna()

    df_bike_availability["bike_availability"] = "all"
    df_bike_availability.loc[df_bike_availability["number_of_bikes"] < df_bike_availability["household_size"], "bike_availability"] = "some"
    df_bike_availability.loc[df_bike_availability["number_of_bikes"] == 0, "bike_availability"] = "none"
    df_bike_availability["bike_availability"] = df_bike_availability["bike_availability"].astype("category")

    df_population = pd.merge(df_population, df_bike_availability[["household_id", "bike_availability"]])

    # Add age range for education
    df_population["age_range"] = "higher_education"
    df_population.loc[df_population["age"]<=10,"age_range"] = "primary_school"
    df_population.loc[df_population["age"].between(11,14),"age_range"] = "middle_school"
    df_population.loc[df_population["age"].between(15,17),"age_range"] = "high_school"
    df_population["age_range"] = df_population["age_range"].astype("category")

    # ── Équipement vélo : appris sur EMC² 2023, plus recopié de l'ENTD 2008 ──────
    #
    # Ticket 015, lot 4 (la cause racine). Ce qui était fait ici :
    #
    #     P(la personne a un vélo) = min(1, number_of_bikes / household_size)
    #     puis 14,8 % de VAE parmi les porteurs
    #
    # Trois erreurs superposées, toutes mesurées face aux microdonnées EMC² Toulouse
    # 2023 (ProGEDO lil-1750) :
    #
    # 1. `number_of_bikes` est **recopié** du ménage ENTD 2008 apparié à la personne, or
    #    l'appariement ne porte NI sur la taille du ménage NI sur l'habitat. Un
    #    célibataire hérite donc des 3 vélos d'une famille de cinq, et une famille de
    #    cinq du zéro vélo d'un couple âgé. Résultat : le total sortait à peu près juste
    #    (53,3 % de porteurs contre ~51 % attendus) mais le gradient de taille de ménage
    #    était **inversé** — 76 % de porteurs chez les personnes seules contre 33 %
    #    observés, 36 % dans les ménages de 4 contre 65 %.
    # 2. La variable ENTD lue est `V1_JNBVELOADT`, les vélos **adultes** :
    #    `V1_JNBVELOENF` n'est jamais chargée, soit 25 % du parc ignoré et 4,2 % des
    #    ménages classés « aucun vélo » alors qu'ils n'ont que des vélos d'enfants.
    # 3. 14,8 % est la part des **ménages équipés** possédant un VAE (8 % / 54 %), pas
    #    la part de VAE **du parc**, qui vaut 7,7 % (`ML21 / M21`). D'où 1,7× trop de VAE.
    #
    # Ce qui le remplace : les trois étages de `llm_module.core.bike_ownership`, appris
    # sur EMC² 2023 pour l'aire toulousaine — `k` tiré par ménage puis attribué
    # nominativement par tirage sans remise pondéré, VAE tiré par vélo. Le nombre de
    # vélos cesse d'être indépendant du foyer qui le reçoit, et c'est tout l'objet du
    # ticket. `number_of_bikes` reste calculé au-dessus pour `bike_availability`, que
    # MATSim consomme ; il ne détermine plus `personal_bike`.
    #
    # Le foyer existe nativement ici (`household_id`) : contrairement au
    # post-traitement de la voie 1, il n'y a AUCUNE clé de ménage à reconstruire à
    # l'adresse, donc ni collision à scinder ni membre absent à compléter. C'est
    # l'avantage de fermer la cause plutôt que de corriger la surface.
    #
    # ⚠ NON REJOUÉ. Les données sources sont bien là (`eqasim-toulouse/data`, montée
    # dans le conteneur), mais la chaîne complète n'a pas été régénérée après ce
    # correctif : il faut `docker compose build eqasim` (le stage importe désormais
    # `llm_module`, monté depuis `docker-compose.yml`) puis rejouer le notebook de
    # génération. Le mécanisme lui-même est celui de la voie 1, validé sur
    # `toulouse_population_1000.json` (cf. `docs/arch/velo-equipement.md`).
    df_population["personal_bike"] = _assign_personal_bike(context, df_population)

    return df_population


# Recodage `professional_activity` → `main_occupation` du persona. **Doit rester
# identique à `_MAIN_OCCUPATION_FR` de `llm_agents.py`**, qui écrit le trait dans le
# JSON : c'est la même variable, lue par l'étage 2 ici et affichée là-bas.
_MAIN_OCCUPATION_FR = {
    "full_time_worker": "Travail à plein temps",
    "part_time_worker": "Travail à temps partiel",
    "unemployed":       "Chômeur/recherche d'emploi",
    "retired":          "Retraité",
    "homemaker":        "Personne au foyer",
    "other":            "Personne au foyer",
}


def _main_occupation_fr(row):
    activity = str(row.get("professional_activity", ""))
    if activity == "student":
        return "Scolaire (jusqu'au Bac)" if int(row["age"]) < 18 else "Étudiant"
    if activity == "under14":
        return "Scolaire (jusqu'au Bac)"
    return _MAIN_OCCUPATION_FR.get(activity, "")


def _assign_personal_bike(context, df_population):
    """`personal_bike` pour chaque personne, par les trois étages du ticket 015.

    Le tirage est **déterministe par hachage** (adresse du domicile pour le stock,
    adresse + index de personne pour l'attribution et le type), comme `housing_type` :
    il ne consomme donc pas `random_seed`, et deux générations donnent le même parc.
    """
    from llm_module.core.bike_ownership import (
        MIN_AGE_ELIGIBLE, NO_BIKE, BikeOwnershipModel, Member, address_key, assign,
        bike_label,
    )
    from llm_module.core.zone_resolver import ZoneResolver

    # Absence de ressource = erreur explicite. Retomber sur l'ancienne imputation
    # produirait une population dont le gradient est faux SANS que rien ne le signale,
    # ce qui est exactement le scénario que le ticket ferme.
    model = BikeOwnershipModel.load()
    resolver = ZoneResolver.load()
    electric_p = model.electric_p

    # Coordonnées du domicile, par ménage. La couche de zones fines est en WGS84
    # lon/lat en entrée ; les localisations d'eqasim sont en Lambert 93.
    df_homes = context.stage("synthesis.population.spatial.home.locations")
    homes = df_homes[["household_id", "geometry"]].drop_duplicates("household_id")
    homes = homes.to_crs("EPSG:4326")
    coordinates = {
        household_id: (float(point.y), float(point.x))
        for household_id, point in zip(homes["household_id"], homes["geometry"])
    }

    # `main_occupation` est une covariable de l'étage 2, et le persona la portera. Elle
    # est recalculée ici avec EXACTEMENT le recodage de `llm_agents.py` (dont ce bloc est
    # la source amont) : une propension calculée sur une autre occupation que celle que
    # le JSON affiche donnerait deux vérités pour le même agent.
    occupations = df_population.apply(_main_occupation_fr, axis=1)

    labels = pd.Series(NO_BIKE, index=df_population.index, dtype=object)
    missing_home = 0
    unusable_law = 0
    for household_id, group in df_population.groupby("household_id", sort=False):
        home = coordinates.get(household_id)
        if home is None:
            missing_home += len(group)
            continue
        latitude, longitude = home
        zone = resolver.resolve(latitude, longitude)
        if zone is None:
            # Hors périmètre d'enquête : on ne devine pas. Le trait reste « Pas de
            # vélo » plutôt qu'un tirage dans une loi qu'on n'a pas.
            missing_home += len(group)
            continue

        key = address_key(latitude, longitude)
        size = int(group["household_size"].iloc[0])
        stock = model.draw_stock(
            household_size=size,
            number_of_cars=group["number_of_cars"].iloc[0],
            density_hh_km2=zone.density_hh_km2,
            dist_center_km=zone.dist_center_km,
            household_key=key,
        )
        # `not stock` confondrait deux cas très différents, et l'un des deux est un
        # échec silencieux : `0` est un ménage qui n'a légitimement aucun vélo, `None`
        # est une loi inutilisable dont on ne sait RIEN tirer. Le second doit se compter
        # et sonner, sinon un modèle dégénéré produit une population intégralement
        # « Pas de vélo » — une valeur parfaitement plausible, donc indétectable.
        if stock is None:
            unusable_law += len(group)
            continue
        if stock == 0:
            continue

        members = [
            Member(index=index,
                   propensity=model.propensity_of(
                       k=stock, household_size=size, age=age,
                       gender="Female" if sex == "female" else "Male",
                       main_occupation=occupation,
                       density_hh_km2=zone.density_hh_km2,
                       dist_center_km=zone.dist_center_km),
                   eligible=bool(age is not None and age >= MIN_AGE_ELIGIBLE))
            for index, age, sex, occupation in zip(
                group.index, group["age"], group["sex"], occupations.loc[group.index])
        ]
        for index in assign(members, stock, key):
            labels.loc[index] = bike_label(
                key, index, df_population.at[index, "age"], electric_p)

    if missing_home:
        print(f"[ALARME] personal_bike : {missing_home} personnes sans domicile "
              f"rattachable à une zone fine EMC² — traitées SANS vélo. Au-delà de "
              f"quelques pourcents, c'est le périmètre de la couche ou celui de la "
              f"population qui a changé.")
    if unusable_law:
        # Ce cas ne devrait jamais se produire avec une ressource exportée : il signale
        # un modèle corrompu, pas une population atypique.
        raise RuntimeError(
            f"[ALARME] personal_bike : loi de k inutilisable pour {unusable_law} "
            f"personnes (softmax dégénéré). La ressource bike_ownership.json est "
            f"corrompue — ré-exportez-la (make bike-ownership). On refuse de produire "
            f"une population intégralement « Pas de vélo », qui passerait pour valide.")
    return labels

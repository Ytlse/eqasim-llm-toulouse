import hashlib
import json
import math
import os
import uuid
import urllib.request

import numpy as np
import pandas as pd
import geopandas as gpd
from faker import Faker
from shapely.geometry import Point, box
from shapely.ops import nearest_points
import shapely.wkt

"""
Export the synthetic population in the JSON format used by the LLM-agents GAMA simulation.
"""

# Fixed namespace for deterministic UUID generation
_UUID_NAMESPACE = uuid.UUID("e4a51200-0000-0000-0000-000000000000")

_fake = Faker("fr_FR")

# Socioprofessional class (INSEE PCS-2020 compatible, 8-class)
_SPC_LABEL = {
    1: "Farmer",
    2: "Craftsperson or Shop Owner",
    3: "Executive or Higher Intellectual Professional",
    4: "Intermediate Professional",
    5: "Employee",
    6: "Manual Worker",
    7: "Retired",
    8: "Other Inactive",
}

_SPC_OCCUPATION_TITLE = {
    1: "Farmer",
    2: "Shop Owner",
    3: "Executive",
    4: "Technician",
    5: "Office Worker",
    6: "Factory Worker",
    7: "Retired",
    8: "Without Occupation",
}

# Employment sector → organization type
_SECTOR_ORGANIZATION = {
    "agriculture": "Agricultural Business",
    "energy_water_waste_mining": "Utilities Company",
    "food_beverages_tobacco": "Food & Beverage Company",
    "refining": "Petrochemical Company",
    "electrical_electronics_ict_machinery": "Technology Company",
    "transport_equipment": "Automotive Manufacturer",
    "other_manufacturing": "Manufacturing Company",
    "construction": "Construction Company",
    "retail_auto": "Retail Chain",
    "transport_storage": "Logistics Company",
    "accommodation_food_services": "Hospitality Business",
    "information_communication": "Media & Communications Company",
    "finance_insurance": "Financial Institution",
    "real_estate": "Real Estate Agency",
    "scientific_technical_support_services": "Consulting Firm",
    "public_admin_education_health": "Public Institution",
    "arts_recreation_other_services": "Cultural Organization",
    "not_applicable": "",
}

# Household monthly income (€) → text label
_INCOME_THRESHOLDS = [
    (1500,  "Very Low"),
    (2500,  "Low"),
    (3500,  "Medium"),
    (5000,  "Medium-High"),
    (float("inf"), "High"),
]

# Professional activity → readable label
_PROFESSIONAL_ACTIVITY_LABEL = {
    "full_time_worker":  "Full-Time Worker",
    "part_time_worker":  "Part-Time Worker",
    "unemployed":        "Unemployed",
    "retired":           "Retired",
    "student":           "Student",
    "under14":           "Child (under 14)",
    "homemaker":         "Homemaker",
    "other":             "Other Inactive",
}

# Professional activity → French calibration label (7 categories)
_MAIN_OCCUPATION_FR = {
    "full_time_worker": "Travail à plein temps",
    "part_time_worker": "Travail à temps partiel",
    "unemployed":       "Chômeur/recherche d'emploi",
    "retired":          "Retraité",
    "homemaker":        "Personne au foyer",
    "other":            "Personne au foyer",
}

# Activity purpose → French calibration label
_PURPOSE_FR = {
    "work":      "Travail",
    "education": "Etude",
    "shop":      "Achats",
}


def _stable_hash(value: int) -> int:
    """Deterministic integer hash independent of PYTHONHASHSEED."""
    return int(hashlib.md5(str(value).encode()).hexdigest(), 16)


def _fetch_otp_polygon(otp_endpoint: str):
    """Fetch OTP graph coverage polygon. Returns None if unavailable.

    Tries OTP1 REST API first, then OTP2 GraphQL (stops convex hull).
    """
    # OTP1: /otp/routers/default returns polygon or boundingBox directly.
    try:
        url = f"{otp_endpoint}/otp/routers/default"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
        raw = data.get("polygon")
        if isinstance(raw, str):
            return shapely.wkt.loads(raw)
        if isinstance(raw, dict):
            from shapely.geometry import shape
            return shape(raw)
        bb = data.get("boundingBox", {})
        if bb:
            return box(bb["minLon"], bb["minLat"], bb["maxLon"], bb["maxLat"])
    except Exception:
        pass

    # OTP2: query stops via GraphQL and return their convex hull.
    graphql_url = f"{otp_endpoint}/otp/transmodel/v3"
    query = "{ stopPlaces { geometry { type coordinates } } }"
    try:
        req = urllib.request.Request(
            graphql_url,
            data=json.dumps({"query": query}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        stops = (data.get("data") or {}).get("stopPlaces") or []
        coords = []
        for sp in stops:
            geom = sp.get("geometry") or {}
            if geom.get("type") == "Point" and geom.get("coordinates"):
                lon, lat = geom["coordinates"]
                coords.append((lon, lat))
        if len(coords) >= 3:
            from shapely.geometry import MultiPoint
            polygon = MultiPoint(coords).convex_hull.buffer(0.02)
            print(f"[llm_agents] OTP2 polygon built from {len(coords)} stops (convex hull + 0.02° buffer)")
            return polygon
        if coords:
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            return box(min(lons) - 0.02, min(lats) - 0.02, max(lons) + 0.02, max(lats) + 0.02)
    except Exception as e:
        print(f"[llm_agents] Warning: could not fetch OTP polygon from {otp_endpoint}: {e}")
    return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _snap_to_polygon(lon: float, lat: float, polygon) -> tuple[float, float, float]:
    """Return (snapped_lon, snapped_lat, extra_distance_m). Zero distance if inside."""
    pt = Point(lon, lat)
    if polygon.contains(pt):
        return lon, lat, 0.0
    snapped = nearest_points(polygon.exterior, pt)[0]
    dist_m = _haversine_m(lat, lon, snapped.y, snapped.x)
    return snapped.x, snapped.y, dist_m


def _generate_name(person_id: int, sex: str) -> str:
    if sex == "male":
        return _fake.name_male()
    elif sex == "female":
        return _fake.name_female()
    return _fake.name()


def _income_label(income) -> str:
    if income is None or (isinstance(income, float) and np.isnan(income)):
        return ""
    for threshold, label in _INCOME_THRESHOLDS:
        if income < threshold:
            return label
    return "High"


_PERSONALITY_TEMPLATES_PATH = os.path.join(
    os.path.dirname(__file__), "personality_templates.json"
)
_personality_templates: list | None = None


def _load_personality_templates() -> list:
    global _personality_templates
    if _personality_templates is None:
        with open(_PERSONALITY_TEMPLATES_PATH, encoding="utf-8") as f:
            _personality_templates = json.load(f)
    return _personality_templates


def _pick_personality(person_id: int) -> dict:
    """Deterministically pick a personality template for a given person_id."""
    templates = _load_personality_templates()
    return templates[_stable_hash(person_id) % len(templates)]


def configure(context):
    context.stage("synthesis.population.enriched")
    context.stage("synthesis.population.activities")
    context.stage("synthesis.population.spatial.locations")
    context.config("output_path")
    context.config("output_prefix", "ile_de_france_")
    context.config("generate_personality_traits", False)


def execute(context):
    output_path = context.config("output_path")
    output_prefix = context.config("output_prefix")
    generate_personality_traits = context.config("generate_personality_traits")

    otp_endpoint = os.environ.get("OTP_ENDPOINT", "")
    otp_polygon = _fetch_otp_polygon(otp_endpoint) if otp_endpoint else None
    if otp_polygon is None:
        print("[llm_agents] OTP polygon unavailable — out-of-graph detection skipped")

    # ── Persons ────────────────────────────────────────────────────────────────
    df_persons = context.stage("synthesis.population.enriched")

    # ── Activities ─────────────────────────────────────────────────────────────
    df_activities = context.stage("synthesis.population.activities")[
        ["person_id", "activity_index", "purpose", "start_time", "end_time", "is_first", "is_last"]
    ]

    # ── Spatial locations (EPSG:2154 → WGS84) ─────────────────────────────────
    df_locations = context.stage("synthesis.population.spatial.locations")[
        ["person_id", "activity_index", "geometry"]
    ]
    df_locations = gpd.GeoDataFrame(df_locations).to_crs("EPSG:4326")
    df_locations["lon"] = df_locations.geometry.x
    df_locations["lat"] = df_locations.geometry.y

    # Merge locations into activities
    df_activities = pd.merge(
        df_activities,
        df_locations[["person_id", "activity_index", "lon", "lat"]],
        on=["person_id", "activity_index"],
        how="left",
    )

    # Group activities by person for fast lookup
    activities_by_person = {
        pid: grp.sort_values("activity_index")
        for pid, grp in df_activities.groupby("person_id")
    }

    # ── Build JSON ─────────────────────────────────────────────────────────────
    result = []

    for _, row in df_persons.iterrows():
        pid = int(row["person_id"])
        sex = str(row["sex"])
        name = _generate_name(pid, sex)

        spc = int(row["socioprofessional_class"]) if not pd.isna(row.get("socioprofessional_class")) else 8
        sector = str(row.get("employment_sector", "not_applicable"))
        income = row.get("household_income")

        occ_title = _SPC_OCCUPATION_TITLE.get(spc, "")
        occ_org = _SECTOR_ORGANIZATION.get(sector, "")
        spc_label = _SPC_LABEL.get(spc, "")
        income_text = _income_label(income)
        pro_act = str(row.get("professional_activity", ""))
        pro_act_label = _PROFESSIONAL_ACTIVITY_LABEL.get(pro_act, pro_act)

        # ── Activities list ────────────────────────────────────────────────────
        raw_acts = activities_by_person.get(pid, pd.DataFrame())

        # Skip persons with no activity other than home
        if raw_acts.empty or raw_acts[raw_acts["purpose"] != "home"].empty:
            continue

        activities_list = []
        home_location = None

        if not raw_acts.empty:
            acts = raw_acts.copy().reset_index(drop=True)

            # 1. Resolve home coordinates strictly from existing home activities.
            #    home_location is the single source of truth: it feeds both identity.home
            #    and the coordinates of any synthetic home activity added below.
            home_rows = acts[acts["purpose"] == "home"]
            if not home_rows.empty:
                _h = home_rows.iloc[0]
                home_lon = None if pd.isna(_h["lon"]) else float(_h["lon"])
                home_lat = None if pd.isna(_h["lat"]) else float(_h["lat"])
                home_location = {"lon": home_lon, "lat": home_lat} if home_lon is not None else None
            else:
                home_lon = None
                home_lat = None

            # 2. Ensure first activity is home
            if acts.iloc[0]["purpose"] != "home":
                first_st = acts.iloc[0]["start_time"]
                home_end = float(first_st) if not pd.isna(first_st) else 0.0
                acts.at[0, "start_time"] = home_end
                acts.at[0, "is_first"] = False
                prepend = pd.DataFrame([{
                    "person_id": pid,
                    "activity_index": int(acts["activity_index"].min()) - 1,
                    "purpose": "home",
                    "start_time": np.nan,
                    "end_time": home_end,
                    "is_first": True,
                    "is_last": False,
                    "lon": home_lon,
                    "lat": home_lat,
                }])
                acts = pd.concat([prepend, acts], ignore_index=True)

            # 3. Ensure last activity is home
            if acts.iloc[-1]["purpose"] != "home":
                last_et = acts.iloc[-1]["end_time"]
                home_start = float(last_et) if not pd.isna(last_et) else 86400.0
                acts.at[len(acts) - 1, "end_time"] = home_start
                acts.at[len(acts) - 1, "is_last"] = False
                append_df = pd.DataFrame([{
                    "person_id": pid,
                    "activity_index": int(acts["activity_index"].max()) + 1,
                    "purpose": "home",
                    "start_time": home_start,
                    "end_time": np.nan,
                    "is_first": False,
                    "is_last": True,
                    "lon": home_lon,
                    "lat": home_lat,
                }])
                acts = pd.concat([acts, append_df], ignore_index=True)

            # 4. Enforce spatial continuity: first and last share home (lat, lon)
            if home_lon is not None:
                acts.at[0, "lon"] = home_lon
                acts.at[0, "lat"] = home_lat
                acts.at[len(acts) - 1, "lon"] = home_lon
                acts.at[len(acts) - 1, "lat"] = home_lat

            # 4.5. Merge consecutive activities with identical purpose and location.
            # Avoids routing requests where origin == destination (e.g. two consecutive
            # work sessions at the same workplace, or duplicate home entries).
            _acts_list = acts.to_dict("records")
            _merged: list[dict] = []
            i = 0
            while i < len(_acts_list):
                cur = dict(_acts_list[i])
                j = i + 1
                while j < len(_acts_list):
                    nxt = _acts_list[j]
                    if nxt["purpose"] != cur["purpose"]:
                        break
                    cur_lon, cur_lat = cur.get("lon"), cur.get("lat")
                    nxt_lon, nxt_lat = nxt.get("lon"), nxt.get("lat")
                    both_null = (cur_lon is None or pd.isna(cur_lon)) and (nxt_lon is None or pd.isna(nxt_lon))
                    both_close = (
                        cur_lon is not None and not pd.isna(cur_lon) and
                        nxt_lon is not None and not pd.isna(nxt_lon) and
                        abs(float(cur_lon) - float(nxt_lon)) < 1e-5 and
                        abs(float(cur_lat) - float(nxt_lat)) < 1e-5
                    )
                    if not (both_null or both_close):
                        break
                    cur["end_time"] = nxt["end_time"]
                    cur["is_last"] = nxt["is_last"]
                    j += 1
                _merged.append(cur)
                i = j
            if len(_merged) < len(_acts_list):
                print(f"[llm_agents] person={pid}: merged {len(_acts_list) - len(_merged)} duplicate activity pair(s) ({len(_acts_list)} → {len(_merged)})")
            acts = pd.DataFrame(_merged).reset_index(drop=True)

            # 5. Build activities list (-1.0 strictly for first/last activity only)
            for _, act in acts.iterrows():
                is_first_act = bool(act["is_first"])
                is_last_act  = bool(act["is_last"])
                st = act["start_time"]
                et = act["end_time"]

                start_time = -1.0 if is_first_act else float(st)
                end_time   = -1.0 if is_last_act  else float(et)

                lon_v = None if pd.isna(act["lon"]) else float(act["lon"])
                lat_v = None if pd.isna(act["lat"]) else float(act["lat"])

                act_entry = {
                    "id": str(uuid.uuid5(_UUID_NAMESPACE, f"{pid}_{int(act['activity_index'])}")),
                    "scheduled_start_time": None,
                    "start_time": start_time,
                    "end_time": end_time,
                    "purpose": str(act["purpose"]),
                    "location": {"lon": lon_v, "lat": lat_v}
                }
                activities_list.append(act_entry)

            # Update home_location from the snapped first home activity so that
            # identity.home reflects the post-snap coordinates (used as last_location at init).
            if otp_polygon is not None and home_location is not None:
                first_home = next(
                    (a for a in activities_list if a["purpose"] == "home" and a["location"]["lon"] is not None),
                    None,
                )
                if first_home:
                    home_location = {"lon": first_home["location"]["lon"], "lat": first_home["location"]["lat"]}

        age_val = int(row["age"])
        age_bracket = f"{(age_val // 5) * 5}-{(age_val // 5) * 5 + 4}"

        if pro_act == "student":
            main_occupation_fr = "Scolaire (jusqu'au Bac)" if age_val < 18 else "Étudiant"
        elif pro_act == "under14":
            main_occupation_fr = "Scolaire (jusqu'au Bac)"
        else:
            main_occupation_fr = _MAIN_OCCUPATION_FR.get(pro_act, "")

        travel_purposes = list({
            _PURPOSE_FR[act["purpose"]]
            for act in activities_list
            if act["purpose"] in _PURPOSE_FR
        })

        traits = {
            "name": name,
            "age": age_val,
            "age_bracket": age_bracket,
            "gender": "Male" if sex == "male" else "Female",
            "main_occupation": main_occupation_fr,
            "travel_purposes": travel_purposes,
            "occupation": {
                "title": occ_title,
                "organization": occ_org,
            },
            # Additional demographic fields
            "household_size": int(row["household_size"]),
            "income": income_text,
            "socioprofessional_class": spc_label,
            "professional_activity": pro_act_label,
            "employment_sector": sector if sector != "not_applicable" else "",
            "car_availability": str(row.get("car_availability", "")),
            "has_driving_license": bool(row.get("has_license", False)),
            "has_pt_subscription": bool(row.get("has_pt_subscription", False)),
            "number_of_cars": int(row.get("number_of_cars", 0)),
            "employed": bool(row.get("employed", False)),
            "studies": bool(row.get("studies", False)),
        }

        if generate_personality_traits:
            persona = _pick_personality(pid)
            p = persona.get("personality", {})
            traits["style"] = persona.get("style", "")
            traits["personality"] = {
                "traits": p.get("traits", []),
                "big_five": p.get("big_five", {}),
            }

        entry = {
            "person_id": str(pid),
            "identity": {
                "name": name,
                "traits_json": traits,
                "home": home_location,
                "activities": activities_list,
            },
            "state": {
                "last_location": None,
                "last_activity_index": 0,
                "cache_current_activity": None,
                "heading_to": None,
                "scheduling_in_progress": False,
                "scheduling_started_at": None,
            },
            "is_llm_based": True,
        }
        result.append(entry)

    n = len(result)
    output_file = os.path.join(output_path, f"{output_prefix}population_{n}.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=4)

    print(f"Exported {n} agents to {output_file}")

    # Sanity check: no two consecutive activities at the same location.
    violations = []
    for entry in result:
        acts = entry["identity"]["activities"]
        for i in range(len(acts) - 1):
            a, b = acts[i], acts[i + 1]
            la, lb = a.get("location") or {}, b.get("location") or {}
            lon_a, lat_a = la.get("lon"), la.get("lat")
            lon_b, lat_b = lb.get("lon"), lb.get("lat")
            if (lon_a is not None and lon_b is not None and
                    abs(lon_a - lon_b) < 1e-5 and abs(lat_a - lat_b) < 1e-5):
                violations.append(
                    f"  person={entry['person_id']} idx={i}→{i+1} "
                    f"{a['purpose']}→{b['purpose']} @ ({lat_a:.5f},{lon_a:.5f})"
                )
    if violations:
        print(f"[llm_agents] WARNING: {len(violations)} consecutive same-location activity pair(s):")
        for v in violations[:10]:
            print(v)
        if len(violations) > 10:
            print(f"  ... and {len(violations) - 10} more")
        raise AssertionError(
            f"{len(violations)} consecutive activities share the same location — merge step failed"
        )
    print(f"[llm_agents] Sanity check passed: no consecutive same-location activities")

    return n

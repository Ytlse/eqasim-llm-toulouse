from tqdm import tqdm
import pandas as pd
import numpy as np

"""
This stage filters out census observations which live or work outside of
Île-de-France.
"""

def configure(context):
    context.stage("data.census.cleaned")
    context.stage("data.spatial.codes")

def execute(context):
    df = context.stage("data.census.cleaned")

    # Filter requested codes
    df_codes = context.stage("data.spatial.codes")

    requested_communes = set(df_codes["commune_id"].unique())
    df = df[df["commune_id"].isin(requested_communes) | (df["commune_id"] == "undefined")]

    excess_iris = set(df["iris_id"].unique()) - set(df_codes["iris_id"].unique())
    if not excess_iris == {"undefined"}:
        raise RuntimeError("Found additional IRIS: %s" % excess_iris)

    return df

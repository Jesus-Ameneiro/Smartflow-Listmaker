"""
Smartflow Batch Selector — Processor
Data loading, summarizing, filtering logic.
"""

import re
import zipfile
from datetime import datetime
from io import BytesIO

import pandas as pd
from dateutil.relativedelta import relativedelta


# ── File Loading ──────────────────────────────────────────────────────────────

def load_file(uploaded_file):
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        return pd.read_csv(uploaded_file)
    elif name.endswith((".xlsx", ".xls")):
        return pd.read_excel(uploaded_file)
    else:
        raise ValueError("Unsupported file type. Please upload a .csv or .xlsx file.")


# ── Tag Utilities ─────────────────────────────────────────────────────────────

def parse_tags(tag_str):
    if pd.isna(tag_str) or str(tag_str).strip() == "":
        return []
    return [t.strip() for t in str(tag_str).split(",") if t.strip()]


def extract_all_tags(df):
    tags = set()
    for val in df.get("Tags", []):
        for t in parse_tags(val):
            tags.add(t)
    return sorted(tags)


def _lower_map(known_countries):
    return {c.lower(): c for c in known_countries}


def country_distribution(df, known_countries):
    lm = _lower_map(known_countries)
    counts = {}
    for val in df.get("Tags", []):
        seen = set()
        for t in parse_tags(val):
            c = lm.get(t.lower())
            if c and c not in seen:
                counts[c] = counts.get(c, 0) + 1
                seen.add(c)
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def special_tag_distribution(df, known_countries):
    lm = _lower_map(known_countries)
    counts = {}
    for val in df.get("Tags", []):
        seen = set()
        for t in parse_tags(val):
            if t.lower() == "latam":
                continue
            if t.lower() not in lm and t not in seen:
                counts[t] = counts.get(t, 0) + 1
                seen.add(t)
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ── Summary Helpers ───────────────────────────────────────────────────────────

def detect_dataset_type(df):
    col = "Investigation Status"
    if col not in df.columns:
        return "uninvestigated"
    values = df[col].dropna()
    if values.empty:
        return "uninvestigated"
    if values.str.lower().str.contains("disqualified").any():
        return "disqualified"
    return "uninvestigated"


def cases_per_year(df):
    if "Last Event" not in df.columns:
        return {}
    years = pd.to_datetime(df["Last Event"], errors="coerce").dt.year
    return years.value_counts().sort_index(ascending=False).to_dict()


def count_outdated(df, months_threshold, today):
    if "Updated" not in df.columns:
        return 0
    cutoff = today - relativedelta(months=int(months_threshold))
    updated = pd.to_datetime(df["Updated"], errors="coerce")
    return int((updated < cutoff).sum())


def get_unexpected_value_cases(df, columns_must_be_empty, dataset_type):
    cols = list(columns_must_be_empty)
    if dataset_type == "disqualified":
        cols = [c for c in cols if c != "Investigation Status"]
    result = {}
    for col in cols:
        if col not in df.columns:
            continue
        mask = df[col].notna() & (df[col].astype(str).str.strip() != "")
        flagged = df.loc[mask, ["External Case ID", "Name", col]].copy()
        if len(flagged) > 0:
            result[col] = flagged.reset_index(drop=True)
    return result


def generate_summary(df, config, outdated_months, today):
    known = config["known_countries"]
    ds_type = detect_dataset_type(df)
    return {
        "total_cases":       len(df),
        "dataset_type":      ds_type,
        "country_dist":      country_distribution(df, known),
        "special_tags":      special_tag_distribution(df, known),
        "cases_per_year":    cases_per_year(df),
        "outdated_count":    count_outdated(df, outdated_months, today),
        "unexpected_values": get_unexpected_value_cases(
                                 df, config["columns_must_be_empty"], ds_type
                             ),
    }


# ── Filtering ─────────────────────────────────────────────────────────────────

def _has_any_tag(tag_str, tag_set):
    return any(t in tag_set for t in parse_tags(tag_str))


def apply_filters(df, config, filters):
    lm = _lower_map(config["known_countries"])
    result = df.copy()

    min_m = int(filters.get("min_machines", 3))
    max_m = filters.get("max_machines")
    if max_m is not None:
        max_m = int(max_m)
        result = result[
            (result["# Total Machines"] >= min_m) &
            (result["# Total Machines"] <= max_m)
        ]
    else:
        result = result[result["# Total Machines"] >= min_m]

    cutoff = filters.get("last_event_cutoff")
    if cutoff is not None:
        result["_le"] = pd.to_datetime(result["Last Event"], errors="coerce")
        result = result[result["_le"] >= pd.Timestamp(cutoff)]

    inc = set(filters.get("include_tags", []))
    if inc:
        result = result[result["Tags"].apply(lambda v: _has_any_tag(v, inc))]

    exc = set(filters.get("exclude_tags", []))
    if exc:
        result = result[~result["Tags"].apply(lambda v: _has_any_tag(v, exc))]

    c_filter = [c.lower() for c in filters.get("country_filter", [])]
    if c_filter:
        result = result[result["Tags"].apply(
            lambda v: any(t.lower() in c_filter for t in parse_tags(v))
        )]

    result["_upd"] = pd.to_datetime(result["Updated"], errors="coerce")
    mode = filters.get("sort_updated", "newest")

    if mode == "oldest":
        result = result.sort_values("_upd", ascending=True)
    elif mode == "newest":
        result = result.sort_values("_upd", ascending=False)
    elif mode == "mixed":
        result = result.sort_values("_upd", ascending=True).reset_index(drop=True)
        n = len(result)
        idx, lo, hi, flag = [], 0, n - 1, True
        while lo <= hi:
            idx.append(lo if flag else hi)
            lo, hi = (lo + 1, hi) if flag else (lo, hi - 1)
            flag = not flag
        result = result.iloc[idx]

    result = result.drop(columns=[c for c in ["_le", "_upd"] if c in result.columns])
    return result.reset_index(drop=True)


# ── ZIP Parsing ───────────────────────────────────────────────────────────────

_EID_RE = re.compile(r"\d+#\d+")


def extract_ids_from_zip(zip_bytes):
    """Extract External Case IDs from filenames inside a ZIP."""
    ids = set()
    with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
        for name in zf.namelist():
            basename = name.split("/")[-1]
            ids.update(_EID_RE.findall(basename))
    return ids

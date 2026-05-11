"""
Smartflow Batch Selector — Processor
All data loading, summarizing, filtering, and GitHub I/O logic.
"""

import io
import base64
from datetime import datetime

import pandas as pd
import requests
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
    """Split a tag cell into a clean list of individual tags."""
    if pd.isna(tag_str) or str(tag_str).strip() == "":
        return []
    return [t.strip() for t in str(tag_str).split(",") if t.strip()]


def extract_all_tags(df):
    """Return a sorted list of all unique tags in the dataset."""
    tags = set()
    for val in df.get("Tags", []):
        for t in parse_tags(val):
            tags.add(t)
    return sorted(tags)


def _known_lower_map(known_countries):
    return {c.lower(): c for c in known_countries}


def country_distribution(df, known_countries):
    """Count cases per country identified from the Tags column."""
    lower_map = _known_lower_map(known_countries)
    counts = {}
    for val in df.get("Tags", []):
        seen = set()
        for t in parse_tags(val):
            canonical = lower_map.get(t.lower())
            if canonical and canonical not in seen:
                counts[canonical] = counts.get(canonical, 0) + 1
                seen.add(canonical)
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def special_tag_distribution(df, known_countries):
    """Count cases with non-country, non-LATAM special tags."""
    lower_map = _known_lower_map(known_countries)
    counts = {}
    for val in df.get("Tags", []):
        seen = set()
        for t in parse_tags(val):
            if t.lower() == "latam":
                continue
            if t.lower() not in lower_map and t not in seen:
                counts[t] = counts.get(t, 0) + 1
                seen.add(t)
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ── Summary Helpers ───────────────────────────────────────────────────────────

def detect_dataset_type(df):
    """Return 'disqualified' or 'uninvestigated' based on Investigation Status."""
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
    """Return a dict of {year: count} based on the Last Event column."""
    if "Last Event" not in df.columns:
        return {}
    years = pd.to_datetime(df["Last Event"], errors="coerce").dt.year
    return (
        years.value_counts()
        .sort_index(ascending=False)
        .to_dict()
    )


def count_outdated(df, months_threshold, today):
    """Count cases where Updated is older than X months from today."""
    if "Updated" not in df.columns:
        return 0
    cutoff = today - relativedelta(months=int(months_threshold))
    updated = pd.to_datetime(df["Updated"], errors="coerce")
    return int((updated < cutoff).sum())


def get_unexpected_value_cases(df, columns_must_be_empty, dataset_type):
    """
    Return a dict of {column: DataFrame} for every monitored column that
    has at least one case with a non-empty value.
    For disqualified datasets, Investigation Status is excluded from the check.
    """
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


def generate_summary(df, config, outdated_months=6, today=None):
    """Compile the full post-upload summary."""
    if today is None:
        today = datetime.today()
    known_countries = config["known_countries"]
    dataset_type = detect_dataset_type(df)
    return {
        "total_cases":        len(df),
        "dataset_type":       dataset_type,
        "country_dist":       country_distribution(df, known_countries),
        "special_tags":       special_tag_distribution(df, known_countries),
        "cases_per_year":     cases_per_year(df),
        "outdated_count":     count_outdated(df, outdated_months, today),
        "unexpected_values":  get_unexpected_value_cases(
                                  df,
                                  config["columns_must_be_empty"],
                                  dataset_type
                              ),
    }


# ── Filtering ─────────────────────────────────────────────────────────────────

def _case_has_any_tag(tag_str, tag_set):
    return any(t in tag_set for t in parse_tags(tag_str))


def apply_filters(df, config, filters):
    """
    Apply all prioritization filters and return the sorted result.
    filters keys:
        min_machines        int
        last_event_cutoff   date | None
        include_tags        list[str]
        exclude_tags        list[str]
        country_filter      list[str]   (empty = all)
        sort_updated        'oldest' | 'newest' | 'mixed'
    """
    result = df.copy()
    known_countries = config["known_countries"]
    lower_map = _known_lower_map(known_countries)

    # 1. Minimum machines
    min_m = int(filters.get("min_machines", 3))
    result = result[result["# Total Machines"] >= min_m]

    # 2. Last event cutoff
    cutoff = filters.get("last_event_cutoff")
    if cutoff is not None:
        result["_le_dt"] = pd.to_datetime(result["Last Event"], errors="coerce")
        result = result[result["_le_dt"] >= pd.Timestamp(cutoff)]

    # 3. Include tags
    inc = set(filters.get("include_tags", []))
    if inc:
        result = result[result["Tags"].apply(lambda v: _case_has_any_tag(v, inc))]

    # 4. Exclude tags
    exc = set(filters.get("exclude_tags", []))
    if exc:
        result = result[~result["Tags"].apply(lambda v: _case_has_any_tag(v, exc))]

    # 5. Country filter
    c_filter = [c.lower() for c in filters.get("country_filter", [])]
    if c_filter:
        def has_country(val):
            return any(t.lower() in c_filter for t in parse_tags(val))
        result = result[result["Tags"].apply(has_country)]

    # 6. Sort by Updated
    result["_upd_dt"] = pd.to_datetime(result["Updated"], errors="coerce")
    sort_mode = filters.get("sort_updated", "newest")

    if sort_mode == "oldest":
        result = result.sort_values("_upd_dt", ascending=True)

    elif sort_mode == "newest":
        result = result.sort_values("_upd_dt", ascending=False)

    elif sort_mode == "mixed":
        # Interleave oldest and newest records alternately
        result = result.sort_values("_upd_dt", ascending=True).reset_index(drop=True)
        n = len(result)
        indices, lo, hi, flag = [], 0, n - 1, True
        while lo <= hi:
            indices.append(lo if flag else hi)
            if flag:
                lo += 1
            else:
                hi -= 1
            flag = not flag
        result = result.iloc[indices]

    # Clean up helper columns
    result = result.drop(columns=[c for c in ["_le_dt", "_upd_dt"] if c in result.columns])
    return result.reset_index(drop=True)


# ── GitHub History ────────────────────────────────────────────────────────────

def _github_headers(token):
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def get_history_files(token, repo, folder="history"):
    """Download all CSV history files from the GitHub repo folder."""
    url = f"https://api.github.com/repos/{repo}/contents/{folder}"
    resp = requests.get(url, headers=_github_headers(token), timeout=15)
    if resp.status_code == 404:
        return []   # folder not created yet — that's fine
    resp.raise_for_status()
    dfs = []
    for entry in resp.json():
        if entry["name"].endswith(".csv"):
            raw = requests.get(entry["download_url"], timeout=15)
            raw.raise_for_status()
            dfs.append(pd.read_csv(io.StringIO(raw.text)))
    return dfs


def validate_against_history(df, history_dfs):
    """Return the set of External Case IDs repeated in any history file."""
    if not history_dfs:
        return set()
    current_ids = set(df["External Case ID"].dropna().astype(str))
    repeated = set()
    for h in history_dfs:
        if "External Case ID" in h.columns:
            repeated |= current_ids & set(h["External Case ID"].dropna().astype(str))
    return repeated


def push_to_github(output_df, token, repo, folder="history"):
    """
    Push the output DataFrame as a timestamped CSV to the GitHub repo.
    Returns (success: bool, filename: str).
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{folder}/batch_{ts}.csv"
    content_b64 = base64.b64encode(output_df.to_csv(index=False).encode()).decode()

    url = f"https://api.github.com/repos/{repo}/contents/{filename}"
    payload = {
        "message": f"Add batch history {ts}",
        "content": content_b64,
    }
    resp = requests.put(url, json=payload, headers=_github_headers(token), timeout=15)
    return resp.status_code == 201, filename

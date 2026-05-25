"""
Smartflow Comparator — Processor
Logic for comparing Smartflow downloads against Pleteo exports.
"""

import re
from datetime import datetime

import pandas as pd
from dateutil.relativedelta import relativedelta


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_machines(val):
    """Parse comma-formatted machine count string to int. Returns None on failure."""
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ── Loaders ───────────────────────────────────────────────────────────────────

def load_smartflow(uploaded_file):
    """
    Load a Smartflow CSV download.
    Returns a cleaned DataFrame with typed columns.
    """
    df = pd.read_csv(uploaded_file, low_memory=False)
    df.columns = [c.strip() for c in df.columns]

    required = {"Case ID", "No. ofMachines", "Last Event", "Country"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Smartflow file is missing columns: {missing}")

    df["_machines"]   = df["No. ofMachines"].apply(_parse_machines)
    df["_last_event"] = pd.to_datetime(
        df["Last Event"].astype(str).str.strip(), format="%d-%b-%y", errors="coerce"
    )
    df["_case_id"]  = df["Case ID"].astype(str).str.strip()
    df["_country"]  = df["Country"].astype(str).str.strip()
    return df


def load_pleteo(uploaded_file):
    """
    Load a Pleteo export (.csv or .xlsx).
    Returns a cleaned DataFrame with typed columns.
    Extracts Investigation Status and Case Investigators when present.
    """
    name = uploaded_file.name.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(uploaded_file)
    elif name.endswith((".xlsx", ".xls")):
        df = pd.read_excel(uploaded_file)
    else:
        raise ValueError("Unsupported Pleteo file type.")

    df.columns = [c.strip() for c in df.columns]

    required = {"External Case ID", "Last Event"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Pleteo file is missing columns: {missing}")

    df["_case_id"]    = df["External Case ID"].astype(str).str.strip()
    df["_last_event"] = pd.to_datetime(
        df["Last Event"].astype(str).str.strip(), errors="coerce"
    )

    machines_col = next(
        (c for c in df.columns if "total machines" in c.lower()), None
    )
    df["_machines"] = (
        pd.to_numeric(df[machines_col], errors="coerce")
        if machines_col else None
    )

    # Investigation Status — normalise: empty string → NaN
    if "Investigation Status" in df.columns:
        df["_inv_status"] = df["Investigation Status"].astype(str).str.strip()
        df["_inv_status"] = df["_inv_status"].replace({"nan": None, "": None})
    else:
        df["_inv_status"] = None

    # Case Investigators — normalise
    if "Case Investigators" in df.columns:
        df["_investigator"] = df["Case Investigators"].astype(str).str.strip()
        df["_investigator"] = df["_investigator"].replace({"nan": None, "": None})
    else:
        df["_investigator"] = None

    return df


# ── Country reports ───────────────────────────────────────────────────────────

def smartflow_country_dist(sf_df):
    """Return {country: count} from Smartflow _country column."""
    counts = sf_df["_country"].value_counts()
    return counts[counts.index != "nan"].to_dict()


def pleteo_country_dist(pl_df, known_countries):
    """Return {country: count} from Pleteo Tags column."""
    lm = {c.lower(): c for c in known_countries}
    counts = {}
    for val in pl_df.get("Tags", []):
        tags = [t.strip() for t in str(val).split(",") if t.strip()] if pd.notna(val) else []
        seen = set()
        for t in tags:
            c = lm.get(t.lower())
            if c and c not in seen:
                counts[c] = counts.get(c, 0) + 1
                seen.add(c)
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


# ── Pleteo Status Report ──────────────────────────────────────────────────────

def pleteo_status_report(pl_df):
    """
    Summarise Investigation Status and Case Investigator presence in the
    Pleteo file. Returns a dict with:
        status_counts       dict  {status_label: count}  (None → 'No Status')
        investigator_counts dict  {investigator_name: count}
        cases_with_status   int
        cases_with_inv      int
    """
    # Status distribution
    status_series = pl_df["_inv_status"].copy()
    status_series = status_series.fillna("No Status")
    status_counts = status_series.value_counts().to_dict()

    # Investigator distribution
    inv_series = pl_df["_investigator"].dropna()
    investigator_counts = inv_series.value_counts().to_dict()

    return {
        "status_counts":     status_counts,
        "investigator_counts": investigator_counts,
        "cases_with_status": int((pl_df["_inv_status"].notna()).sum()),
        "cases_with_inv":    int((pl_df["_investigator"].notna()).sum()),
    }


def outdated_status_report(outd_df, pl_df):
    """
    For the outdated cases pool specifically, return the Investigation Status
    distribution as found in the Pleteo file.
    Returns a dict: {status_label: count}
    """
    pl_lookup = (
        pl_df[["_case_id", "_inv_status", "_investigator"]]
        .drop_duplicates("_case_id")
        .set_index("_case_id")
    )
    statuses = outd_df["_case_id"].map(pl_lookup["_inv_status"]).fillna("No Status")
    return statuses.value_counts().to_dict()


# ── Core comparison ───────────────────────────────────────────────────────────

def find_difference_cases(sf_df, pl_df):
    """
    Cases that exist in Smartflow but NOT in Pleteo.
    Difference cases have no Investigation Status or Investigator by definition.
    Returns a DataFrame of Smartflow rows.
    """
    pleteo_ids = set(pl_df["_case_id"].dropna())
    mask = ~sf_df["_case_id"].isin(pleteo_ids)
    return sf_df[mask].copy().reset_index(drop=True)


def find_outdated_cases(sf_df, pl_df):
    """
    Cases that exist in BOTH files where Smartflow Last Event > Pleteo Last Event.
    Enriched with Pleteo's Last Event, Investigation Status, and Investigator.
    """
    pl_lookup = (
        pl_df[["_case_id", "_last_event", "_inv_status", "_investigator"]]
        .dropna(subset=["_case_id"])
        .drop_duplicates("_case_id")
        .set_index("_case_id")
    )

    common = sf_df[sf_df["_case_id"].isin(pl_lookup.index)].copy()
    common["_pleteo_last_event"] = common["_case_id"].map(pl_lookup["_last_event"])
    common["_inv_status"]        = common["_case_id"].map(pl_lookup["_inv_status"])
    common["_investigator"]      = common["_case_id"].map(pl_lookup["_investigator"])

    # Pandas 3.0+ requires both sides to be datetime64 before comparison
    sf_last = pd.to_datetime(common["_last_event"], errors="coerce")
    pl_last = pd.to_datetime(common["_pleteo_last_event"], errors="coerce")
    mask = sf_last.notna() & pl_last.notna() & (sf_last > pl_last)
    return common[mask].copy().reset_index(drop=True)


def filter_outdated_by_status(outd_df, include_statuses):
    """
    Filter the outdated pool to only include cases whose Pleteo
    Investigation Status is in include_statuses OR is null (no status).
    include_statuses: list of status strings selected by the agent.
                      Pass None or empty list to include everything.
    """
    if not include_statuses:
        return outd_df.copy()

    inc_set = set(include_statuses)
    # Always include cases with no status; include those whose status matches
    mask = outd_df["_inv_status"].isna() | outd_df["_inv_status"].isin(inc_set)
    return outd_df[mask].copy().reset_index(drop=True)


# ── History validation ────────────────────────────────────────────────────────

def validate_comparator_history(case_ids, history_dfs, expiration_months, today):
    """
    Return a dict: case_id → {batch_numbers, stored_at, times_stored}
    Only cases stored within the expiration window are flagged.
    """
    cutoff = today - relativedelta(months=int(expiration_months))
    result = {}

    for batch in history_dfs:
        stored_at = batch.get("stored_at") or batch.get("confirmed_at")
        if stored_at is None or stored_at < cutoff:
            continue
        col = next(
            (c for c in ["Case ID", "case_id", "_case_id"] if c in batch["df"].columns),
            None,
        )
        if col is None:
            continue
        for eid in batch["df"][col].dropna().astype(str):
            if eid in case_ids:
                result.setdefault(eid, []).append(batch)

    summary = {}
    for eid, matched in result.items():
        def _ts(b):
            return b.get("stored_at") or b.get("confirmed_at") or datetime.min
        latest = max(matched, key=_ts)
        summary[eid] = {
            "batch_numbers": sorted({b["number"] for b in matched}),
            "latest_batch":  latest["number"],
            "stored_at":     _ts(latest),
            "times_stored":  len(matched),
        }
    return summary


# ── Output builder ────────────────────────────────────────────────────────────

OUTPUT_COLS = [
    "Case ID", "Organization Name", "Country",
    "No. ofMachines", "Last Event", "Case Status", "Products",
]


def build_output(sf_df, selected_ids):
    """Build the final output DataFrame from a set of selected Case IDs."""
    out = sf_df[sf_df["_case_id"].isin(selected_ids)].copy()
    keep = [c for c in OUTPUT_COLS if c in out.columns]
    return out[keep].reset_index(drop=True)


def select_by_country_distribution(sf_df, country_alloc):
    """
    Given {country: n_cases}, return a set of Case IDs picking up to
    n_cases per country sorted by Last Event descending.
    """
    selected = set()
    for country, n in country_alloc.items():
        pool = sf_df[sf_df["_country"] == country].copy()
        pool = pool.sort_values("_last_event", ascending=False)
        ids  = pool["_case_id"].head(int(n)).tolist()
        selected.update(ids)
    return selected

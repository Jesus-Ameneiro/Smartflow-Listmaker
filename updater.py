"""
Case Update Prioritizer — Core Logic
Flags outdated cases by comparing Smartflow vs Pleteo Last Event dates.
"""

import io

import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta

# Columns loaded from each file — prune everything else to save memory
_SF_COLS = ["Case ID", "Last Event", "Organization Name", "Country",
            "No. ofMachines", "Case Status"]
_PL_COLS = ["External Case ID", "Last Event",
            "Investigation Status", "Case Investigators"]


# ── Flag definitions ──────────────────────────────────────────────────────────

FLAG_ORDER = {
    "🟣 Critical": 0,   # ≥ 12 months
    "🔴 Red":      1,   # ≥ 6 and < 12 months
    "🟠 Orange":   2,   # ≥ 3 and < 6 months
    "🟡 Yellow":   3,   # > 0 and < 3 months
}

FLAG_THRESHOLDS = [
    (12, "🟣 Critical"),
    (6,  "🔴 Red"),
    (3,  "🟠 Orange"),
    (0,  "🟡 Yellow"),
]


def get_flag(sf_date, pl_date):
    """
    Return the priority flag string based on month difference.
    Returns None if up to date (SF ≤ PL).
    """
    sf = pd.to_datetime(sf_date, errors="coerce")
    pl = pd.to_datetime(pl_date, errors="coerce")
    if pd.isna(sf) or pd.isna(pl):
        return None
    if sf <= pl:
        return None
    delta  = relativedelta(sf, pl)
    months = delta.years * 12 + delta.months
    for threshold, label in FLAG_THRESHOLDS:
        if months >= threshold:
            return label
    return "🟡 Yellow"


def months_diff(sf_date, pl_date):
    """Return integer months difference (SF - PL). 0 if SF ≤ PL or dates missing."""
    sf = pd.to_datetime(sf_date, errors="coerce")
    pl = pd.to_datetime(pl_date, errors="coerce")
    if pd.isna(sf) or pd.isna(pl) or sf <= pl:
        return 0
    delta = relativedelta(sf, pl)
    return delta.years * 12 + delta.months


# ── File loaders ──────────────────────────────────────────────────────────────

@st.cache_data(show_spinner=False)
def load_smartflow(file_bytes):
    """Load and prune Smartflow CSV. Cached by file content."""
    available = pd.read_csv(io.BytesIO(file_bytes), nrows=0).columns.tolist()
    use_cols  = [c for c in _SF_COLS if c in available]
    df = pd.read_csv(io.BytesIO(file_bytes), usecols=use_cols, low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    if "Case ID" not in df.columns:
        raise ValueError("Smartflow file must contain a 'Case ID' column.")
    df["_sf_case_id"]    = df["Case ID"].astype(str).str.strip()
    df["_sf_last_event"] = pd.to_datetime(
        df["Last Event"].astype(str).str.strip(), format="%d-%b-%y", errors="coerce"
    )
    return df


@st.cache_data(show_spinner=False)
def load_pleteo(file_bytes, file_name=""):
    """Load and prune Pleteo file. Cached by file content."""
    name = file_name.lower()
    if name.endswith(".csv"):
        available = pd.read_csv(io.BytesIO(file_bytes), nrows=0).columns.tolist()
        use_cols  = [c for c in _PL_COLS if c in available]
        df = pd.read_csv(io.BytesIO(file_bytes), usecols=use_cols)
    elif name.endswith((".xlsx", ".xls")):
        available = pd.read_excel(io.BytesIO(file_bytes), nrows=0).columns.tolist()
        use_cols  = [c for c in _PL_COLS if c in available]
        df = pd.read_excel(io.BytesIO(file_bytes), usecols=use_cols)
    else:
        raise ValueError("Unsupported Pleteo file type.")
    df.columns = [c.strip() for c in df.columns]
    if "External Case ID" not in df.columns:
        raise ValueError("Pleteo file must contain an 'External Case ID' column.")
    df["_pl_case_id"]    = df["External Case ID"].astype(str).str.strip()
    df["_pl_last_event"] = pd.to_datetime(
        df["Last Event"].astype(str).str.strip(), errors="coerce"
    )
    inv_col = "Investigation Status"
    df["_inv_status"] = (
        df[inv_col].astype(str).str.strip().replace({"nan": None, "": None})
        if inv_col in df.columns else None
    )
    inv_col2 = "Case Investigators"
    df["_investigator"] = (
        df[inv_col2].astype(str).str.strip().replace({"nan": None, "": None})
        if inv_col2 in df.columns else None
    )
    return df


# ── Core comparison ───────────────────────────────────────────────────────────

def parse_case_ids(raw_string):
    """Parse a comma-separated string of Case IDs into a deduplicated list."""
    return list(dict.fromkeys(
        x.strip() for x in raw_string.split(",") if x.strip()
    ))


def verify_and_flag(case_ids, sf_df, pl_df):
    """
    For each Case ID:
    - Look up in Pleteo (External Case ID)
    - Look up in Smartflow (Case ID)
    - Compare Last Event dates
    - Assign flag

    Returns a DataFrame with all results including up-to-date and not-found.
    """
    sf_lookup = (
        sf_df[["_sf_case_id", "_sf_last_event",
               "Organization Name", "Country", "No. ofMachines", "Case Status"]]
        .drop_duplicates("_sf_case_id")
        .set_index("_sf_case_id")
    )
    pl_lookup = (
        pl_df[["_pl_case_id", "_pl_last_event", "_inv_status", "_investigator"]]
        .drop_duplicates("_pl_case_id")
        .set_index("_pl_case_id")
    )

    rows = []
    for cid in case_ids:
        in_sf = cid in sf_lookup.index
        in_pl = cid in pl_lookup.index

        sf_le = pd.to_datetime(sf_lookup.loc[cid, "_sf_last_event"], errors="coerce") if in_sf else pd.NaT
        pl_le = pd.to_datetime(pl_lookup.loc[cid, "_pl_last_event"], errors="coerce") if in_pl else pd.NaT

        org        = sf_lookup.loc[cid, "Organization Name"] if in_sf else "—"
        country    = sf_lookup.loc[cid, "Country"]           if in_sf else "—"
        machines   = sf_lookup.loc[cid, "No. ofMachines"]    if in_sf else "—"
        case_status= sf_lookup.loc[cid, "Case Status"]       if in_sf else "—"
        inv_status = pl_lookup.loc[cid, "_inv_status"]       if in_pl else "—"
        investigator = pl_lookup.loc[cid, "_investigator"]   if in_pl else "—"

        if not in_pl and not in_sf:
            status = "❌ Not Found"
            flag   = None
            months = 0
        elif not in_pl:
            status = "⚠️ Not in Pleteo"
            flag   = None
            months = 0
        elif not in_sf:
            status = "⚠️ Not in Smartflow"
            flag   = None
            months = 0
        elif pd.isna(sf_le) or pd.isna(pl_le):
            status = "⚠️ Date missing"
            flag   = None
            months = 0
        else:
            flag   = get_flag(sf_le, pl_le)
            months = months_diff(sf_le, pl_le)
            if flag:
                status = "🔄 Outdated"
            else:
                status = "✅ Up to Date"

        rows.append({
            "External Case ID":     cid,
            "Organization":         org,
            "Country":              country,
            "No. of Machines":      machines,
            "Smartflow Last Event": sf_le.strftime("%Y-%m-%d") if pd.notna(sf_le) else "—",
            "Pleteo Last Event":    pl_le.strftime("%Y-%m-%d") if pd.notna(pl_le) else "—",
            "Months Outdated":      months if months > 0 else "—",
            "Priority":             flag if flag else "—",
            "Status":               status,
            "Investigation Status": inv_status if inv_status else "—",
            "Investigator":         investigator if investigator else "—",
            "Case Status":          case_status,
        })

    return pd.DataFrame(rows)


def get_outdated_only(results_df):
    """Return only outdated rows, sorted by priority (Critical first)."""
    outdated = results_df[results_df["Status"] == "🔄 Outdated"].copy()
    outdated["_sort"] = outdated["Priority"].map(FLAG_ORDER).fillna(99)
    outdated = outdated.sort_values(["_sort", "Months Outdated"], ascending=[True, False])
    return outdated.drop(columns=["_sort"]).reset_index(drop=True)


# ── Output columns ────────────────────────────────────────────────────────────

OUTPUT_COLS = [
    "External Case ID", "Organization", "Country",
    "No. of Machines", "Smartflow Last Event", "Pleteo Last Event",
    "Case Status", "Investigation Status", "Investigator",
]

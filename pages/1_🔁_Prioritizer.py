"""
Case Update Prioritizer
Ruvixx · LATAM Compliance Operations

Identifies outdated cases in Pleteo by comparing Last Event dates
against Smartflow, and flags them by severity for prioritized updating.
"""

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from github_manager import (
    get_all_confirmed_update_ids,
    get_update_history,
    push_update_history,
)
from updater import (
    FLAG_ORDER,
    OUTPUT_COLS,
    get_outdated_only,
    load_pleteo,
    load_smartflow,
    parse_case_ids,
    verify_and_flag,
)

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Case Update Prioritizer",
    page_icon="🔁",
    layout="wide",
)
st.title("🔁 Case Update Prioritizer")
st.caption("Ruvixx · LATAM Compliance Operations")

# ── Credentials ───────────────────────────────────────────────────────────────
def _gh_creds():
    try:
        return st.secrets["GITHUB_TOKEN"], st.secrets["GITHUB_REPO"]
    except KeyError:
        return None, None

def _require_creds():
    token, repo = _gh_creds()
    if not token or not repo:
        st.error("Add **GITHUB_TOKEN** and **GITHUB_REPO** to Streamlit secrets.")
        st.stop()
    return token, repo

# ── Session state ─────────────────────────────────────────────────────────────
for k, v in {
    "_sf_df":          None,
    "_pl_df":          None,
    "_sf_name":        None,
    "_pl_name":        None,
    "_results_df":     None,
    "_outdated_df":    None,
    "_output_df":      None,
    "_confirmed":      False,
    "_history":        None,
    "_confirmed_ids":  None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

MAX_CASES = 100

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — UPLOAD FILES
# ─────────────────────────────────────────────────────────────────────────────
st.header("1. Upload Files")
uc1, uc2 = st.columns(2)
with uc1:
    sf_up = st.file_uploader("Smartflow investigations (.csv)", type=["csv"], key="sf_up")
with uc2:
    pl_up = st.file_uploader("Pleteo case file (.csv or .xlsx)", type=["csv","xlsx"], key="pl_up")

if sf_up and sf_up.name != st.session_state._sf_name:
    try:
        st.session_state._sf_df   = load_smartflow(sf_up)
        st.session_state._sf_name = sf_up.name
        st.session_state._results_df  = None
        st.session_state._outdated_df = None
        st.session_state._output_df   = None
        st.session_state._confirmed   = False
    except Exception as e:
        st.error(f"Smartflow error: {e}")

if pl_up and pl_up.name != st.session_state._pl_name:
    try:
        st.session_state._pl_df   = load_pleteo(pl_up)
        st.session_state._pl_name = pl_up.name
        st.session_state._results_df  = None
        st.session_state._outdated_df = None
        st.session_state._output_df   = None
        st.session_state._confirmed   = False
    except Exception as e:
        st.error(f"Pleteo error: {e}")

sf_df = st.session_state._sf_df
pl_df = st.session_state._pl_df

# File identity cards
if sf_df is not None or pl_df is not None:
    fi1, fi2 = st.columns(2)
    if sf_df is not None:
        sf_le_min = sf_df["_sf_last_event"].min()
        sf_le_max = sf_df["_sf_last_event"].max()
        fi1.success(
            f"📡 **Smartflow** — `{st.session_state._sf_name}`\n\n"
            f"- Cases: **{len(sf_df):,}**\n"
            f"- Last Event range: "
            f"**{sf_le_min.strftime('%Y-%m-%d') if pd.notna(sf_le_min) else '—'}** → "
            f"**{sf_le_max.strftime('%Y-%m-%d') if pd.notna(sf_le_max) else '—'}**"
        )
    if pl_df is not None:
        pl_le_min = pl_df["_pl_last_event"].min()
        pl_le_max = pl_df["_pl_last_event"].max()
        fi2.success(
            f"📋 **Pleteo** — `{st.session_state._pl_name}`\n\n"
            f"- Cases: **{len(pl_df):,}**\n"
            f"- Last Event range: "
            f"**{pl_le_min.strftime('%Y-%m-%d') if pd.notna(pl_le_min) else '—'}** → "
            f"**{pl_le_max.strftime('%Y-%m-%d') if pd.notna(pl_le_max) else '—'}**"
        )

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — UPDATE HISTORY (always visible)
# ─────────────────────────────────────────────────────────────────────────────
# ── Silent background load — needed for Section 5 (exclude confirmed cases) ──
_bg_token, _bg_repo = _gh_creds()
if _bg_token and _bg_repo and st.session_state._history is None:
    with st.spinner("Loading update history…"):
        st.session_state._history       = get_update_history(_bg_token, _bg_repo)
        st.session_state._confirmed_ids = get_all_confirmed_update_ids(
            st.session_state._history
        )

st.divider()

# Stop here if files not loaded
if sf_df is None or pl_df is None:
    st.info("Upload both files to continue.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — ENTER CASE IDs
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Enter Case IDs")
st.caption("Paste External Case IDs separated by commas.")

id_input = st.text_area(
    "Case IDs",
    placeholder="e.g. 648494#1, 957340#1, 884133#1",
    height=110,
    key="id_input",
)

if st.button("🔍 Verify & Flag Cases", type="primary", key="verify_btn"):
    if not id_input.strip():
        st.warning("Please enter at least one Case ID.")
    else:
        case_ids = parse_case_ids(id_input)
        with st.spinner(f"Comparing {len(case_ids)} case(s)…"):
            results = verify_and_flag(case_ids, sf_df, pl_df)
            outdated = get_outdated_only(results)
        st.session_state._results_df  = results
        st.session_state._outdated_df = outdated
        st.session_state._output_df   = None
        st.session_state._confirmed   = False

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — VERIFICATION RESULTS
# ─────────────────────────────────────────────────────────────────────────────
results_df  = st.session_state._results_df
outdated_df = st.session_state._outdated_df

if results_df is None:
    st.info("Enter Case IDs above and click **Verify & Flag Cases** to begin.")
    st.stop()

st.header("3. Verification Results")

# Summary metrics
total_q  = len(results_df)
n_outd   = len(outdated_df)
n_ok     = (results_df["Status"] == "✅ Up to Date").sum()
n_err    = total_q - n_outd - n_ok

rm1, rm2, rm3, rm4 = st.columns(4)
rm1.metric("Total Queried",  total_q)
rm2.metric("🔄 Outdated",    n_outd)
rm3.metric("✅ Up to Date",  n_ok)
rm4.metric("⚠️ Issues",      n_err)

# Flag breakdown (outdated only)
if n_outd > 0:
    flag_counts = outdated_df["Priority"].value_counts()
    fb_cols = st.columns(len(FLAG_ORDER))
    for i, (flag, _) in enumerate(
        sorted(FLAG_ORDER.items(), key=lambda x: x[1])
    ):
        count = int(flag_counts.get(flag, 0))
        fb_cols[i].metric(flag, count)

st.divider()

# Full results table
with st.expander("📋 Full Verification Results (all cases)", expanded=False):
    st.dataframe(results_df, width="stretch", hide_index=True)

# Outdated table (sorted by priority)
if n_outd > 0:
    st.subheader("🔄 Outdated Cases — Priority Order")
    st.dataframe(
        outdated_df[[
            "External Case ID", "Organization", "Country",
            "No. of Machines", "Smartflow Last Event", "Pleteo Last Event",
            "Months Outdated", "Priority", "Investigation Status", "Investigator",
        ]],
        width="stretch",
        hide_index=True,
    )
else:
    st.success("✅ All queried cases are up to date — no updates required.")
    st.stop()

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — OUTPUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Output Configuration")

# Previously confirmed exclusion
confirmed_ids = st.session_state._confirmed_ids or set()
prev_in_pool  = outdated_df[
    outdated_df["External Case ID"].isin(confirmed_ids)
]

exclude_confirmed = False
if len(prev_in_pool) > 0:
    exclude_confirmed = st.checkbox(
        f"Exclude {len(prev_in_pool)} previously confirmed case(s) from output",
        value=True,
        key="excl_confirmed",
    )

# Flag filter
st.subheader("Filter by Priority")
all_flags    = sorted(FLAG_ORDER.keys(), key=lambda f: FLAG_ORDER[f])
flags_in_pool = [f for f in all_flags if f in outdated_df["Priority"].values]
selected_flags = st.multiselect(
    "Include cases with these priority flags",
    options=flags_in_pool,
    default=flags_in_pool,
    key="flag_filter",
    help="Deselect flags to exclude those cases from the output.",
)

# Case limit
st.subheader("Case Limit")
case_limit = st.number_input(
    "Maximum cases in output",
    min_value=1,
    value=min(MAX_CASES, n_outd),
    key="case_limit",
    help=f"Recommended maximum: {MAX_CASES} (Smartflow credit limit).",
)
if case_limit > MAX_CASES:
    override = st.checkbox(
        f"I understand this exceeds {MAX_CASES} credits — proceed anyway ({case_limit} cases)",
        value=False,
        key="limit_override",
    )
else:
    override = True

if st.button(
    "⚙️ Generate Output",
    type="primary",
    key="gen_output_btn",
    disabled=not selected_flags or not override,
):
    pool = outdated_df[outdated_df["Priority"].isin(selected_flags)].copy()

    if exclude_confirmed and confirmed_ids:
        pool = pool[~pool["External Case ID"].isin(confirmed_ids)]

    output = pool.head(int(case_limit))
    keep   = [c for c in OUTPUT_COLS if c in output.columns]
    st.session_state._output_df = output[keep].reset_index(drop=True)
    st.session_state._confirmed = False

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — PREVIEW & GENERATE
# ─────────────────────────────────────────────────────────────────────────────
output_df = st.session_state._output_df
if output_df is None:
    st.info("Configure output above and click **Generate Output** to continue.")
    st.stop()

st.header("5. Preview & Generate")

# Priority summary of output
out_with_priority = st.session_state._outdated_df[
    st.session_state._outdated_df["External Case ID"].isin(output_df["External Case ID"])
]
out_flag_counts = out_with_priority["Priority"].value_counts()
op_cols = st.columns(len(FLAG_ORDER) + 1)
op_cols[0].metric("Total in Output", len(output_df))
for i, (flag, _) in enumerate(sorted(FLAG_ORDER.items(), key=lambda x: x[1])):
    op_cols[i + 1].metric(flag, int(out_flag_counts.get(flag, 0)))

st.dataframe(output_df, width="stretch", hide_index=True)

st.download_button(
    "⬇️ Download Output CSV",
    data=output_df.to_csv(index=False).encode(),
    file_name=f"update_priority_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    mime="text/csv",
    key="dl_output",
)

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — CONFIRM & SAVE
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Confirm Cases as Updated")
st.caption(
    "Confirm when these cases have been manually updated in Pleteo. "
    "They will be recorded in the Update History and can be excluded from future outputs."
)

if not st.session_state._confirmed:
    if st.button(
        "✅ Confirm & Save to Update History",
        type="primary",
        key="confirm_btn",
    ):
        token_c, repo_c = _require_creds()
        with st.spinner("Saving to GitHub…"):
            ok, fname = push_update_history(output_df, token_c, repo_c)
        if ok:
            st.session_state._confirmed       = True
            st.session_state._history         = None   # invalidate cache
            st.session_state._confirmed_ids   = None
            st.success(f"✅ Saved to update history: `{fname}`")
            st.balloons()
        else:
            st.error("❌ Push failed. Check token permissions.")
else:
    st.success("✅ These cases have already been confirmed and saved to update history.")

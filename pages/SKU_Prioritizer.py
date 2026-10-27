"""
Case Update Prioritizer — SketchUp
Ruvixx · LATAM Compliance Operations

Identifies outdated cases in Pleteo by comparing Last Event dates
against Smartflow, and flags them by severity for prioritized updating.

Interaction model: widgets only RECORD choices; every heavy step runs inside a
button (Process Files, Verify & Flag, Generate Output, Confirm) and its result is
stored in session state. This page keeps its own data under `_pr_*` keys so it can
never overwrite what the Comparator page stores under `_sf_df` / `_output_df`.
"""

from datetime import datetime

import pandas as pd
import streamlit as st

from comparator import add_case_type_column
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
_pg_agent = st.session_state.get("_agent_name", "Unknown")
_pg_email = st.session_state.get("_agent_email", "")
_pg_c1, _pg_c2 = st.columns([4, 1])
_pg_c1.caption(f"Ruvixx · LATAM Compliance Operations · Signed in as **{_pg_agent}** ({_pg_email})")
if _pg_c2.button("Sign Out", key="signout_pg"):
    st.session_state._agent_name  = None
    st.session_state._agent_email = None
    st.rerun()


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


# ── Auth guard ────────────────────────────────────────────────────────────────
if not st.session_state.get("_agent_name"):
    st.warning("⚠️ You are not signed in. Please return to the main page to sign in.")
    st.page_link("app.py", label="Go to Sign In", icon="🔐")
    st.stop()


# ── Product Configuration ─────────────────────────────────────────────────────
# Storage paths are product-scoped — SketchUp and Tekla are fully isolated.
PRODUCT        = "sketchup"
HISTORY_FOLDER = "history_comparator"
UPDATER_FOLDER = "history_updater"
BLACKLIST_PATH = "history_comparator/blacklist.csv"
COMP_UPD_PATH  = "history_comparator/updates_log.csv"
TAG_PREFS_PATH = "history_comparator/tag_preferences.json"
DRAFT_PATH     = "drafts_comparator/draft_latest.csv"
MAX_CASES      = 100

# Same master list as the SketchUp Comparator page (either page clears both).
_PRODUCT_DATA_KEYS = [
    '_sf_df', '_pl_df', '_sf_name', '_pl_name', '_diff_df', '_outd_df',
    '_pre_result', '_output_df', '_confirmed_comp', '_comp_batches',
    '_excl_preview', '_focused_pool', '_country_alloc', '_req_count',
    '_blacklist_df', '_blacklist_sha', '_draft_sha', '_draft_restored',
    '_tag_prefs', '_comp_updates_df', '_comp_updates_sha', '_history',
    '_confirmed_ids', '_lookup_result', '_results_df', '_outdated_df',
    '_confirmed', '_sel_reset', '_excl_map',
    '_sku_excl_map', '_val_params', '_cmp_info', '_opt_statuses', '_opt_tags',
    '_mach_bounds', '_applied_cfg', '_show_diff_tbl', '_show_outd_tbl',
    '_show_excl_tbl', '_draft_checked',
    '_pr_sf_df', '_pr_pl_df', '_pr_sf_name', '_pr_pl_name', '_pr_results_df',
    '_pr_outdated_df', '_pr_output_df', '_pr_confirmed', '_pr_verified_ids',
]
if st.session_state.get("_active_product") != PRODUCT:
    for _k in _PRODUCT_DATA_KEYS:
        st.session_state.pop(_k, None)
    st.session_state["_active_product"] = PRODUCT

for k, v in {
    "_pr_sf_df":        None,
    "_pr_pl_df":        None,
    "_pr_sf_name":      None,
    "_pr_pl_name":      None,
    "_pr_results_df":   None,
    "_pr_outdated_df":  None,
    "_pr_output_df":    None,
    "_pr_confirmed":    False,
    "_pr_verified_ids": None,
    "_history":         None,      # shared with the History page (same meaning)
    "_confirmed_ids":   None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — UPLOAD & PROCESS FILES
# ─────────────────────────────────────────────────────────────────────────────
st.header("1. Upload Files")
uc1, uc2 = st.columns(2)
with uc1:
    sf_up = st.file_uploader("Smartflow investigations (.csv)", type=["csv"], key="sf_up")
with uc2:
    pl_up = st.file_uploader("Pleteo case file (.csv or .xlsx)", type=["csv", "xlsx"], key="pl_up")

_both_uploaded = sf_up is not None and pl_up is not None
_already_processed = bool(
    _both_uploaded
    and st.session_state._pr_sf_df is not None
    and st.session_state._pr_pl_df is not None
    and sf_up.name == st.session_state._pr_sf_name
    and pl_up.name == st.session_state._pr_pl_name
)

pf1, pf2 = st.columns([1, 4])
process_clicked = pf1.button(
    "▶ Process Files", type="primary", key="process_files",
    disabled=(not _both_uploaded) or _already_processed,
)
if not _both_uploaded:
    pf2.caption("Upload both files, then click **Process Files**.")
elif _already_processed:
    pf2.caption("✅ These files are already processed.")
else:
    pf2.caption(
        "Files selected — click **Process Files** to read them. "
        "The Pleteo .xlsx can take up to a minute."
    )

if process_clicked:
    _sf_new, _pl_new, _errs = None, None, []
    try:
        with st.spinner(f"Reading Smartflow ({sf_up.size / 1e6:.1f} MB)…"):
            _sf_new = load_smartflow(sf_up.getvalue())
    except Exception as e:
        _errs.append(f"Smartflow error: {e}")
    if not _errs:       # fail fast: don't spend a minute on Pleteo if Smartflow is unreadable
        try:
            with st.spinner(f"Reading Pleteo ({pl_up.size / 1e6:.1f} MB)… please wait"):
                _pl_new = load_pleteo(pl_up.getvalue(), pl_up.name)
        except Exception as e:
            _errs.append(f"Pleteo error: {e}")

    if _errs:
        for _m in _errs:
            st.error(_m)
    else:
        st.session_state._pr_sf_df       = _sf_new
        st.session_state._pr_pl_df       = _pl_new
        st.session_state._pr_sf_name     = sf_up.name
        st.session_state._pr_pl_name     = pl_up.name
        st.session_state._pr_results_df  = None      # results came from the old files
        st.session_state._pr_outdated_df = None
        st.session_state._pr_verified_ids = None
        st.session_state._pr_output_df   = None
        st.session_state._pr_confirmed   = False
        st.rerun()

sf_df = st.session_state._pr_sf_df
pl_df = st.session_state._pr_pl_df

# Stale-data guard: a frame without the computed columns came from older code.
if sf_df is not None and not {"_sf_case_id", "_sf_last_event"}.issubset(sf_df.columns):
    st.session_state._pr_sf_df, st.session_state._pr_sf_name = None, None
    sf_df = None
    st.warning("⚠️ Cached Smartflow data was outdated — please re-upload and process it.")
if pl_df is not None and not {"_pl_case_id", "_pl_last_event"}.issubset(pl_df.columns):
    st.session_state._pr_pl_df, st.session_state._pr_pl_name = None, None
    pl_df = None
    st.warning("⚠️ Cached Pleteo data was outdated — please re-upload and process it.")

files_ready = sf_df is not None and pl_df is not None

if files_ready:
    fi1, fi2 = st.columns(2)
    _fmt = lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "—"
    fi1.success(
        f"📡 **Smartflow** — `{st.session_state._pr_sf_name}`\n\n"
        f"- Cases: **{len(sf_df):,}**\n"
        f"- Last Event range: **{_fmt(sf_df['_sf_last_event'].min())}** → "
        f"**{_fmt(sf_df['_sf_last_event'].max())}**"
    )
    fi2.success(
        f"📋 **Pleteo** — `{st.session_state._pr_pl_name}`\n\n"
        f"- Cases: **{len(pl_df):,}**\n"
        f"- Last Event range: **{_fmt(pl_df['_pl_last_event'].min())}** → "
        f"**{_fmt(pl_df['_pl_last_event'].max())}**"
    )

# ── Silent once-per-session load: update history (used to exclude confirmed cases)
_bg_token, _bg_repo = _gh_creds()
if _bg_token and _bg_repo and st.session_state._history is None:
    with st.spinner("Loading update history…"):
        st.session_state._history       = get_update_history(_bg_token, _bg_repo, folder=UPDATER_FOLDER)
        st.session_state._confirmed_ids = get_all_confirmed_update_ids(st.session_state._history)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — ENTER CASE IDs
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Enter Case IDs")
st.caption("Paste External Case IDs separated by commas.")

id_input = st.text_area(
    "Case IDs",
    placeholder="e.g. 648494#1, 957340#1, 884133#1",
    height=110,
    key="id_input",
)

verify_clicked = st.button(
    "🔍 Verify & Flag Cases", type="primary", key="verify_btn", disabled=not files_ready
)
if not files_ready:
    st.caption("Process both files (Section 1) to enable verification.")

if verify_clicked:
    if not id_input.strip():
        st.warning("Please enter at least one Case ID.")
    else:
        _ids = parse_case_ids(id_input)
        with st.spinner(f"Comparing {len(_ids)} case(s)…"):
            _results  = verify_and_flag(_ids, sf_df, pl_df)
            _outdated = get_outdated_only(_results)
        st.session_state._pr_results_df   = _results
        st.session_state._pr_outdated_df  = _outdated
        st.session_state._pr_verified_ids = _ids
        st.session_state._pr_output_df    = None
        st.session_state._pr_confirmed    = False
        # Seed the Section 4 widgets for THIS result set (before they are drawn)
        st.session_state["flag_filter"] = [
            f for f in sorted(FLAG_ORDER, key=FLAG_ORDER.get)
            if f in set(_outdated["Priority"])
        ]
        st.session_state["case_limit"]    = max(1, min(MAX_CASES, len(_outdated)))
        st.session_state["excl_confirmed"] = True

_verified = st.session_state._pr_verified_ids
if _verified is not None and id_input.strip() and parse_case_ids(id_input) != _verified:
    st.caption(
        "⚠️ The Case IDs changed since the last verification — "
        "click **Verify & Flag Cases** to refresh the results."
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — VERIFICATION RESULTS
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Verification Results")

results_df  = st.session_state._pr_results_df
outdated_df = st.session_state._pr_outdated_df
has_results  = results_df is not None
has_outdated = has_results and len(outdated_df) > 0

if not has_results:
    st.info("Enter Case IDs above and click **Verify & Flag Cases** to begin.")
else:
    total_q = len(results_df)
    n_outd  = len(outdated_df)
    n_ok    = (results_df["Status"] == "✅ Up to Date").sum()
    n_err   = total_q - n_outd - n_ok

    rm1, rm2, rm3, rm4 = st.columns(4)
    rm1.metric("Total Queried", total_q)
    rm2.metric("🔄 Outdated",   n_outd)
    rm3.metric("✅ Up to Date", n_ok)
    rm4.metric("⚠️ Issues",     n_err)

    if n_outd > 0:
        flag_counts = outdated_df["Priority"].value_counts()
        fb_cols = st.columns(len(FLAG_ORDER))
        for i, (flag, _) in enumerate(sorted(FLAG_ORDER.items(), key=lambda x: x[1])):
            fb_cols[i].metric(flag, int(flag_counts.get(flag, 0)))

    with st.expander("📋 Full Verification Results (all cases)", expanded=False):
        st.dataframe(results_df, width="stretch", hide_index=True)

    if n_outd > 0:
        st.subheader("🔄 Outdated Cases — Priority Order")
        st.dataframe(
            outdated_df[[
                "External Case ID", "Organization", "Country",
                "No. of Machines", "Smartflow Last Event", "Pleteo Last Event",
                "Months Outdated", "Priority", "Investigation Status", "Investigator",
            ]],
            width="stretch", hide_index=True,
        )
    else:
        st.success("✅ All queried cases are up to date — no updates required.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — OUTPUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Output Configuration")

confirmed_ids = st.session_state._confirmed_ids or set()
n_prev = (
    int(outdated_df["External Case ID"].isin(confirmed_ids).sum()) if has_outdated else 0
)

exclude_confirmed = st.checkbox(
    f"Exclude {n_prev} previously confirmed case(s) from output"
    if has_outdated else "Exclude previously confirmed cases from output",
    value=True,
    key="excl_confirmed",
    disabled=(not has_outdated) or n_prev == 0,
    help="Cases already confirmed as updated in the Update History.",
)

st.subheader("Filter by Priority")
flags_in_pool = (
    [f for f in sorted(FLAG_ORDER, key=FLAG_ORDER.get) if f in set(outdated_df["Priority"])]
    if has_outdated else []
)
selected_flags = st.multiselect(
    "Include cases with these priority flags",
    options=flags_in_pool,
    key="flag_filter",
    disabled=not has_outdated,
    help="Deselect flags to exclude those cases from the output.",
)

st.subheader("Case Limit")
case_limit = st.number_input(
    "Maximum cases in output",
    min_value=1,
    value=max(1, min(MAX_CASES, len(outdated_df))) if has_outdated else MAX_CASES,
    key="case_limit",
    disabled=not has_outdated,
    help=f"Recommended maximum: {MAX_CASES} (Smartflow credit limit).",
)
# Keyed by the limit so a consent given for one number never carries to another.
override = st.checkbox(
    f"I understand this exceeds {MAX_CASES} credits — proceed anyway ({case_limit} cases)",
    value=False,
    key=f"limit_override_{case_limit}",
    disabled=case_limit <= MAX_CASES,
)
override_ok = case_limit <= MAX_CASES or override

gen_clicked = st.button(
    "⚙️ Generate Output",
    type="primary",
    key="gen_output_btn",
    disabled=(not has_outdated) or (not selected_flags) or (not override_ok),
)
if gen_clicked:
    pool = outdated_df[outdated_df["Priority"].isin(selected_flags)].copy()
    if exclude_confirmed and confirmed_ids:
        pool = pool[~pool["External Case ID"].isin(confirmed_ids)]
    output = pool.head(int(case_limit))
    if output.empty:
        st.warning("⚠️ No cases are left after these filters — nothing to generate.")
    else:
        keep = [c for c in OUTPUT_COLS if c in output.columns]
        out  = add_case_type_column(output[keep].reset_index(drop=True), machines_col="No. of Machines")
        st.session_state._pr_output_df = out
        st.session_state._pr_confirmed = False

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PREVIEW & GENERATE
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Preview & Generate")

output_df = st.session_state._pr_output_df
if output_df is None:
    st.info("Configure the output above and click **Generate Output** to see the preview.")
else:
    out_with_priority = outdated_df[
        outdated_df["External Case ID"].isin(output_df["External Case ID"])
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
# SECTION 6 — CONFIRM & SAVE
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Confirm Cases as Updated")
_agent_p = st.session_state.get("_agent_name", "Unknown")
_email_p = st.session_state.get("_agent_email", "")
st.caption(
    f"Confirming as **{_agent_p}** ({_email_p}). "
    "These cases will be recorded in the Update History and excluded from future outputs."
)

if st.session_state._pr_confirmed and output_df is not None:
    st.success("✅ These cases have already been confirmed and saved to update history.")
else:
    if st.button(
        "✅ Confirm & Save to Update History",
        type="primary",
        key="confirm_btn",
        disabled=output_df is None,
    ):
        token_c, repo_c = _require_creds()
        save_df_p = output_df.copy()
        save_df_p.insert(0, "Responsible",  st.session_state.get("_agent_name", "Unknown"))
        save_df_p.insert(1, "Confirmed By", st.session_state.get("_agent_email", ""))
        save_df_p.insert(2, "Confirmed At", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with st.spinner("Saving to GitHub…"):
            ok, fname = push_update_history(save_df_p, token_c, repo_c, folder=UPDATER_FOLDER)
        if ok:
            st.session_state._pr_confirmed  = True
            st.session_state._history       = None      # invalidate cache
            st.session_state._confirmed_ids = None
            st.success(f"✅ Saved to update history: `{fname}`")
            st.balloons()
        else:
            st.error("❌ Push failed. Check token permissions.")
    if output_df is None:
        st.caption("Generate an output (Section 4) to enable confirmation.")

"""
Smartflow Comparator
Ruvixx · LATAM Compliance Operations

Compares a Smartflow download against a Pleteo export to surface:
  • Difference cases  — in Smartflow, missing from Pleteo
  • Outdated cases    — in both, but Smartflow Last Event is newer
"""

import json
from datetime import date, datetime

import pandas as pd
import streamlit as st

from comparator import (
    build_output,
    extract_tags_from_outdated,
    filter_outdated_by_status,
    filter_outdated_by_tags,
    find_difference_cases,
    find_outdated_cases,
    load_pleteo,
    load_smartflow,
    outdated_status_report,
    pleteo_country_dist,
    pleteo_status_report,
    select_by_country_distribution,
    smartflow_country_dist,
    validate_comparator_history,
)
from github_manager import (
    add_to_blacklist,
    delete_batch,
    get_all_batches,
    get_blacklist,
    push_batch,
    remove_from_blacklist,
)

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json") as f:
    config = json.load(f)

KNOWN_COUNTRIES = config["known_countries"]
HISTORY_FOLDER  = "history_comparator"
MAX_CASES       = 100

# ── Page ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Smartflow Comparator",
    page_icon="🔄",
    layout="wide",
)
st.title("🔄 Smartflow Comparator")
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
        st.error(
            "GitHub credentials not configured. "
            "Add **GITHUB_TOKEN** and **GITHUB_REPO** to your Streamlit secrets."
        )
        st.stop()
    return token, repo


# ── Session defaults ──────────────────────────────────────────────────────────
for k, v in {
    "_sf_df":              None,
    "_pl_df":              None,
    "_sf_name":            None,
    "_pl_name":            None,
    "_diff_df":            None,
    "_outd_df":            None,
    "_excl_map":           {},
    "_pre_result":         None,
    "_output_df":          None,
    "_confirmed_comp":     False,
    "_comp_batches":       None,
    "_excl_preview":       set(),    # Case IDs excluded in current preview session
    "_focused_pool":       None,     # Cached pool for refill
    "_country_alloc":      {},       # Cached allocation for refill
    "_req_count":          0,        # Original requested count for refill
    "_blacklist_df":       None,     # Cached blacklist
    "_blacklist_sha":      None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — FILE UPLOADS
# ─────────────────────────────────────────────────────────────────────────────
st.header("1. Upload Files")

uc1, uc2 = st.columns(2)
with uc1:
    sf_upload = st.file_uploader(
        "Smartflow download (.csv)", type=["csv"], key="sf_upload"
    )
with uc2:
    pl_upload = st.file_uploader(
        "Pleteo export (.csv or .xlsx)", type=["csv", "xlsx"], key="pl_upload"
    )

if sf_upload and sf_upload.name != st.session_state._sf_name:
    try:
        st.session_state._sf_df   = load_smartflow(sf_upload)
        st.session_state._sf_name = sf_upload.name
        st.session_state._diff_df = None
        st.session_state._outd_df = None
        st.session_state._pre_result     = None
        st.session_state._output_df      = None
        st.session_state._confirmed_comp = False
    except Exception as e:
        st.error(f"Smartflow file error: {e}")

if pl_upload and pl_upload.name != st.session_state._pl_name:
    try:
        st.session_state._pl_df   = load_pleteo(pl_upload)
        st.session_state._pl_name = pl_upload.name
        st.session_state._diff_df = None
        st.session_state._outd_df = None
        st.session_state._pre_result     = None
        st.session_state._output_df      = None
        st.session_state._confirmed_comp = False
    except Exception as e:
        st.error(f"Pleteo file error: {e}")

sf_df = st.session_state._sf_df
pl_df = st.session_state._pl_df

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — COMPARATOR HISTORY  (always visible, no files required)
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Comparator History")

_hist_token, _hist_repo = _gh_creds()
if not _hist_token or not _hist_repo:
    st.warning("Configure GitHub credentials to view history.")
else:
    # Auto-load history and blacklist on first render
    if st.session_state._comp_batches is None:
        with st.spinner("Loading history…"):
            st.session_state._comp_batches = get_all_batches(
                _hist_token, _hist_repo, HISTORY_FOLDER
            )
    if st.session_state._blacklist_df is None:
        sha, bl_df = get_blacklist(_hist_token, _hist_repo)
        st.session_state._blacklist_sha = sha
        st.session_state._blacklist_df  = bl_df

    batches = st.session_state._comp_batches

    # Always show summary metrics outside the expander
    if batches:
        total_cases_all = sum(len(b["df"]) for b in batches)
        hm1, hm2, hm3 = st.columns(3)
        hm1.metric("Confirmed Batches", len(batches))
        hm2.metric("Total Cases", f"{total_cases_all:,}")
        hm3.metric("Pending (Batch #19)", "600")
    elif batches is not None:
        st.info("No confirmed comparator outputs found yet.")

    # All detail inside a collapsed expander
    with st.expander("📂 View & Manage Batch History", expanded=False):
        if st.button("🔄 Refresh History", key="load_comp_hist"):
            with st.spinner("Fetching…"):
                st.session_state._comp_batches = get_all_batches(
                    _hist_token, _hist_repo, HISTORY_FOLDER
                )
            st.rerun()

        if batches:
            st.divider()

            # ── Batch list ────────────────────────────────────────────────────
            header_cols = st.columns([1, 2, 1, 1])
            header_cols[0].markdown("**Batch #**")
            header_cols[1].markdown("**Confirmed At**")
            header_cols[2].markdown("**Cases**")
            header_cols[3].markdown("**Action**")
            st.divider()

            for b in batches:
                c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
                c1.write(f"#{b['number']}")
                c2.write(
                    b["confirmed_at"].strftime("%Y-%m-%d %H:%M")
                    if b["confirmed_at"] else "—"
                )
                c3.write(len(b["df"]))
                if c4.button("🗑️ Delete", key=f"del_comp_{b['sha'][:8]}"):
                    with st.spinner(f"Deleting Batch #{b['number']}…"):
                        ok = delete_batch(_hist_token, _hist_repo, b["path"], b["sha"])
                    if ok:
                        st.success(f"Batch #{b['number']} deleted.")
                        st.session_state._comp_batches = None
                        st.rerun()
                    else:
                        st.error("❌ Deletion failed.")
                st.divider()

            # ── Inspect individual batch ──────────────────────────────────────
            sel_num = st.selectbox(
                "Inspect batch",
                options=[b["number"] for b in batches],
                format_func=lambda n: f"Batch #{n}",
                key="inspect_comp_batch",
            )
            sel_batch = next(b for b in batches if b["number"] == sel_num)
            st.dataframe(sel_batch["df"], use_container_width=True, hide_index=True)
            st.download_button(
                f"⬇️ Download Batch #{sel_num}",
                data=sel_batch["df"].to_csv(index=False).encode(),
                file_name=f"comparator_batch_{sel_num:03d}.csv",
                mime="text/csv",
                key=f"dl_comp_{sel_num}",
            )

            st.divider()

            # ── Download all batches combined ─────────────────────────────────
            st.caption(
                "Combines every confirmed batch into a single file with "
                "**Batch #** and **Confirmed At** columns for identification."
            )
            combined_parts = []
            for b in batches:
                part = b["df"].copy()
                part.insert(0, "Batch #", b["number"])
                part.insert(1, "Confirmed At",
                    b["confirmed_at"].strftime("%Y-%m-%d %H:%M")
                    if b["confirmed_at"] else "—"
                )
                combined_parts.append(part)
            combined_df = pd.concat(combined_parts, ignore_index=True)
            st.download_button(
                f"⬇️ Download All {len(batches)} Batches ({total_cases_all:,} cases)",
                data=combined_df.to_csv(index=False).encode(),
                file_name=f"comparator_all_batches_{datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key="dl_all_batches",
                type="primary",
            )

    st.divider()

    # ── Black List Management ─────────────────────────────────────────────────
    with st.expander("🚫 Black List Management", expanded=False):
        st.caption(
            "Cases in the black list are automatically excluded from all future "
            "output generation. Add cases from the preview or manage entries here."
        )

        bl_df = st.session_state._blacklist_df
        if bl_df is None or bl_df.empty:
            st.info("The black list is empty.")
        else:
            st.metric("Black Listed Cases", len(bl_df))
            st.dataframe(
                bl_df.rename(columns={
                    "case_id": "Case ID",
                    "organization_name": "Organization",
                    "country": "Country",
                    "added_at": "Added At",
                }),
                use_container_width=True, hide_index=True,
            )

            # Remove selected cases from blacklist
            remove_ids = st.multiselect(
                "Select Case IDs to remove from black list",
                options=bl_df["case_id"].tolist(),
                key="bl_remove_sel",
            )
            if remove_ids and st.button(
                f"✅ Remove {len(remove_ids)} case(s) from Black List",
                key="bl_remove_btn",
            ):
                with st.spinner("Updating black list…"):
                    ok, updated = remove_from_blacklist(
                        remove_ids, _hist_token, _hist_repo
                    )
                if ok:
                    st.session_state._blacklist_df  = updated
                    st.session_state._blacklist_sha = None
                    st.success(f"✅ Removed {len(remove_ids)} case(s) from black list.")
                    st.rerun()
                else:
                    st.error("❌ Failed to update black list.")

        if st.button("🔄 Refresh Black List", key="bl_refresh"):
            sha, bl_df = get_blacklist(_hist_token, _hist_repo)
            st.session_state._blacklist_sha = sha
            st.session_state._blacklist_df  = bl_df
            st.rerun()

    # ── Case ID Lookup ────────────────────────────────────────────────────────
    with st.expander("🔍 Verify Case IDs Against History", expanded=False):
        st.caption(
            "Paste one or more Case IDs separated by commas to check whether "
            "they exist in any confirmed batch and see their update status."
        )

        lookup_input = st.text_area(
            "Case IDs",
            placeholder="e.g. 648494#1, 957340#1, 884133#1",
            height=90,
            key="lookup_input",
        )

        if st.button("🔎 Verify", key="lookup_btn") and lookup_input.strip():
            query_ids = {x.strip() for x in lookup_input.split(",") if x.strip()}
            batches_now = st.session_state._comp_batches or []

            # Build lookup index: case_id → list of {batch #, confirmed_at}
            id_to_batches = {}
            for b in batches_now:
                col = next(
                    (c for c in ["Case ID", "case_id"] if c in b["df"].columns),
                    None,
                )
                if col is None:
                    continue
                for cid in b["df"][col].dropna().astype(str):
                    if cid in query_ids:
                        id_to_batches.setdefault(cid, []).append(b)

            # Build results table
            rows = []
            for qid in sorted(query_ids):
                if qid in id_to_batches:
                    for b in id_to_batches[qid]:
                        # Get org name from batch df
                        col = next(
                            (c for c in ["Case ID", "case_id"]
                             if c in b["df"].columns), None
                        )
                        org_col = next(
                            (c for c in ["Organization Name", "organization_name"]
                             if c in b["df"].columns), None
                        )
                        org = "—"
                        if col and org_col:
                            match = b["df"][b["df"][col].astype(str) == qid]
                            if not match.empty:
                                org = match.iloc[0][org_col]
                        rows.append({
                            "Case ID":      qid,
                            "Organization": org,
                            "Batch #":      f"#{b['number']}",
                            "Confirmed At": (
                                b["confirmed_at"].strftime("%Y-%m-%d %H:%M")
                                if b["confirmed_at"] else "—"
                            ),
                            "Status": "✅ In History",
                        })
                else:
                    rows.append({
                        "Case ID":      qid,
                        "Organization": "—",
                        "Batch #":      "—",
                        "Confirmed At": "—",
                        "Status":       "❌ Not Found",
                    })

            result_df = pd.DataFrame(rows)
            found_count    = (result_df["Status"] == "✅ In History").sum()
            notfound_count = (result_df["Status"] == "❌ Not Found").sum()

            lm1, lm2, lm3 = st.columns(3)
            lm1.metric("Queried",   len(query_ids))
            lm2.metric("Found",     found_count)
            lm3.metric("Not Found", notfound_count)

            if notfound_count:
                st.warning(
                    f"⚠️ {notfound_count} case(s) not found in any confirmed batch."
                )
            if found_count:
                st.success(
                    f"✅ {found_count} case(s) located in history."
                )

            st.dataframe(result_df, use_container_width=True, hide_index=True)

st.divider()

if sf_df is None or pl_df is None:
    st.info("Upload both files to begin.")
    st.stop()

# ── File identity cards ───────────────────────────────────────────────────────
fi1, fi2 = st.columns(2)

with fi1:
    sf_le_min = sf_df["_last_event"].min()
    sf_le_max = sf_df["_last_event"].max()
    st.success(
        f"📡 **Smartflow** — `{st.session_state._sf_name}`\n\n"
        f"- Cases: **{len(sf_df):,}**\n"
        f"- Unique Case IDs: **{sf_df['_case_id'].nunique():,}**\n"
        f"- Last Event range: "
        f"**{sf_le_min.strftime('%Y-%m-%d') if pd.notna(sf_le_min) else '—'}** → "
        f"**{sf_le_max.strftime('%Y-%m-%d') if pd.notna(sf_le_max) else '—'}**"
    )

with fi2:
    pl_le_min = pl_df["_last_event"].min()
    pl_le_max = pl_df["_last_event"].max()
    pl_note = ""
    if len(pl_df) < 5000:
        pl_note = "\n\n⚠️ **Small file detected — verify this is the full Pleteo export.**"
    st.success(
        f"📋 **Pleteo** — `{st.session_state._pl_name}`\n\n"
        f"- Cases: **{len(pl_df):,}**\n"
        f"- Unique Case IDs: **{pl_df['_case_id'].nunique():,}**\n"
        f"- Last Event range: "
        f"**{pl_le_min.strftime('%Y-%m-%d') if pd.notna(pl_le_min) else '—'}** → "
        f"**{pl_le_max.strftime('%Y-%m-%d') if pd.notna(pl_le_max) else '—'}**"
        f"{pl_note}"
    )

if len(pl_df) < 5000:
    st.warning(
        f"⚠️ The Pleteo file contains only **{len(pl_df):,}** cases. "
        "If this is a partial or filtered export, Difference cases will be inflated — "
        "cases that exist in the full Pleteo database will appear as missing. "
        "**Upload the complete Pleteo export before running the comparison.**"
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — COUNTRY DISTRIBUTION
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Country Distribution")

sf_countries = smartflow_country_dist(sf_df)
pl_countries = pleteo_country_dist(pl_df, KNOWN_COUNTRIES)

cc1, cc2 = st.columns(2)
with cc1:
    st.subheader("📡 Smartflow")
    if sf_countries:
        st.metric("Total Countries", len(sf_countries))
        st.dataframe(
            pd.DataFrame(sf_countries.items(), columns=["Country", "Cases"])
              .sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No country data found.")

with cc2:
    st.subheader("📋 Pleteo (from Tags)")
    if pl_countries:
        st.metric("Total Countries", len(pl_countries))
        st.dataframe(
            pd.DataFrame(pl_countries.items(), columns=["Country", "Cases"])
              .sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No country data found.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — PLETEO INVESTIGATION STATUS REPORT
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Pleteo — Investigation Status Report")
st.caption(
    "Status and investigator data sourced exclusively from the Pleteo file. "
    "Difference cases (not in Pleteo) have no status or investigator by definition."
)

status_report = pleteo_status_report(pl_df)

sm1, sm2 = st.columns(2)
sm1.metric("Cases with Investigation Status", status_report["cases_with_status"])
sm2.metric("Cases with Assigned Investigator", status_report["cases_with_inv"])

sr_left, sr_right = st.columns(2)

with sr_left:
    st.subheader("📊 Cases per Investigation Status")
    if status_report["status_counts"]:
        st.dataframe(
            pd.DataFrame(
                status_report["status_counts"].items(),
                columns=["Investigation Status", "Cases"],
            ).sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No Investigation Status data found.")

with sr_right:
    st.subheader("👤 Cases per Investigator")
    if status_report["investigator_counts"]:
        st.dataframe(
            pd.DataFrame(
                status_report["investigator_counts"].items(),
                columns=["Investigator", "Cases"],
            ).sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No Case Investigator assignments found.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — VALIDATE AGAINST HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Validate Against History")
st.caption(
    "Cases confirmed within the expiration window will be excluded. "
    "Cases outside the window are eligible for inclusion again."
)

vh1, vh2, _ = st.columns([1, 1, 2])
with vh1:
    exp_months = st.number_input(
        "Expiration window (months)",
        min_value=1, max_value=24, value=3, key="exp_months",
        help="Cases stored in history within this many months will be excluded.",
    )
with vh2:
    today_comp = st.date_input("Today's date", value=date.today(), key="today_comp")
today_comp_dt = datetime(today_comp.year, today_comp.month, today_comp.day)

col_val, col_ref = st.columns([1, 4])
with col_val:
    if st.button("🔎 Validate Against History", key="validate_comp"):
        token, repo = _require_creds()
        with st.spinner("Fetching comparator history from GitHub…"):
            st.session_state._comp_batches = get_all_batches(
                token, repo, HISTORY_FOLDER
            )
        batches    = st.session_state._comp_batches
        all_sf_ids = set(sf_df["_case_id"].dropna())
        excl       = validate_comparator_history(
            all_sf_ids, batches, exp_months, today_comp_dt
        )
        st.session_state._excl_map = excl

with col_ref:
    if st.button("🔄 Refresh History Cache", key="refresh_comp"):
        st.session_state._comp_batches = None
        st.rerun()

excl_map = st.session_state._excl_map

if excl_map:
    st.warning(
        f"⚠️ **{len(excl_map)}** case(s) found in history within the last "
        f"{exp_months} month(s) — they will be excluded from the output pool."
    )
    excl_rows = []
    for eid, info in excl_map.items():
        name_m = sf_df.loc[sf_df["_case_id"] == eid, "Organization Name"]
        excl_rows.append({
            "Case ID":      eid,
            "Organization": name_m.iloc[0] if len(name_m) else "—",
            "Batch(es)":    ", ".join(f"#{n}" for n in info["batch_numbers"]),
            "Last Stored":  info["stored_at"].strftime("%Y-%m-%d %H:%M"),
            "Times Stored": info["times_stored"],
        })
    with st.expander(f"View {len(excl_map)} excluded case(s)"):
        st.dataframe(pd.DataFrame(excl_rows), use_container_width=True, hide_index=True)
elif st.session_state._comp_batches is not None:
    count = len(st.session_state._comp_batches)
    st.success(
        f"✅ No recent repeated cases found across {count} history file(s)."
        if count else
        "✅ No history found — this will be the first comparator output."
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — COMPARISON REPORT
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Comparison Report")

sf_pool = sf_df[~sf_df["_case_id"].isin(excl_map.keys())].copy()

if st.button("🔍 Run Comparison", type="primary", key="run_comparison"):
    with st.spinner("Comparing files…"):
        st.session_state._diff_df = find_difference_cases(sf_pool, pl_df)
        st.session_state._outd_df = find_outdated_cases(sf_pool, pl_df)

diff_df = st.session_state._diff_df
outd_df = st.session_state._outd_df

if diff_df is None or outd_df is None:
    st.info("Click **Run Comparison** to analyse both files.")
    st.stop()

m1, m2, m3 = st.columns(3)
m1.metric("Difference Cases", len(diff_df), help="In Smartflow, missing from Pleteo")
m2.metric("Outdated Cases",   len(outd_df), help="In both; Smartflow Last Event is newer")
m3.metric("Available Pool",   len(sf_pool))

st.divider()

# ── Difference breakdown ──────────────────────────────────────────────────────
with st.expander(f"📋 Difference Cases — {len(diff_df):,} total", expanded=False):
    diff_cols = ["Case ID", "Organization Name", "Country",
                 "No. ofMachines", "Last Event", "Case Status"]
    st.dataframe(
        diff_df[[c for c in diff_cols if c in diff_df.columns]],
        use_container_width=True, hide_index=True,
    )

# ── Outdated breakdown ────────────────────────────────────────────────────────
with st.expander(f"⏰ Outdated Cases — {len(outd_df):,} total", expanded=False):
    outd_display = outd_df.copy()
    outd_display["Smartflow Last Event"] = outd_display["_last_event"].dt.strftime("%Y-%m-%d")
    outd_display["Pleteo Last Event"]    = pd.to_datetime(
        outd_display["_pleteo_last_event"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    outd_display["Investigation Status"] = outd_display["_inv_status"].fillna("—")
    outd_display["Investigator"]         = outd_display["_investigator"].fillna("—")
    outd_cols = [
        "Case ID", "Organization Name", "Country", "No. ofMachines",
        "Smartflow Last Event", "Pleteo Last Event",
        "Investigation Status", "Investigator", "Case Status",
    ]
    st.dataframe(
        outd_display[[c for c in outd_cols if c in outd_display.columns]],
        use_container_width=True, hide_index=True,
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — OUTPUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("7. Output Configuration")

# ── Focus ─────────────────────────────────────────────────────────────────────
st.subheader("Focus")
focus_diff = st.checkbox("Include Difference cases", value=True,  key="focus_diff")
focus_outd = st.checkbox("Include Outdated cases",   value=False, key="focus_outd")

if not focus_diff and not focus_outd:
    st.warning("⚠️ Select at least one focus to continue.")
    st.stop()

# ── Outdated filters — only visible when Outdated focus is selected ──────────
# Initialise outd_filtered as empty; only populated when focus_outd is active.
outd_filtered = pd.DataFrame(columns=outd_df.columns) if not focus_outd else outd_df.copy()

if focus_outd:
    if len(outd_df) == 0:
        st.info("ℹ️ No outdated cases found in the current dataset.")
    else:
        # ── Status Filter ─────────────────────────────────────────────────────
        st.subheader("🔁 Outdated — Investigation Status Filter")
        st.caption(
            "Select which Investigation Statuses to include in the outdated pool. "
            "**No Status** is a regular option. If nothing is selected, all outdated cases are included."
        )

        outd_status_counts = outdated_status_report(outd_df, pl_df)
        no_status_count    = outd_status_counts.get("No Status", 0)
        has_status_count   = sum(v for k, v in outd_status_counts.items() if k != "No Status")

        osm1, osm2 = st.columns(2)
        osm1.metric("Outdated — No Status", no_status_count)
        osm2.metric("Outdated — With Status", has_status_count)

        st.dataframe(
            pd.DataFrame(
                outd_status_counts.items(),
                columns=["Investigation Status", "Outdated Cases"],
            ).sort_values("Outdated Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )

        all_statuses = sorted(outd_status_counts.keys())
        include_statuses = st.multiselect(
            "Include cases with these statuses",
            options=all_statuses,
            default=all_statuses,
            key="reinv_statuses",
            help="Deselect any status to exclude those cases from the outdated pool.",
        )

        outd_filtered = filter_outdated_by_status(outd_df, include_statuses)

        if not include_statuses:
            st.warning("⚠️ No statuses selected — all outdated cases will be included.")
            outd_filtered = outd_df.copy()
        elif set(include_statuses) == set(all_statuses):
            st.info(f"ℹ️ All statuses selected — **{len(outd_filtered):,}** outdated cases included.")
        else:
            excluded_statuses = set(all_statuses) - set(include_statuses)
            st.info(
                f"ℹ️ Outdated pool after status filter: **{len(outd_filtered):,}** cases. "
                f"Excluded: {', '.join(sorted(excluded_statuses))}."
            )

        # ── Tag Filter ────────────────────────────────────────────────────────
        st.subheader("🏷️ Outdated — Tag Filter")
        st.caption(
            "Filter the outdated pool by Pleteo tags. "
            "Include and exclude filters can be applied simultaneously."
        )

        available_tags = extract_tags_from_outdated(outd_filtered)

        if available_tags:
            tf1, tf2 = st.columns(2)
            with tf1:
                include_tags = st.multiselect(
                    "Include — case must have ANY of these tags",
                    options=available_tags,
                    default=[],
                    key="outd_inc_tags",
                    help="Leave empty to skip this filter.",
                )
            with tf2:
                exclude_tags = st.multiselect(
                    "Exclude — remove cases with ANY of these tags",
                    options=available_tags,
                    default=[],
                    key="outd_exc_tags",
                    help="Leave empty to skip this filter.",
                )

            outd_filtered = filter_outdated_by_tags(outd_filtered, include_tags, exclude_tags)

            if include_tags or exclude_tags:
                st.info(
                    f"ℹ️ Outdated pool after tag filter: **{len(outd_filtered):,}** cases."
                )
        else:
            st.info("No tags found in the current outdated pool.")

# Build focused pool
pool_ids = set()
if focus_diff:
    pool_ids |= set(diff_df["_case_id"])
if focus_outd:
    pool_ids |= set(outd_filtered["_case_id"])

focused_pool = sf_pool[sf_pool["_case_id"].isin(pool_ids)].copy()

# Exclude black-listed cases from the pool
bl_df = st.session_state._blacklist_df
if bl_df is not None and not bl_df.empty:
    bl_ids = set(bl_df["case_id"].astype(str))
    n_before = len(focused_pool)
    focused_pool = focused_pool[~focused_pool["_case_id"].isin(bl_ids)].copy()
    n_removed = n_before - len(focused_pool)
    if n_removed > 0:
        st.info(f"ℹ️ {n_removed} black-listed case(s) excluded from the pool.")

st.divider()

# ── Machine filter ────────────────────────────────────────────────────────────
st.subheader("Machine Filter")
st.caption(
    "Filter the focused pool by machine count before selecting countries. "
    "Cases outside the range will not appear in the country distribution."
)

pool_machines = focused_pool["_machines"].dropna()
machine_min_possible = int(pool_machines.min()) if len(pool_machines) else 1
machine_max_possible = int(pool_machines.max()) if len(pool_machines) else 100

mf1, mf2 = st.columns(2)
with mf1:
    mach_min = st.number_input(
        "Minimum Machines",
        min_value=machine_min_possible,
        max_value=machine_max_possible,
        value=machine_min_possible,
        key="comp_mach_min",
    )
with mf2:
    mach_max = st.number_input(
        "Maximum Machines",
        min_value=machine_min_possible,
        max_value=machine_max_possible,
        value=machine_max_possible,
        key="comp_mach_max",
    )

if mach_max < mach_min:
    st.warning("⚠️ Maximum is less than minimum — no cases will match.")
    focused_pool = focused_pool.iloc[0:0]  # empty
elif mach_max == mach_min:
    st.info(f"Exact match: only cases with **{mach_min}** machine(s).")
    focused_pool = focused_pool[focused_pool["_machines"] == mach_min].copy()
else:
    focused_pool = focused_pool[
        (focused_pool["_machines"] >= mach_min) &
        (focused_pool["_machines"] <= mach_max)
    ].copy()

st.info(
    f"Pool after machine filter: **{len(focused_pool):,}** cases "
    f"({mach_min}–{mach_max} machines) across "
    f"**{focused_pool['_country'].nunique()}** countries."
)

st.divider()

# ── Country distribution ──────────────────────────────────────────────────────
st.subheader("Country Distribution")
st.caption(
    "Select which countries to include and how many cases per country. "
    f"Total must not exceed **{MAX_CASES}** cases."
)

available_countries = sorted(focused_pool["_country"].dropna().unique())
country_case_counts = focused_pool["_country"].value_counts().to_dict()

if available_countries:
    with st.expander("📊 Available cases per country in focused pool", expanded=True):
        st.dataframe(
            pd.DataFrame([
                {"Country": c, "Available Cases": country_case_counts.get(c, 0)}
                for c in available_countries
            ]).sort_values("Available Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )

    selected_countries = st.multiselect(
        "Select countries for output",
        options=available_countries,
        default=[],
        key="out_countries",
    )

    country_alloc  = {}
    total_requested = 0

    if selected_countries:
        st.markdown("**Cases to include per country:**")
        alloc_cols = st.columns(min(len(selected_countries), 4))
        for i, country in enumerate(selected_countries):
            available = country_case_counts.get(country, 0)
            with alloc_cols[i % 4]:
                n = st.number_input(
                    country,
                    min_value=0, max_value=available,
                    value=min(10, available),
                    key=f"alloc_{country}",
                    help=f"{available} available",
                )
                country_alloc[country] = n
        total_requested = sum(country_alloc.values())

        if total_requested > MAX_CASES:
            st.warning(
                f"⚠️ Total requested: **{total_requested}** cases — "
                f"exceeds the recommended limit of **{MAX_CASES}**. "
                "Check the override option below to generate anyway."
            )
        elif total_requested == 0:
            st.warning("⚠️ All country allocations are set to 0.")
        else:
            st.success(f"✅ Total requested: **{total_requested}** / {MAX_CASES} cases.")
else:
    st.info("No countries available in the focused pool.")
    selected_countries = []
    country_alloc      = {}
    total_requested    = 0

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 7 — PREVIEW & GENERATE
# ─────────────────────────────────────────────────────────────────────────────
st.header("8. Preview & Generate")

override_limit = False
if total_requested > MAX_CASES:
    override_limit = st.checkbox(
        f"I understand this exceeds {MAX_CASES} cases — generate anyway ({total_requested} cases)",
        value=False,
        key="override_limit",
    )

can_generate = (
    selected_countries
    and total_requested > 0
    and (total_requested <= MAX_CASES or override_limit)
)

if st.button(
    "👁️ Generate Preview",
    type="secondary",
    disabled=not can_generate,
    key="gen_preview",
):
    selected_ids = select_by_country_distribution(focused_pool, country_alloc)
    pre_result   = build_output(sf_pool, selected_ids)

    diff_ids = set(diff_df["_case_id"])
    outd_ids = set(outd_filtered["_case_id"])
    def _tag(cid):
        parts = []
        if cid in diff_ids: parts.append("Difference")
        if cid in outd_ids: parts.append("Outdated")
        return " + ".join(parts) if parts else "—"
    pre_result["Type"] = pre_result["Case ID"].apply(_tag)

    st.session_state._pre_result      = pre_result
    st.session_state._output_df       = None
    st.session_state._confirmed_comp  = False
    st.session_state._excl_preview    = set()
    st.session_state._focused_pool    = focused_pool.copy()
    st.session_state._country_alloc   = dict(country_alloc)
    st.session_state._req_count       = total_requested

pre_result = st.session_state._pre_result

if pre_result is not None:
    excl_preview  = st.session_state._excl_preview
    focused_pool  = st.session_state._focused_pool
    country_alloc = st.session_state._country_alloc

    # Active preview = pre_result minus excluded cases
    active_preview = pre_result[
        ~pre_result["Case ID"].isin(excl_preview)
    ].reset_index(drop=True)

    st.subheader("Preview")
    type_counts = active_preview["Type"].value_counts().to_dict()
    pm1, pm2, pm3, pm4 = st.columns(4)
    pm1.metric("In Output",  len(active_preview))
    pm2.metric("Difference", type_counts.get("Difference", 0))
    pm3.metric("Outdated",   type_counts.get("Outdated", 0))
    pm4.metric("Excluded",   len(excl_preview))

    preview_cols = ["Case ID", "Organization Name", "Country",
                    "No. ofMachines", "Last Event", "Type"]
    disp_cols = [c for c in preview_cols if c in active_preview.columns]

    # Selectable dataframe
    sel_event = st.dataframe(
        active_preview[disp_cols],
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key="preview_sel",
    )
    selected_rows = (
        sel_event.selection.rows
        if sel_event and hasattr(sel_event, "selection") and sel_event.selection
        else []
    )

    # Action buttons
    btn1, btn2, btn3, btn4 = st.columns(4)

    if selected_rows:
        if btn1.button(f"🚫 Exclude Selected ({len(selected_rows)})", key="excl_sel_btn"):
            new_excl = set(active_preview.iloc[selected_rows]["Case ID"].tolist())
            st.session_state._excl_preview |= new_excl
            st.rerun()

    if excl_preview and focused_pool is not None:
        if btn2.button("🔄 Refill List", key="refill_btn"):
            kept_ids = set(active_preview["Case ID"].tolist())
            all_seen = kept_ids | excl_preview
            bl_ids   = set(
                st.session_state._blacklist_df["case_id"].astype(str)
            ) if (st.session_state._blacklist_df is not None and
                  not st.session_state._blacklist_df.empty) else set()

            new_rows = []
            for country, alloc_n in country_alloc.items():
                kept_n = len(active_preview[active_preview["Country"] == country])
                need   = alloc_n - kept_n
                if need <= 0:
                    continue
                candidates = focused_pool[
                    (focused_pool["_country"] == country) &
                    (~focused_pool["_case_id"].isin(all_seen)) &
                    (~focused_pool["_case_id"].isin(bl_ids))
                ].sort_values("_last_event", ascending=False).head(need)
                if not candidates.empty:
                    new_rows.append(build_output(focused_pool, set(candidates["_case_id"])))

            if new_rows:
                additions = pd.concat(new_rows, ignore_index=True)
                diff_ids_r = set(diff_df["_case_id"])
                outd_ids_r = set(outd_filtered["_case_id"])
                def _tag_r(cid):
                    p = []
                    if cid in diff_ids_r: p.append("Difference")
                    if cid in outd_ids_r: p.append("Outdated")
                    return " + ".join(p) if p else "—"
                additions["Type"] = additions["Case ID"].apply(_tag_r)
                refilled = pd.concat([active_preview, additions], ignore_index=True)
                st.session_state._pre_result = refilled
                st.success(f"✅ Added {len(additions)} replacement case(s).")
                st.rerun()
            else:
                st.warning("⚠️ No additional cases available in the pool to refill.")

    if excl_preview:
        if btn3.button(f"⛔ Add {len(excl_preview)} to Black List", key="add_bl_btn"):
            token, repo = _require_creds()
            rows_for_bl = [
                {"case_id": r["Case ID"],
                 "organization_name": r.get("Organization Name", ""),
                 "country": r.get("Country", "")}
                for _, r in pre_result[pre_result["Case ID"].isin(excl_preview)].iterrows()
            ]
            with st.spinner("Saving to black list…"):
                ok, updated_bl = add_to_blacklist(rows_for_bl, token, repo)
            if ok:
                st.session_state._blacklist_df  = updated_bl
                st.session_state._blacklist_sha = None
                st.success(f"✅ {len(rows_for_bl)} case(s) added to the black list.")
            else:
                st.error("❌ Failed to save black list.")

    if excl_preview:
        if btn4.button("↩️ Clear Exclusions", key="clear_excl_btn"):
            st.session_state._excl_preview = set()
            st.rerun()

    if excl_preview:
        with st.expander(f"🚫 Excluded cases — {len(excl_preview)} case(s)", expanded=False):
            excl_view = pre_result[pre_result["Case ID"].isin(excl_preview)][disp_cols]
            st.dataframe(excl_view.reset_index(drop=True), use_container_width=True, hide_index=True)

    st.divider()

    if st.button("⚙️ Generate Output File", type="primary", key="gen_output"):
        st.session_state._output_df      = active_preview
        st.session_state._confirmed_comp = False


output_df = st.session_state._output_df

if output_df is not None:
    st.success(f"✅ Output file ready — **{len(output_df)}** case(s).")
    st.download_button(
        "⬇️ Download Output CSV",
        data=output_df.to_csv(index=False).encode(),
        file_name=f"comparator_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
        key="dl_output",
    )

    st.divider()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 8 — CONFIRM & SAVE TO HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    st.header("9. Confirm & Save to History")
    st.caption(
        "Confirming saves this output as a history record. "
        "These cases will be excluded from future outputs within the expiration window."
    )

    if not st.session_state._confirmed_comp:
        if st.button(
            "✅ Confirm & Push to GitHub History",
            type="primary",
            key="confirm_comp",
        ):
            token, repo = _require_creds()
            with st.spinner("Pushing to GitHub…"):
                ok, fname = push_batch(output_df, token, repo, HISTORY_FOLDER)
            if ok:
                st.session_state._confirmed_comp = True
                st.session_state._comp_batches   = None
                st.success(f"✅ Saved to history: `{fname}`")
                st.balloons()
            else:
                st.error("❌ Push failed. Check token permissions.")
    else:
        st.success("✅ This output has already been confirmed and saved to history.")

    st.divider()


# ─────────────────────────────────────────────────────────────────────────────

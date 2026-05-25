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
    find_difference_cases,
    find_outdated_cases,
    load_pleteo,
    load_smartflow,
    pleteo_country_dist,
    select_by_country_distribution,
    smartflow_country_dist,
    validate_comparator_history,
)
from github_manager import (
    delete_batch,
    get_all_batches,
    push_batch,
)

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json") as f:
    config = json.load(f)

KNOWN_COUNTRIES  = config["known_countries"]
HISTORY_FOLDER   = "history_comparator"
MAX_CASES        = 100

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
    "_sf_df":          None,
    "_pl_df":          None,
    "_sf_name":        None,
    "_pl_name":        None,
    "_diff_df":        None,
    "_outd_df":        None,
    "_excl_map":       {},
    "_pre_result":     None,
    "_output_df":      None,
    "_confirmed_comp": False,
    "_comp_batches":   None,
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
        "Smartflow download (.csv)",
        type=["csv"],
        key="sf_upload",
    )
with uc2:
    pl_upload = st.file_uploader(
        "Pleteo export (.csv or .xlsx)",
        type=["csv", "xlsx"],
        key="pl_upload",
    )

# Load Smartflow
if sf_upload and sf_upload.name != st.session_state._sf_name:
    try:
        st.session_state._sf_df   = load_smartflow(sf_upload)
        st.session_state._sf_name = sf_upload.name
        st.session_state._diff_df = None
        st.session_state._outd_df = None
        st.session_state._pre_result  = None
        st.session_state._output_df   = None
        st.session_state._confirmed_comp = False
    except Exception as e:
        st.error(f"Smartflow file error: {e}")

# Load Pleteo
if pl_upload and pl_upload.name != st.session_state._pl_name:
    try:
        st.session_state._pl_df   = load_pleteo(pl_upload)
        st.session_state._pl_name = pl_upload.name
        st.session_state._diff_df = None
        st.session_state._outd_df = None
        st.session_state._pre_result  = None
        st.session_state._output_df   = None
        st.session_state._confirmed_comp = False
    except Exception as e:
        st.error(f"Pleteo file error: {e}")

sf_df = st.session_state._sf_df
pl_df = st.session_state._pl_df

if sf_df is None or pl_df is None:
    st.info("Upload both files to begin.")
    st.stop()

st.success(
    f"✅ Files loaded — Smartflow: **{len(sf_df):,}** cases | "
    f"Pleteo: **{len(pl_df):,}** cases"
)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — COUNTRY REPORT
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Country Distribution")

sf_countries = smartflow_country_dist(sf_df)
pl_countries = pleteo_country_dist(pl_df, KNOWN_COUNTRIES)

cc1, cc2 = st.columns(2)
with cc1:
    st.subheader("📡 Smartflow")
    if sf_countries:
        sf_c_df = pd.DataFrame(
            sf_countries.items(), columns=["Country", "Cases"]
        ).sort_values("Cases", ascending=False)
        st.metric("Total Countries", len(sf_countries))
        st.dataframe(sf_c_df, use_container_width=True, hide_index=True)
    else:
        st.info("No country data found.")

with cc2:
    st.subheader("📋 Pleteo (from Tags)")
    if pl_countries:
        pl_c_df = pd.DataFrame(
            pl_countries.items(), columns=["Country", "Cases"]
        ).sort_values("Cases", ascending=False)
        st.metric("Total Countries", len(pl_countries))
        st.dataframe(pl_c_df, use_container_width=True, hide_index=True)
    else:
        st.info("No country data found.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — VALIDATE AGAINST HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Validate Against History")
st.caption(
    "Cases confirmed within the expiration window will be excluded. "
    "Cases outside the window are eligible for inclusion again."
)

vh1, vh2, _ = st.columns([1, 1, 2])
with vh1:
    exp_months = st.number_input(
        "Expiration window (months)",
        min_value=1, max_value=24, value=3,
        key="exp_months",
        help="Cases stored in history within this many months will be excluded.",
    )
with vh2:
    today_comp = st.date_input(
        "Today's date", value=date.today(), key="today_comp"
    )
today_comp_dt = datetime(today_comp.year, today_comp.month, today_comp.day)

col_val, col_ref = st.columns([1, 4])
with col_val:
    if st.button("🔎 Validate Against History", key="validate_comp"):
        token, repo = _require_creds()
        with st.spinner("Fetching comparator history from GitHub…"):
            st.session_state._comp_batches = get_all_batches(
                token, repo, HISTORY_FOLDER
            )
        batches = st.session_state._comp_batches
        all_sf_ids = set(sf_df["_case_id"].dropna())
        excl = validate_comparator_history(
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
# SECTION 4 — COMPARISON REPORT
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Comparison Report")

# Apply exclusions to Smartflow pool
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

# ── Metrics ───────────────────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)
m1.metric("Difference Cases", len(diff_df), help="In Smartflow, missing from Pleteo")
m2.metric("Outdated Cases", len(outd_df),   help="In both; Smartflow Last Event is newer")
m3.metric("Available Pool", len(sf_pool))

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
    outd_display["Pleteo Last Event"]    = outd_display["_pleteo_last_event"].dt.strftime("%Y-%m-%d")
    outd_cols = [
        "Case ID", "Organization Name", "Country", "No. ofMachines",
        "Smartflow Last Event", "Pleteo Last Event", "Case Status",
    ]
    st.dataframe(
        outd_display[[c for c in outd_cols if c in outd_display.columns]],
        use_container_width=True, hide_index=True,
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — OUTPUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Output Configuration")

# ── Focus selection ───────────────────────────────────────────────────────────
st.subheader("Focus")
focus_diff = st.checkbox("Include Difference cases", value=True, key="focus_diff")
focus_outd = st.checkbox("Include Outdated cases",   value=False, key="focus_outd")

if not focus_diff and not focus_outd:
    st.warning("⚠️ Select at least one focus to continue.")
    st.stop()

# Merge pool based on selection
pool_ids = set()
if focus_diff:
    pool_ids |= set(diff_df["_case_id"])
if focus_outd:
    pool_ids |= set(outd_df["_case_id"])

focused_pool = sf_pool[sf_pool["_case_id"].isin(pool_ids)].copy()

st.info(
    f"Combined pool from selected focus: **{len(focused_pool):,}** cases "
    f"across **{focused_pool['_country'].nunique()}** countries."
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

# Show country availability
avail_df = pd.DataFrame([
    {"Country": c, "Available Cases": country_case_counts.get(c, 0)}
    for c in available_countries
]).sort_values("Available Cases", ascending=False)

with st.expander("📊 Available cases per country in focused pool", expanded=True):
    st.dataframe(avail_df, use_container_width=True, hide_index=True)

selected_countries = st.multiselect(
    "Select countries for output",
    options=available_countries,
    default=[],
    key="out_countries",
)

country_alloc = {}
total_requested = 0

if selected_countries:
    st.markdown("**Cases to include per country:**")
    alloc_cols = st.columns(min(len(selected_countries), 4))
    for i, country in enumerate(selected_countries):
        available = country_case_counts.get(country, 0)
        with alloc_cols[i % 4]:
            n = st.number_input(
                f"{country}",
                min_value=0,
                max_value=available,
                value=min(10, available),
                key=f"alloc_{country}",
                help=f"{available} available",
            )
            country_alloc[country] = n
    total_requested = sum(country_alloc.values())

    # Running total indicator
    if total_requested > MAX_CASES:
        st.error(
            f"⛔ Total requested: **{total_requested}** cases — "
            f"exceeds the Smartflow limit of **{MAX_CASES}**. "
            "Reduce the allocation before generating."
        )
    elif total_requested == 0:
        st.warning("⚠️ All country allocations are set to 0.")
    else:
        st.success(f"✅ Total requested: **{total_requested}** / {MAX_CASES} cases.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — PREVIEW & GENERATE
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Preview & Generate")

can_generate = (
    selected_countries
    and total_requested > 0
    and total_requested <= MAX_CASES
)

if st.button(
    "👁️ Generate Preview",
    type="secondary",
    disabled=not can_generate,
    key="gen_preview",
):
    selected_ids = select_by_country_distribution(focused_pool, country_alloc)
    pre_result   = build_output(sf_pool, selected_ids)

    # Tag each row with its focus type
    diff_ids = set(diff_df["_case_id"])
    outd_ids = set(outd_df["_case_id"])
    def _tag(cid):
        parts = []
        if cid in diff_ids:
            parts.append("Difference")
        if cid in outd_ids:
            parts.append("Outdated")
        return " + ".join(parts) if parts else "—"
    pre_result["Type"] = pre_result["Case ID"].apply(_tag)

    st.session_state._pre_result     = pre_result
    st.session_state._output_df      = None
    st.session_state._confirmed_comp = False

pre_result = st.session_state._pre_result

if pre_result is not None:
    st.subheader("Preview")
    type_counts = pre_result["Type"].value_counts().to_dict()
    pm1, pm2, pm3 = st.columns(3)
    pm1.metric("Total Cases",     len(pre_result))
    pm2.metric("Difference",      type_counts.get("Difference", 0))
    pm3.metric("Outdated",        type_counts.get("Outdated", 0))

    preview_cols = [
        "Case ID", "Organization Name", "Country",
        "No. ofMachines", "Last Event", "Type",
    ]
    st.dataframe(
        pre_result[[c for c in preview_cols if c in pre_result.columns]],
        use_container_width=True,
        hide_index=True,
    )

    if st.button(
        "⚙️ Generate Output File",
        type="primary",
        key="gen_output",
    ):
        st.session_state._output_df      = pre_result.drop(columns=["Type"])
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
    # SECTION 7 — CONFIRM & SAVE TO HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    st.header("7. Confirm & Save to History")
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
            # Store using Case ID column so history reader can find it
            save_df = output_df.rename(columns={"Case ID": "Case ID"})
            with st.spinner("Pushing to GitHub…"):
                ok, fname = push_batch(save_df, token, repo, HISTORY_FOLDER)
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
# SECTION 8 — HISTORY VIEWER
# ─────────────────────────────────────────────────────────────────────────────
st.header("8. Comparator History")
st.caption(
    "All confirmed comparator outputs stored in GitHub. "
    "Batch numbers are derived from chronological order."
)

token, repo = _gh_creds()
if not token or not repo:
    st.warning("Configure GitHub credentials to view history.")
else:
    if st.button("🔄 Load / Refresh Comparator History", key="load_comp_hist"):
        with st.spinner("Fetching…"):
            st.session_state._comp_batches = get_all_batches(
                token, repo, HISTORY_FOLDER
            )

    batches = st.session_state._comp_batches

    if batches is None:
        st.info("Click **Load / Refresh Comparator History** to view records.")
    elif not batches:
        st.info("No confirmed comparator outputs found yet.")
    else:
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
                    ok = delete_batch(token, repo, b["path"], b["sha"])
                if ok:
                    st.success(f"Batch #{b['number']} deleted.")
                    st.session_state._comp_batches = None
                    st.rerun()
                else:
                    st.error("❌ Deletion failed.")
            st.divider()

        # ── Inspect individual batch ──────────────────────────────────────────
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

"""
Smartflow Batch Selector
Ruvixx · LATAM Compliance Operations
"""

import json
from datetime import date, datetime

import pandas as pd
import streamlit as st

from github_manager import (
    delete_batch,
    find_batch_for_ids,
    find_repeated_within_window,
    get_all_batches,
    get_updates_log,
    push_batch,
    push_updates_log,
)
from processor import (
    apply_filters,
    country_distribution,
    extract_all_tags,
    extract_ids_from_zip,
    generate_summary,
    load_file,
)

# ── Config ────────────────────────────────────────────────────────────────────
with open("config.json") as f:
    config = json.load(f)

KNOWN_COUNTRIES = config["known_countries"]
HISTORY_FOLDER  = config["github"]["history_folder"]

# ── Page setup ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Smartflow Batch Selector",
    page_icon="🔍",
    layout="wide",
)

st.title("🔍 Smartflow Batch Selector")
st.caption("Ruvixx · LATAM Compliance Operations")


# ── GitHub credentials helper ─────────────────────────────────────────────────
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


# ── Session state defaults ────────────────────────────────────────────────────
for key, default in {
    "_loaded_name":  None,
    "_df_raw":       None,
    "_excluded_map": {},    # eid → {batch_numbers, latest_batch, latest_confirmed, times_confirmed}
    "_output_df":    None,
    "_confirmed":    False,
    "_batches":      None,  # cached list from GitHub
    "_updates_sha":  None,
    "_updates_df":   None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — UPLOAD
# ─────────────────────────────────────────────────────────────────────────────
st.header("1. Upload Dataset")
uploaded = st.file_uploader(
    "Upload a Pleteo export (.csv or .xlsx)",
    type=["csv", "xlsx"],
)

if uploaded is None:
    st.info("Upload a file to begin.")
    st.stop()

if st.session_state._loaded_name != uploaded.name:
    try:
        st.session_state._df_raw = load_file(uploaded)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        st.stop()
    st.session_state._loaded_name = uploaded.name
    st.session_state._excluded_map = {}
    st.session_state._output_df    = None
    st.session_state._confirmed    = False

df_raw = st.session_state._df_raw


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — CONFIRM UPDATED CASES
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Confirm Updated Cases")
st.caption(
    "Use this section to mark cases from previous confirmed batches as "
    "**updated in Pleteo**. This is independent from batch selection — "
    "confirming a batch means it was sent to Smartflow; this section "
    "records that the case was actually updated in the CRM."
)

upd_method = st.radio(
    "Input method",
    ["Manual — paste External Case IDs", "File — upload ZIP of updated case files"],
    horizontal=True,
    key="upd_method",
)

upd_ids_raw = set()

if upd_method == "Manual — paste External Case IDs":
    text_input = st.text_area(
        "Paste External Case IDs separated by commas",
        placeholder="648494#1, 957340#1, 884133#1",
        height=100,
        key="upd_text",
    )
    if text_input.strip():
        upd_ids_raw = {x.strip() for x in text_input.split(",") if x.strip()}

else:
    zip_upload = st.file_uploader(
        "Upload ZIP file (filenames must contain the External Case ID, e.g. 648494#1)",
        type=["zip"],
        key="upd_zip",
    )
    if zip_upload is not None:
        try:
            upd_ids_raw = extract_ids_from_zip(zip_upload.read())
            if upd_ids_raw:
                st.success(f"✅ {len(upd_ids_raw)} External Case ID(s) extracted from ZIP filenames.")
            else:
                st.warning("No External Case IDs found in ZIP filenames.")
        except Exception as e:
            st.error(f"Could not read ZIP: {e}")

if upd_ids_raw and st.button("✅ Confirm Updates", type="primary", key="btn_confirm_updates"):
    token, repo = _require_creds()

    # Load batches if not cached
    if st.session_state._batches is None:
        with st.spinner("Loading history from GitHub…"):
            st.session_state._batches = get_all_batches(token, repo, HISTORY_FOLDER)
    batches = st.session_state._batches

    found_map, not_found = find_batch_for_ids(upd_ids_raw, batches)

    if not_found:
        st.warning(
            f"⚠️ The following ID(s) were not found in any confirmed batch "
            f"and will be skipped: **{', '.join(sorted(not_found))}**"
        )

    if found_map:
        # Load or init updates log
        if st.session_state._updates_df is None:
            with st.spinner("Loading updates log…"):
                sha, ulog = get_updates_log(token, repo)
            st.session_state._updates_sha = sha
            st.session_state._updates_df  = ulog

        ulog = st.session_state._updates_df.copy()
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_rows = []
        for eid, batch_nums in found_map.items():
            new_rows.append({
                "external_case_id": eid,
                "confirmed_at":     now,
                "source":           upd_method.split("—")[0].strip().lower(),
                "batch_numbers":    ",".join(str(n) for n in batch_nums),
            })
        ulog = pd.concat(
            [ulog, pd.DataFrame(new_rows)], ignore_index=True
        )

        with st.spinner("Saving updates log to GitHub…"):
            ok = push_updates_log(
                ulog, token, repo, sha=st.session_state._updates_sha
            )

        if ok:
            st.session_state._updates_df  = ulog
            st.session_state._updates_sha = None  # will be refreshed next load
            st.success(
                f"✅ {len(found_map)} case(s) marked as confirmed updated: "
                f"**{', '.join(sorted(found_map.keys()))}**"
            )
        else:
            st.error("❌ Failed to save updates log to GitHub.")
    else:
        st.info("No valid IDs to process.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — DATASET SUMMARY  (reactive to outdated threshold)
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Dataset Summary")

c_thresh, c_today, _ = st.columns([1, 1, 2])
with c_thresh:
    outdated_months = st.number_input(
        "Outdated threshold (months)",
        min_value=1, max_value=60, value=6,
        key="outdated_months",
        help="Cases not updated within this many months are considered outdated.",
    )
with c_today:
    today_input = st.date_input("Today's date", value=date.today(), key="today_input")

today_dt = datetime(today_input.year, today_input.month, today_input.day)

# Summary always recomputes live — no button, fully reactive
summary = generate_summary(df_raw, config, outdated_months, today_dt)

ds_type = summary["dataset_type"]
st.markdown(
    f"**Dataset type detected:** "
    f"{'🔵 Uninvestigated' if ds_type == 'uninvestigated' else '🟠 Disqualified'}"
)

m1, m2, m3 = st.columns(3)
m1.metric("Total Cases", summary["total_cases"])
m2.metric(f"Outdated (> {outdated_months} months)", summary["outdated_count"])
m3.metric("Countries Detected", len(summary["country_dist"]))

st.divider()

col_left, col_right = st.columns(2)
with col_left:
    st.subheader("📍 Cases per Country")
    if summary["country_dist"]:
        st.dataframe(
            pd.DataFrame(summary["country_dist"].items(), columns=["Country", "Cases"])
              .sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No country tags identified.")

with col_right:
    st.subheader("🏷️ Cases with Special Tags")
    if summary["special_tags"]:
        st.dataframe(
            pd.DataFrame(summary["special_tags"].items(), columns=["Tag", "Cases"])
              .sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )
    else:
        st.info("No special tags found.")

st.divider()

st.subheader("📅 Cases by Last Event Year")
if summary["cases_per_year"]:
    st.dataframe(
        pd.DataFrame(summary["cases_per_year"].items(), columns=["Year", "Cases"])
          .sort_values("Year", ascending=False),
        use_container_width=True, hide_index=True,
    )
else:
    st.info("Could not parse Last Event dates.")

st.divider()

st.subheader("⚠️ Data Quality — Unexpected Values")
unexp = summary["unexpected_values"]
if unexp:
    total_flagged = sum(len(v) for v in unexp.values())
    st.warning(
        f"{total_flagged} case(s) have values in columns that should be empty."
    )
    for col, flagged_df in unexp.items():
        with st.expander(f"Column '{col}' — {len(flagged_df)} case(s)"):
            st.dataframe(flagged_df, use_container_width=True, hide_index=True)
else:
    st.success("✅ All monitored columns are clean.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — VALIDATE AGAINST HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Validate Against History")
st.caption(
    "Cases are only flagged as repeated if they appear in a confirmed batch "
    f"confirmed within the last **{outdated_months} month(s)**. "
    "Cases from older batches are eligible for re-selection."
)

col_val, col_ref = st.columns([1, 1])
with col_val:
    if st.button("🔎 Validate Dataset", use_container_width=True):
        token, repo = _require_creds()
        with st.spinner("Fetching history from GitHub…"):
            st.session_state._batches = get_all_batches(token, repo, HISTORY_FOLDER)
        batches = st.session_state._batches

        current_ids = set(df_raw["External Case ID"].dropna().astype(str))
        excl_map = find_repeated_within_window(
            current_ids, batches, outdated_months, today_dt
        )
        st.session_state._excluded_map = excl_map

with col_ref:
    if st.button("🔄 Refresh History Cache", use_container_width=True):
        st.session_state._batches = None
        st.rerun()

excl_map = st.session_state._excluded_map

if excl_map:
    st.warning(
        f"⚠️ **{len(excl_map)}** case(s) are repeated within the last "
        f"{outdated_months} month(s) and will be excluded from the output."
    )
    rep_rows = []
    for eid, info in excl_map.items():
        name_matches = df_raw.loc[
            df_raw["External Case ID"].astype(str) == eid, "Name"
        ]
        name = name_matches.iloc[0] if len(name_matches) else "—"
        rep_rows.append({
            "External Case ID": eid,
            "Name":             name,
            "Batch(es)":        ", ".join(f"#{n}" for n in info["batch_numbers"]),
            "Latest Confirmed": info["latest_confirmed"].strftime("%Y-%m-%d %H:%M"),
            "Times Confirmed":  info["times_confirmed"],
        })
    st.dataframe(
        pd.DataFrame(rep_rows),
        use_container_width=True,
        hide_index=True,
    )
elif st.session_state._batches is not None:
    count = len(st.session_state._batches)
    st.success(
        f"✅ No repeated cases within the {outdated_months}-month window "
        f"across {count} confirmed batch(es)."
        if count else
        "✅ No history files found — this will be the first confirmed batch."
    )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PRIORITIZATION & FILTERS
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Prioritization & Filters")

# Working pool: remove excluded cases
df_work = df_raw.copy()
if excl_map:
    df_work = df_work[
        ~df_work["External Case ID"].astype(str).isin(excl_map.keys())
    ].reset_index(drop=True)
    st.info(
        f"ℹ️ {len(excl_map)} repeated case(s) excluded. "
        f"Working pool: **{len(df_work)}** cases."
    )

all_tags        = extract_all_tags(df_work)
country_counts  = country_distribution(df_work, KNOWN_COUNTRIES)
country_options = list(country_counts.keys())

st.subheader("Machine & Event Filters")
r1c1, r1c2, r1c3 = st.columns(3)
with r1c1:
    min_machines = st.number_input(
        "Minimum Total Machines", min_value=1, value=3, key="min_machines"
    )
with r1c2:
    use_max = st.checkbox("Set maximum machines", value=False, key="use_max_machines")
    max_machines = None
    if use_max:
        max_machines = st.number_input(
            "Maximum Total Machines",
            min_value=min_machines,
            value=max(min_machines, 10),
            key="max_machines",
            help="If equal to minimum, only cases with that exact machine count are selected.",
        )
        if max_machines == min_machines:
            st.info(f"ℹ️ Exact match — only cases with **{min_machines}** machine(s) will be selected.")
with r1c3:
    last_event_cutoff = st.date_input(
        "Last Event — earliest allowed date",
        value=date(2023, 1, 1),
        key="last_event",
    )

st.subheader("Tag Filters")
r2c1, r2c2 = st.columns(2)
with r2c1:
    include_tags = st.multiselect(
        "Include — cases must have ANY of these tags",
        options=all_tags, default=[], key="inc_tags",
        help="Leave empty to skip.",
    )
with r2c2:
    exclude_tags = st.multiselect(
        "Exclude — remove cases with ANY of these tags",
        options=all_tags, default=[], key="exc_tags",
        help="Leave empty to skip.",
    )

st.subheader("Sort Order — Updated Column")
sort_updated = st.radio(
    "Select cases by Update recency",
    ["Oldest", "Newest", "Mixed"],
    index=1,
    horizontal=True,
    key="sort_upd",
    help=(
        "Oldest: least recently updated first. "
        "Newest: most recently updated first. "
        "Mixed: alternates oldest/newest for balanced selection."
    ),
).lower()

st.subheader("Country Distribution Filter")
if country_options:
    with st.expander("📊 View country distribution", expanded=False):
        st.dataframe(
            pd.DataFrame(country_counts.items(), columns=["Country", "Cases"])
              .sort_values("Cases", ascending=False),
            use_container_width=True, hide_index=True,
        )

    filter_mode = st.radio(
        "Country filter mode",
        ["All countries", "Select specific countries",
         "Top N countries", "Top X% of countries"],
        horizontal=True, key="c_mode",
    )
    country_filter = []
    if filter_mode == "Select specific countries":
        country_filter = st.multiselect(
            "Countries to include", options=country_options, key="c_select"
        )
    elif filter_mode == "Top N countries":
        top_n = st.number_input(
            "Top N countries (by case count)",
            min_value=1, max_value=len(country_options),
            value=min(3, len(country_options)), key="c_topn",
        )
        country_filter = country_options[: int(top_n)]
        st.info(f"Selected: {', '.join(country_filter)}")
    elif filter_mode == "Top X% of countries":
        top_pct = st.slider("Percentage of countries", 1, 100, 50, key="c_pct")
        n = max(1, round(len(country_options) * top_pct / 100))
        country_filter = country_options[:n]
        st.info(f"{top_pct}% = {n} country/ies: {', '.join(country_filter)}")
else:
    st.info("No country tags detected — country filter unavailable.")
    country_filter = []

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — GENERATE BATCH
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Generate Batch")

case_count = st.number_input(
    "Number of cases to select",
    min_value=1,
    max_value=max(len(df_work), 1),
    value=min(50, max(len(df_work), 1)),
    key="case_count",
)
if case_count > 100:
    st.warning(
        "⚠️ Smartflow allows a maximum of **100 case event downloads per day**. "
        "Selecting more than 100 may exceed your daily credit limit."
    )

if st.button("⚙️ Generate Batch", type="primary", use_container_width=True, key="gen_batch"):
    filters = {
        "min_machines":      min_machines,
        "max_machines":      max_machines,
        "last_event_cutoff": last_event_cutoff,
        "include_tags":      include_tags,
        "exclude_tags":      exclude_tags,
        "country_filter":    country_filter,
        "sort_updated":      sort_updated,
    }
    with st.spinner("Applying filters…"):
        filtered = apply_filters(df_work, config, filters)

    if filtered.empty:
        st.warning("⚠️ No cases match the current filters. Try relaxing one or more conditions.")
        st.session_state._output_df = None
    else:
        st.session_state._output_df = filtered.head(int(case_count))
        st.session_state._confirmed = False

# ── Output table ──────────────────────────────────────────────────────────────
if st.session_state._output_df is not None:
    out = st.session_state._output_df
    st.success(f"✅ **{len(out)}** case(s) selected.")

    display_cols = [
        "External Case ID", "Name", "# Total Machines", "# Recent Machines",
        "Case Tier", "Data Score", "Last Event", "Updated", "Tags",
    ]
    st.dataframe(
        out[[c for c in display_cols if c in out.columns]],
        use_container_width=True, hide_index=True,
    )

    st.download_button(
        "⬇️ Download Output CSV",
        data=out.to_csv(index=False).encode(),
        file_name=f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )

    st.divider()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 7 — CONFIRM & SAVE TO HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    st.header("7. Confirm & Save to History")
    st.caption(
        "Confirming this batch records these cases as **sent to Smartflow for processing**. "
        "They will be excluded from future batch selections within the outdated window."
    )

    if not st.session_state._confirmed:
        if st.button(
            "✅ Confirm & Push to GitHub History",
            type="primary",
            key="confirm_batch",
        ):
            token, repo = _require_creds()
            with st.spinner("Pushing batch to GitHub…"):
                ok, fname = push_batch(out, token, repo, HISTORY_FOLDER)
            if ok:
                st.session_state._confirmed = True
                st.session_state._batches   = None   # invalidate cache
                st.success(f"✅ Batch saved: `{fname}`")
                st.balloons()
            else:
                st.error("❌ Push failed. Check your token permissions.")
    else:
        st.success("✅ This batch has already been confirmed and saved to history.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 8 — HISTORY VIEWER
# ─────────────────────────────────────────────────────────────────────────────
st.header("8. Confirmed Batch History")
st.caption(
    "All confirmed batches stored in GitHub. "
    "Batch numbers are derived from chronological order — "
    "deleting a batch automatically re-enumerates the remaining ones."
)

token, repo = _gh_creds()
if not token or not repo:
    st.warning("Configure GitHub credentials to view history.")
else:
    col_load, col_clear = st.columns([1, 4])
    with col_load:
        if st.button("🔄 Load / Refresh History", key="load_hist"):
            with st.spinner("Fetching history…"):
                st.session_state._batches     = get_all_batches(token, repo, HISTORY_FOLDER)
                st.session_state._updates_sha = None
                st.session_state._updates_df  = None

    batches = st.session_state._batches

    if batches is None:
        st.info("Click **Load / Refresh History** to view confirmed batches.")
    elif not batches:
        st.info("No confirmed batches found yet.")
    else:
        # Load updates log once for confirmed-updated counts
        if st.session_state._updates_df is None:
            with st.spinner("Loading updates log…"):
                sha, ulog = get_updates_log(token, repo)
            st.session_state._updates_sha = sha
            st.session_state._updates_df  = ulog
        ulog = st.session_state._updates_df

        # Build summary table
        rows = []
        for b in batches:
            confirmed_date = (
                b["confirmed_at"].strftime("%Y-%m-%d %H:%M") if b["confirmed_at"] else "—"
            )
            n_cases = len(b["df"])
            if not ulog.empty and "external_case_id" in ulog.columns:
                batch_eids = set(
                    b["df"]["External Case ID"].dropna().astype(str)
                ) if "External Case ID" in b["df"].columns else set()
                updated_count = ulog[
                    ulog["external_case_id"].astype(str).isin(batch_eids)
                ].shape[0]
            else:
                updated_count = 0

            rows.append({
                "Batch #":          b["number"],
                "Confirmed At":     confirmed_date,
                "Cases":            n_cases,
                "Confirmed Updated": updated_count,
                "_path":            b["path"],
                "_sha":             b["sha"],
            })

        header_cols = st.columns([1, 2, 1, 2, 1])
        header_cols[0].markdown("**Batch #**")
        header_cols[1].markdown("**Confirmed At**")
        header_cols[2].markdown("**Cases**")
        header_cols[3].markdown("**Updated in Pleteo**")
        header_cols[4].markdown("**Action**")
        st.divider()

        for row in rows:
            c1, c2, c3, c4, c5 = st.columns([1, 2, 1, 2, 1])
            c1.write(f"#{row['Batch #']}")
            c2.write(row["Confirmed At"])
            c3.write(row["Cases"])
            c4.write(row["Confirmed Updated"])
            if c5.button("🗑️ Delete", key=f"del_{row['_sha'][:8]}"):
                with st.spinner(f"Deleting Batch #{row['Batch #']}…"):
                    ok = delete_batch(token, repo, row["_path"], row["_sha"])
                if ok:
                    st.success(
                        f"Batch #{row['Batch #']} deleted. "
                        "Remaining batches will be re-enumerated on next refresh."
                    )
                    st.session_state._batches = None
                    st.rerun()
                else:
                    st.error("❌ Deletion failed.")
            st.divider()

        # ── Expand individual batch ───────────────────────────────────────────
        batch_nums = [b["number"] for b in batches]
        selected_num = st.selectbox(
            "Inspect batch cases",
            options=batch_nums,
            format_func=lambda n: f"Batch #{n}",
            key="inspect_batch",
        )
        selected_batch = next(b for b in batches if b["number"] == selected_num)
        disp_cols = [
            "External Case ID", "Name", "# Total Machines",
            "Last Event", "Updated", "Tags",
        ]
        st.dataframe(
            selected_batch["df"][[c for c in disp_cols if c in selected_batch["df"].columns]],
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            f"⬇️ Download Batch #{selected_num}",
            data=selected_batch["df"].to_csv(index=False).encode(),
            file_name=f"batch_{selected_num:03d}_{selected_batch['confirmed_at'].strftime('%Y%m%d')}.csv"
                      if selected_batch["confirmed_at"] else f"batch_{selected_num:03d}.csv",
            mime="text/csv",
        )

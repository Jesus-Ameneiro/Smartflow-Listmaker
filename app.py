"""
Smartflow Batch Selector
Ruvixx · LATAM Compliance Operations
"""

import io
import json
from datetime import date, datetime

import pandas as pd
import streamlit as st

from processor import (
    apply_filters,
    country_distribution,
    extract_all_tags,
    generate_summary,
    get_history_files,
    load_file,
    push_to_github,
    validate_against_history,
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

# Load once per filename; reset state when a new file is uploaded
if st.session_state.get("_loaded_name") != uploaded.name:
    try:
        df_raw = load_file(uploaded)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        st.stop()
    st.session_state._df_raw      = df_raw
    st.session_state._loaded_name = uploaded.name
    st.session_state._excluded    = set()
    st.session_state._output_df   = None
    st.session_state._confirmed   = False

df_raw = st.session_state._df_raw

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — SUMMARY
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Dataset Summary")

# Outdated threshold & today's date live at the top of the summary
c_thresh, c_today, _ = st.columns([1, 1, 2])
with c_thresh:
    outdated_months = st.number_input(
        "Outdated threshold (months)",
        min_value=1, max_value=60, value=6,
        help="Cases not updated within this many months are considered outdated.",
    )
with c_today:
    today_input = st.date_input("Today's date", value=date.today())

today_dt = datetime(today_input.year, today_input.month, today_input.day)
summary  = generate_summary(df_raw, config, outdated_months, today_dt)

# ── Dataset type badge ────────────────────────────────────────────────────────
ds_type = summary["dataset_type"]
type_badge = "🔵 Uninvestigated" if ds_type == "uninvestigated" else "🟠 Disqualified"
st.markdown(f"**Dataset type detected:** {type_badge}")

# ── Metric row ────────────────────────────────────────────────────────────────
m1, m2, m3 = st.columns(3)
m1.metric("Total Cases", summary["total_cases"])
m2.metric(f"Outdated (>{outdated_months} months)", summary["outdated_count"])
m3.metric("Countries Detected", len(summary["country_dist"]))

st.divider()

# ── Country distribution ───────────────────────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("📍 Cases per Country")
    if summary["country_dist"]:
        c_df = pd.DataFrame(
            list(summary["country_dist"].items()),
            columns=["Country", "Cases"],
        ).sort_values("Cases", ascending=False)
        st.dataframe(c_df, use_container_width=True, hide_index=True)
    else:
        st.info("No country tags identified in this dataset.")

with col_right:
    st.subheader("🏷️ Cases with Special Tags")
    if summary["special_tags"]:
        s_df = pd.DataFrame(
            list(summary["special_tags"].items()),
            columns=["Tag", "Cases"],
        ).sort_values("Cases", ascending=False)
        st.dataframe(s_df, use_container_width=True, hide_index=True)
    else:
        st.info("No special tags found.")

st.divider()

# ── Cases per year (Last Event) ───────────────────────────────────────────────
st.subheader("📅 Cases by Last Event Year")
if summary["cases_per_year"]:
    y_df = pd.DataFrame(
        list(summary["cases_per_year"].items()),
        columns=["Year", "Cases"],
    ).sort_values("Year", ascending=False)
    st.dataframe(y_df, use_container_width=True, hide_index=True)
else:
    st.info("Could not parse Last Event dates.")

st.divider()

# ── Data quality: unexpected values ──────────────────────────────────────────
st.subheader("⚠️ Data Quality — Unexpected Values")
unexp = summary["unexpected_values"]
if unexp:
    total_flagged = sum(len(v) for v in unexp.values())
    st.warning(
        f"{total_flagged} case(s) have values in columns that should be empty. "
        "Expand each column below for details."
    )
    for col, flagged_df in unexp.items():
        with st.expander(f"Column '{col}' — {len(flagged_df)} case(s)"):
            st.dataframe(flagged_df, use_container_width=True, hide_index=True)
else:
    st.success("✅ All monitored columns are clean — no unexpected values found.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — VALIDATE AGAINST HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Validate Against History")
st.caption(
    "Compare this dataset against previously confirmed batches stored in GitHub. "
    "Repeated cases will be automatically excluded from the output."
)

if st.button("🔎 Validate Dataset", use_container_width=False):
    try:
        token = st.secrets["GITHUB_TOKEN"]
        repo  = st.secrets["GITHUB_REPO"]
        with st.spinner("Fetching history files from GitHub…"):
            history_dfs = get_history_files(token, repo, HISTORY_FOLDER)
        repeated = validate_against_history(df_raw, history_dfs)
        st.session_state._excluded = repeated

        if repeated:
            st.warning(
                f"⚠️ {len(repeated)} repeated case(s) found in history "
                "— they will be excluded from the output."
            )
            rep_view = df_raw[
                df_raw["External Case ID"].astype(str).isin(repeated)
            ][["External Case ID", "Name"]].reset_index(drop=True)
            st.dataframe(rep_view, use_container_width=True, hide_index=True)
        else:
            if history_dfs:
                st.success(
                    f"✅ No repeated cases found across {len(history_dfs)} history file(s)."
                )
            else:
                st.info("No history files found yet — this appears to be the first batch.")

    except KeyError:
        st.error(
            "GitHub credentials are not configured. "
            "Add GITHUB_TOKEN and GITHUB_REPO to your Streamlit secrets."
        )
    except Exception as e:
        st.error(f"Validation error: {e}")

excluded_ids = st.session_state.get("_excluded", set())

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — PRIORITIZATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Prioritization & Filters")

# Working pool after exclusions
df_work = df_raw.copy()
if excluded_ids:
    df_work = df_work[
        ~df_work["External Case ID"].astype(str).isin(excluded_ids)
    ].reset_index(drop=True)
    st.info(
        f"ℹ️ {len(excluded_ids)} repeated case(s) excluded from the working pool. "
        f"Remaining: **{len(df_work)}** cases."
    )

all_tags = extract_all_tags(df_work)
country_counts = country_distribution(df_work, KNOWN_COUNTRIES)
country_options = list(country_counts.keys())

# ── Row 1: Machines + Last Event ─────────────────────────────────────────────
st.subheader("Machine & Event Filters")
r1c1, r1c2 = st.columns(2)
with r1c1:
    min_machines = st.number_input(
        "Minimum Total Machines",
        min_value=1, value=3,
        help="Exclude cases with fewer machines than this threshold.",
    )
with r1c2:
    last_event_cutoff = st.date_input(
        "Last Event — earliest allowed date",
        value=date(2023, 1, 1),
        help="Exclude cases whose last event is before this date.",
    )

# ── Row 2: Tag Filters ────────────────────────────────────────────────────────
st.subheader("Tag Filters")
r2c1, r2c2 = st.columns(2)
with r2c1:
    include_tags = st.multiselect(
        "Include cases that have ANY of these tags",
        options=all_tags,
        default=[],
        help="Leave empty to skip this filter.",
    )
with r2c2:
    exclude_tags = st.multiselect(
        "Exclude cases that have ANY of these tags",
        options=all_tags,
        default=[],
        help="Leave empty to skip this filter.",
    )

# ── Row 3: Sort by Updated ────────────────────────────────────────────────────
st.subheader("Sort Order — Updated Column")
sort_updated = st.radio(
    "Select cases by Update recency",
    options=["Oldest", "Newest", "Mixed"],
    index=1,
    horizontal=True,
    help=(
        "Oldest: prioritize least recently updated cases. "
        "Newest: prioritize most recently updated cases. "
        "Mixed: alternate between oldest and newest for a balanced selection."
    ),
).lower()

# ── Row 4: Country Distribution ───────────────────────────────────────────────
st.subheader("Country Distribution Filter")

if country_options:
    c_dist_df = pd.DataFrame(
        list(country_counts.items()), columns=["Country", "Cases"]
    ).sort_values("Cases", ascending=False)
    with st.expander("📊 View full country distribution", expanded=False):
        st.dataframe(c_dist_df, use_container_width=True, hide_index=True)

    filter_mode = st.radio(
        "Country filter mode",
        ["All countries", "Select specific countries", "Top N countries", "Top X% of countries"],
        horizontal=True,
    )

    country_filter = []
    if filter_mode == "Select specific countries":
        country_filter = st.multiselect(
            "Select countries to include in the output",
            options=country_options,
        )

    elif filter_mode == "Top N countries":
        max_n = len(country_options)
        top_n = st.number_input(
            "Number of top countries (ranked by case count)",
            min_value=1, max_value=max_n, value=min(3, max_n),
        )
        country_filter = country_options[: int(top_n)]
        st.info(f"Selected countries: {', '.join(country_filter)}")

    elif filter_mode == "Top X% of countries":
        top_pct = st.slider("Percentage of countries to include", 1, 100, 50)
        n = max(1, round(len(country_options) * top_pct / 100))
        country_filter = country_options[:n]
        st.info(f"Selected {top_pct}% = {n} country/ies: {', '.join(country_filter)}")
else:
    st.info("No country tags detected in this dataset — country filter unavailable.")
    filter_mode    = "All countries"
    country_filter = []

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — GENERATE BATCH
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Generate Batch")

case_count = st.number_input(
    "Number of cases to select",
    min_value=1,
    max_value=len(df_work) if len(df_work) > 0 else 1,
    value=min(50, len(df_work)),
)
if case_count > 100:
    st.warning(
        "⚠️ Smartflow allows a maximum of **100 case event downloads per day**. "
        "Selecting more than 100 may exceed your daily credit limit."
    )

if st.button("⚙️ Generate Batch", type="primary", use_container_width=True):
    filters = {
        "min_machines":      min_machines,
        "last_event_cutoff": last_event_cutoff,
        "include_tags":      include_tags,
        "exclude_tags":      exclude_tags,
        "country_filter":    country_filter,
        "sort_updated":      sort_updated,
    }
    with st.spinner("Applying filters…"):
        filtered = apply_filters(df_work, config, filters)

    if filtered.empty:
        st.warning(
            "⚠️ No cases match the current filter combination. "
            "Try relaxing one or more filters."
        )
        st.session_state._output_df = None
    else:
        output = filtered.head(int(case_count))
        st.session_state._output_df = output
        st.session_state._confirmed = False

# ── Output ────────────────────────────────────────────────────────────────────
if st.session_state.get("_output_df") is not None:
    output_df = st.session_state._output_df

    st.success(f"✅ **{len(output_df)}** case(s) selected.")

    display_cols = [
        "External Case ID", "Name", "# Total Machines", "# Recent Machines",
        "Case Tier", "Data Score", "Last Event", "Updated", "Tags",
    ]
    display_cols = [c for c in display_cols if c in output_df.columns]
    st.dataframe(output_df[display_cols], use_container_width=True, hide_index=True)

    # Download button
    csv_bytes = output_df.to_csv(index=False).encode()
    st.download_button(
        label="⬇️ Download Output CSV",
        data=csv_bytes,
        file_name=f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        mime="text/csv",
    )

    st.divider()

    # ─────────────────────────────────────────────────────────────────────────
    # SECTION 6 — CONFIRM & SAVE TO HISTORY
    # ─────────────────────────────────────────────────────────────────────────
    st.header("6. Confirm & Save to History")
    st.caption(
        "Confirming will push this batch to the GitHub repository as a permanent history record, "
        "so these cases will be excluded from future batch selections."
    )

    if not st.session_state.get("_confirmed"):
        if st.button("✅ Confirm & Push to GitHub History", type="primary"):
            try:
                token = st.secrets["GITHUB_TOKEN"]
                repo  = st.secrets["GITHUB_REPO"]
                with st.spinner("Pushing to GitHub…"):
                    success, filename = push_to_github(
                        output_df, token, repo, HISTORY_FOLDER
                    )
                if success:
                    st.session_state._confirmed = True
                    st.success(f"✅ Batch saved to history: `{filename}`")
                    st.balloons()
                else:
                    st.error(
                        "❌ GitHub returned an unexpected response. "
                        "Check your token permissions and repository name."
                    )
            except KeyError:
                st.error(
                    "GitHub credentials are not configured. "
                    "Add GITHUB_TOKEN and GITHUB_REPO to your Streamlit secrets."
                )
            except Exception as e:
                st.error(f"Push error: {e}")
    else:
        st.success("✅ This batch has already been confirmed and saved to history.")

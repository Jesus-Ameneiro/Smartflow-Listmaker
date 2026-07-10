"""
Comparator History & Blacklist
Ruvixx · LATAM Compliance Operations

Manages all confirmed batch history, blacklisted cases, and
provides case ID lookup against confirmed batches.
Session state is shared with the Smartflow Comparator page.
"""

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from github_manager import (
    add_to_blacklist,
    confirm_cases_updated,
    delete_batch,
    get_all_batches,
    get_blacklist,
    get_comp_updates_log,
    remove_from_blacklist,
)

# ── Config ────────────────────────────────────────────────────────────────────
HISTORY_FOLDER = "history_comparator"

st.set_page_config(
    page_title="Comparator History",
    page_icon="📋",
    layout="wide",
)
st.title("📋 Comparator History & Blacklist")
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

# ── Session state (shared with Comparator page) ───────────────────────────────
for k, v in {
    "_comp_batches":     None,
    "_blacklist_df":     None,
    "_blacklist_sha":    None,
    "_comp_updates_df":  None,
    "_comp_updates_sha": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

token, repo = _gh_creds()
if not token or not repo:
    st.warning("Configure GitHub credentials to use this page.")
    st.stop()

# Auto-load on first render
if st.session_state._comp_batches is None:
    with st.spinner("Loading history…"):
        st.session_state._comp_batches = get_all_batches(token, repo, HISTORY_FOLDER)

if st.session_state._blacklist_df is None:
    sha, bl_df = get_blacklist(token, repo)
    st.session_state._blacklist_sha = sha
    st.session_state._blacklist_df  = bl_df

if st.session_state._comp_updates_df is None:
    sha_ul, ul_df = get_comp_updates_log(token, repo)
    st.session_state._comp_updates_sha = sha_ul
    st.session_state._comp_updates_df  = ul_df

batches = st.session_state._comp_batches

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — BATCH HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("1. Confirmed Batch History")
st.caption(
    "All confirmed Comparator outputs stored in GitHub. "
    "Batch numbers are derived from chronological order — "
    "deleting a batch auto-renumbers the remaining ones."
)

if st.button("🔄 Refresh All", key="refresh_all"):
    with st.spinner("Refreshing…"):
        st.session_state._comp_batches    = get_all_batches(token, repo, HISTORY_FOLDER)
        sha, bl_df = get_blacklist(token, repo)
        st.session_state._blacklist_sha   = sha
        st.session_state._blacklist_df    = bl_df
        sha_ul, ul_df = get_comp_updates_log(token, repo)
        st.session_state._comp_updates_sha = sha_ul
        st.session_state._comp_updates_df  = ul_df
    st.rerun()

batches = st.session_state._comp_batches

if not batches:
    st.info("No confirmed batches found yet.")
else:
    total_cases_all = sum(len(b["df"]) for b in batches)
    hm1, hm2 = st.columns(2)
    hm1.metric("Total Confirmed Batches", len(batches))
    hm2.metric("Total Cases in History",  f"{total_cases_all:,}")

    st.divider()

    # ── Batch list ────────────────────────────────────────────────────────────
    hdr = st.columns([1, 2, 1, 1])
    hdr[0].markdown("**Batch #**")
    hdr[1].markdown("**Confirmed At**")
    hdr[2].markdown("**Cases**")
    hdr[3].markdown("**Action**")
    st.divider()

    for b in batches:
        c1, c2, c3, c4 = st.columns([1, 2, 1, 1])
        c1.write(f"#{b['number']}")
        c2.write(b["confirmed_at"].strftime("%Y-%m-%d %H:%M") if b["confirmed_at"] else "—")
        c3.write(len(b["df"]))
        if c4.button("🗑️ Delete", key=f"del_hist_{b['sha'][:8]}"):
            with st.spinner(f"Deleting Batch #{b['number']}…"):
                ok = delete_batch(token, repo, b["path"], b["sha"])
            if ok:
                st.success(f"Batch #{b['number']} deleted.")
                st.session_state._comp_batches = None
                st.rerun()
            else:
                st.error("❌ Deletion failed.")
        st.divider()

    # ── Inspect + Download ────────────────────────────────────────────────────
    sel_num = st.selectbox(
        "Inspect batch",
        options=[b["number"] for b in batches],
        format_func=lambda n: f"Batch #{n}",
        key="inspect_batch_hist",
    )
    sel_batch = next(b for b in batches if b["number"] == sel_num)
    st.dataframe(sel_batch["df"], width="stretch", hide_index=True)

    dl1, dl2 = st.columns(2)
    dl1.download_button(
        f"⬇️ Download Batch #{sel_num}",
        data=sel_batch["df"].to_csv(index=False).encode(),
        file_name=f"comparator_batch_{sel_num:03d}.csv",
        mime="text/csv",
        key=f"dl_batch_{sel_num}",
    )

    # ── Download all combined ─────────────────────────────────────────────────
    combined_parts = []
    for b in batches:
        part = b["df"].copy()
        part.insert(0, "Batch #", b["number"])
        part.insert(1, "Confirmed At",
            b["confirmed_at"].strftime("%Y-%m-%d %H:%M") if b["confirmed_at"] else "—"
        )
        combined_parts.append(part)
    combined_df = pd.concat(combined_parts, ignore_index=True)
    dl2.download_button(
        f"⬇️ Download All {len(batches)} Batches ({total_cases_all:,} cases)",
        data=combined_df.to_csv(index=False).encode(),
        file_name=f"comparator_all_batches_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
        key="dl_all_hist",
        type="primary",
    )

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — BLACKLIST MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Black List")
st.caption(
    "Cases in the black list are automatically excluded from all future "
    "Comparator output generation. Cases can be added from the Comparator "
    "preview or managed here."
)

bl_df = st.session_state._blacklist_df

if bl_df is None or bl_df.empty:
    st.info("The black list is empty.")
else:
    st.metric("Black Listed Cases", len(bl_df))
    st.dataframe(
        bl_df.rename(columns={
            "case_id":           "Case ID",
            "organization_name": "Organization",
            "country":           "Country",
            "added_at":          "Added At",
        }),
        width="stretch", hide_index=True,
    )

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
            ok, updated = remove_from_blacklist(remove_ids, token, repo)
        if ok:
            st.session_state._blacklist_df  = updated
            st.session_state._blacklist_sha = None
            st.success(f"✅ Removed {len(remove_ids)} case(s).")
            st.rerun()
        else:
            st.error("❌ Failed to update black list.")

st.divider()

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — CASE ID LOOKUP
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Verify Case IDs Against History")
st.caption(
    "Paste Case IDs to check whether they exist in any confirmed batch, "
    "and optionally confirm them as updated in Pleteo."
)

lookup_input = st.text_area(
    "Case IDs (comma-separated)",
    placeholder="e.g. 648494#1, 957340#1, 884133#1",
    height=90,
    key="lookup_input",
)

if st.button("🔎 Verify", key="lookup_btn") and lookup_input.strip():
    query_ids = {x.strip() for x in lookup_input.split(",") if x.strip()}
    batches_now = st.session_state._comp_batches or []

    # Build index
    id_to_batches = {}
    for b in batches_now:
        col = next((c for c in ["Case ID", "case_id"] if c in b["df"].columns), None)
        if col is None:
            continue
        for cid in b["df"][col].dropna().astype(str):
            if cid in query_ids:
                id_to_batches.setdefault(cid, []).append(b)

    # Load updates log if needed
    if st.session_state._comp_updates_df is None:
        sha_ul, ul_df = get_comp_updates_log(token, repo)
        st.session_state._comp_updates_sha = sha_ul
        st.session_state._comp_updates_df  = ul_df
    already_updated = set()
    if st.session_state._comp_updates_df is not None:
        already_updated = set(
            st.session_state._comp_updates_df["case_id"].astype(str)
        )

    rows = []
    for qid in sorted(query_ids):
        if qid in id_to_batches:
            for b in id_to_batches[qid]:
                col = next((c for c in ["Case ID","case_id"] if c in b["df"].columns), None)
                org_col = next((c for c in ["Organization Name","organization_name"] if c in b["df"].columns), None)
                org = "—"
                if col and org_col:
                    match = b["df"][b["df"][col].astype(str) == qid]
                    if not match.empty:
                        org = match.iloc[0][org_col]
                upd_status = "✅ Confirmed Updated" if qid in already_updated else "⏳ Pending Update"
                rows.append({
                    "Case ID":      qid,
                    "Organization": org,
                    "Batch #":      f"#{b['number']}",
                    "Confirmed At": b["confirmed_at"].strftime("%Y-%m-%d %H:%M") if b["confirmed_at"] else "—",
                    "Batch Status": "✅ In History",
                    "Update Status": upd_status,
                })
        else:
            rows.append({
                "Case ID":      qid,
                "Organization": "—",
                "Batch #":      "—",
                "Confirmed At": "—",
                "Batch Status": "❌ Not Found",
                "Update Status": "—",
            })

    result_df = pd.DataFrame(rows)
    st.session_state._lookup_result = result_df

lookup_result = st.session_state.get("_lookup_result")
if lookup_result is not None and not lookup_result.empty:
    found    = (lookup_result["Batch Status"] == "✅ In History").sum()
    notfound = (lookup_result["Batch Status"] == "❌ Not Found").sum()
    pending  = (lookup_result["Update Status"] == "⏳ Pending Update").sum()

    lm1, lm2, lm3, lm4 = st.columns(4)
    lm1.metric("Queried",   len(lookup_result))
    lm2.metric("Found",     found)
    lm3.metric("Not Found", notfound)
    lm4.metric("Pending Update", pending)

    st.dataframe(lookup_result, width="stretch", hide_index=True)

    # Confirm as updated
    pending_df = lookup_result[lookup_result["Update Status"] == "⏳ Pending Update"]
    if not pending_df.empty:
        st.subheader("Confirm as Updated in Pleteo")
        to_confirm = st.multiselect(
            "Select cases to confirm as updated",
            options=pending_df["Case ID"].tolist(),
            default=pending_df["Case ID"].tolist(),
            key="confirm_upd_lookup",
        )
        if to_confirm and st.button(
            f"✅ Confirm {len(to_confirm)} Case(s) as Updated",
            type="primary",
            key="confirm_upd_lookup_btn",
        ):
            # Build rows for confirmation
            id_to_batch_info = {}
            for b in (st.session_state._comp_batches or []):
                col = next((c for c in ["Case ID","case_id"] if c in b["df"].columns), None)
                org_c = next((c for c in ["Organization Name","organization_name"] if c in b["df"].columns), None)
                if not col: continue
                for cid in b["df"][col].dropna().astype(str):
                    if cid not in id_to_batch_info:
                        org_v = "—"
                        if org_c:
                            m = b["df"][b["df"][col].astype(str) == cid]
                            if not m.empty: org_v = m.iloc[0][org_c]
                        id_to_batch_info[cid] = {"batch_number": b["number"], "organization_name": org_v}

            conf_rows = [
                {
                    "case_id":           cid,
                    "organization_name": id_to_batch_info.get(cid, {}).get("organization_name", "—"),
                    "country":           "—",
                    "batch_number":      id_to_batch_info.get(cid, {}).get("batch_number", "—"),
                }
                for cid in to_confirm
            ]
            with st.spinner("Saving…"):
                ok, updated_ul = confirm_cases_updated(conf_rows, token, repo)
            if ok:
                st.session_state._comp_updates_df  = updated_ul
                st.session_state._comp_updates_sha = None
                st.success(f"✅ {len(to_confirm)} case(s) confirmed as updated.")
                st.session_state._lookup_result = None
                st.rerun()
            else:
                st.error("❌ Failed to save.")

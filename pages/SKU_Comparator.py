"""
Smartflow Comparator — SketchUp
Ruvixx · LATAM Compliance Operations

Compares a Smartflow download against a Pleteo export to surface:
  • Difference cases  — in Smartflow, missing from Pleteo
  • Outdated cases    — in both, but Smartflow Last Event is newer

Interaction model
-----------------
Widgets only RECORD choices. Every heavy step runs inside a button and its
result is stored in session state:
  Process Files -> Validate (optional) -> Run Comparison -> Apply Filters
  -> Generate Preview -> Generate Output File -> Confirm
Changing a widget re-runs the page, but nothing expensive is recomputed.
Each step clears everything downstream of it, so stale results never mix.
"""

from datetime import date, datetime

import pandas as pd
import streamlit as st

from comparator import (
    add_case_type_column,
    apply_output_filters,
    build_output,
    extract_tags_from_outdated,
    find_difference_cases,
    find_outdated_cases,
    load_pleteo,
    load_smartflow,
    outdated_status_report,
    select_by_country_distribution,
    validate_comparator_history,
)
from github_manager import (
    add_to_blacklist,
    delete_draft,
    get_all_batches,
    get_blacklist,
    load_draft,
    load_tag_preferences,
    push_batch,
    save_draft,
    save_tag_preferences,
)

# ── Page ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Smartflow Comparator",
    page_icon="🔄",
    layout="wide",
)
st.title("🔄 Smartflow Comparator")
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
        st.error(
            "GitHub credentials not configured. "
            "Add **GITHUB_TOKEN** and **GITHUB_REPO** to your Streamlit secrets."
        )
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

# Every session key this product's pages own. Cleared when the user arrives from
# another product. SketchUp-only keys (_sku_excl_map, _pr_*) deliberately do not
# reuse the names the Tekla pages use, so nothing can leak between products.
_PRODUCT_DATA_KEYS = [
    # shared with the (unchanged) Tekla pages — same names, same meaning
    '_sf_df', '_pl_df', '_sf_name', '_pl_name', '_diff_df', '_outd_df',
    '_pre_result', '_output_df', '_confirmed_comp', '_comp_batches',
    '_excl_preview', '_focused_pool', '_country_alloc', '_req_count',
    '_blacklist_df', '_blacklist_sha', '_draft_sha', '_draft_restored',
    '_tag_prefs', '_comp_updates_df', '_comp_updates_sha', '_history',
    '_confirmed_ids', '_lookup_result', '_results_df', '_outdated_df',
    '_confirmed', '_sel_reset', '_excl_map',
    # SketchUp Comparator only
    '_sku_excl_map', '_val_params', '_cmp_info', '_opt_statuses', '_opt_tags',
    '_mach_bounds', '_applied_cfg', '_show_diff_tbl', '_show_outd_tbl',
    '_show_excl_tbl', '_draft_checked',
    # SketchUp Prioritizer only (listed here so either page clears them)
    '_pr_sf_df', '_pr_pl_df', '_pr_sf_name', '_pr_pl_name', '_pr_results_df',
    '_pr_outdated_df', '_pr_output_df', '_pr_confirmed', '_pr_verified_ids',
]
if st.session_state.get("_active_product") != PRODUCT:
    for _k in _PRODUCT_DATA_KEYS:
        st.session_state.pop(_k, None)
    st.session_state["_active_product"] = PRODUCT

for k, v in {
    "_sf_df":            None,
    "_pl_df":            None,
    "_sf_name":          None,
    "_pl_name":          None,
    "_diff_df":          None,
    "_outd_df":          None,
    "_sku_excl_map":     {},       # history-validation result (SketchUp only)
    "_val_params":       None,     # window/date used at the last Validate click
    "_cmp_info":         None,     # {"pool", "diff", "outd"} from Run Comparison
    "_opt_statuses":     [],       # status options, fixed at Run Comparison
    "_opt_tags":         [],       # tag options, fixed at Run Comparison
    "_mach_bounds":      None,     # (min, max) machines, fixed at Run Comparison
    "_applied_cfg":      None,     # snapshot + funnel + country counts from Apply
    "_focused_pool":     None,     # pool produced by Apply Filters
    "_pre_result":       None,
    "_output_df":        None,
    "_confirmed_comp":   False,
    "_comp_batches":     None,
    "_excl_preview":     set(),    # Case IDs excluded in the current preview
    "_country_alloc":    {},       # allocation used for the current preview
    "_req_count":        0,
    "_blacklist_df":     None,
    "_blacklist_sha":    None,
    "_comp_updates_df":  None,
    "_comp_updates_sha": None,
    "_draft_sha":        None,
    "_draft_restored":   False,
    "_draft_checked":    False,    # draft is looked up ONCE per session
    "_sel_reset":        0,
    "_tag_prefs":        None,
    "_show_diff_tbl":    False,
    "_show_outd_tbl":    False,
    "_show_excl_tbl":    False,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Helpers ───────────────────────────────────────────────────────────────────
_FILTER_WIDGET_KEYS = (
    "reinv_statuses", "outd_inc_tags", "outd_exc_tags",
    "comp_mach_min", "comp_mach_max",
)


def _clear_country_widgets(keep=None):
    """Drop country selection + allocation inputs (optionally keep valid ones)."""
    if keep is None:
        st.session_state.pop("out_countries", None)
        for _k in [k for k in st.session_state if str(k).startswith("alloc_")]:
            del st.session_state[_k]


def _reset_from(stage):
    """
    Clear everything downstream of `stage`:
      'comparison' -> comparison results, filter options, applied pool, preview
      'applied'    -> applied pool, country choices, preview
      'preview'    -> preview only
    The finished OUTPUT file is never touched here: it is a separate artefact
    (and may be a recovered draft) that must survive re-processing.
    """
    if stage == "comparison":
        st.session_state._diff_df       = None
        st.session_state._outd_df       = None
        st.session_state._cmp_info      = None
        st.session_state._opt_statuses  = []
        st.session_state._opt_tags      = []
        st.session_state._mach_bounds   = None
        st.session_state._show_diff_tbl = False
        st.session_state._show_outd_tbl = False
        for _k in _FILTER_WIDGET_KEYS:
            st.session_state.pop(_k, None)
        stage = "applied"
    if stage == "applied":
        st.session_state._focused_pool = None
        st.session_state._applied_cfg  = None
        _clear_country_widgets()
        stage = "preview"
    if stage == "preview":
        st.session_state._pre_result    = None
        st.session_state._excl_preview  = set()
        st.session_state._country_alloc = {}
        st.session_state._req_count     = 0


def _sf_pool_now():
    """Smartflow rows minus the cases excluded by the last history validation."""
    sf = st.session_state._sf_df
    excl = set((st.session_state._sku_excl_map or {}).keys())
    return sf[~sf["_case_id"].isin(excl)] if excl else sf


def _toggle_button(label_show, label_hide, flag_key, btn_key, disabled=False):
    """A button that shows/hides a list. Returns whether the list is shown."""
    shown = st.session_state.get(flag_key, False)
    if st.button(label_hide if shown else label_show, key=btn_key, disabled=disabled):
        shown = not shown
        st.session_state[flag_key] = shown
    return shown and not disabled


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — UPLOAD & PROCESS FILES
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

_both_uploaded = sf_upload is not None and pl_upload is not None
_already_processed = bool(
    _both_uploaded
    and st.session_state._sf_df is not None
    and st.session_state._pl_df is not None
    and sf_upload.name == st.session_state._sf_name
    and pl_upload.name == st.session_state._pl_name
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
        with st.spinner(f"Reading Smartflow ({sf_upload.size / 1e6:.1f} MB)…"):
            _sf_new = load_smartflow(sf_upload.getvalue())
    except Exception as e:
        _errs.append(f"Smartflow file error: {e}")
    if not _errs:       # fail fast: don't spend a minute on Pleteo if Smartflow is unreadable
        try:
            with st.spinner(f"Reading Pleteo ({pl_upload.size / 1e6:.1f} MB)… please wait"):
                _pl_new = load_pleteo(pl_upload.getvalue(), pl_upload.name)
        except Exception as e:
            _errs.append(f"Pleteo file error: {e}")

    if _errs:
        for _m in _errs:
            st.error(_m)
    else:
        st.session_state._sf_df          = _sf_new
        st.session_state._pl_df          = _pl_new
        st.session_state._sf_name        = sf_upload.name
        st.session_state._pl_name        = pl_upload.name
        st.session_state._sku_excl_map   = {}
        st.session_state._val_params     = None
        st.session_state._show_excl_tbl  = False
        _reset_from("comparison")
        st.rerun()

sf_df = st.session_state._sf_df
pl_df = st.session_state._pl_df

# Stale-data guard: a frame without the computed columns came from older code.
_required_sf = {"_last_event", "_case_id", "_country", "_machines"}
_required_pl = {"_last_event", "_case_id"}
if sf_df is not None and not _required_sf.issubset(sf_df.columns):
    st.session_state._sf_df, st.session_state._sf_name = None, None
    sf_df = None
    st.warning("⚠️ Cached Smartflow data was outdated — please re-upload and process it.")
if pl_df is not None and not _required_pl.issubset(pl_df.columns):
    st.session_state._pl_df, st.session_state._pl_name = None, None
    pl_df = None
    st.warning("⚠️ Cached Pleteo data was outdated — please re-upload and process it.")

files_ready = sf_df is not None and pl_df is not None

if files_ready:
    fi1, fi2 = st.columns(2)
    _sf_min, _sf_max = sf_df["_last_event"].min(), sf_df["_last_event"].max()
    _pl_min, _pl_max = pl_df["_last_event"].min(), pl_df["_last_event"].max()
    _fmt = lambda d: d.strftime("%Y-%m-%d") if pd.notna(d) else "—"
    fi1.success(
        f"📡 **Smartflow** — `{st.session_state._sf_name}`\n\n"
        f"- Cases: **{len(sf_df):,}**\n"
        f"- Unique Case IDs: **{sf_df['_case_id'].nunique():,}**\n"
        f"- Last Event range: **{_fmt(_sf_min)}** → **{_fmt(_sf_max)}**"
    )
    fi2.success(
        f"📋 **Pleteo** — `{st.session_state._pl_name}`\n\n"
        f"- Cases: **{len(pl_df):,}**\n"
        f"- Unique Case IDs: **{pl_df['_case_id'].nunique():,}**\n"
        f"- Last Event range: **{_fmt(_pl_min)}** → **{_fmt(_pl_max)}**"
    )
    if len(pl_df) < 5000:
        st.warning(
            f"⚠️ The Pleteo file contains only **{len(pl_df):,}** cases. "
            "If this is a partial or filtered export, Difference cases will be inflated — "
            "cases that exist in the full Pleteo database will appear as missing. "
            "**Upload the complete Pleteo export before running the comparison.**"
        )

# ── Silent once-per-session loads (history, black list, tag defaults) ─────────
_bg_token, _bg_repo = _gh_creds()
if _bg_token and _bg_repo:
    if st.session_state._comp_batches is None:
        with st.spinner("Loading history for validation…"):
            st.session_state._comp_batches = get_all_batches(
                _bg_token, _bg_repo, HISTORY_FOLDER
            )
    if st.session_state._blacklist_df is None:
        _sha, _bl = get_blacklist(_bg_token, _bg_repo, path=BLACKLIST_PATH)
        st.session_state._blacklist_sha = _sha
        st.session_state._blacklist_df  = _bl
    if st.session_state._tag_prefs is None:
        st.session_state._tag_prefs = load_tag_preferences(
            _bg_token, _bg_repo, path=TAG_PREFS_PATH
        )

# ── Draft recovery: looked up ONCE per session (not on every widget click) ────
if (
    _bg_token and _bg_repo
    and not st.session_state._draft_checked
    and st.session_state._output_df is None
    and not st.session_state._draft_restored
):
    st.session_state._draft_checked = True
    _d_sha, _d_df = load_draft(_bg_token, _bg_repo, path=DRAFT_PATH)
    if _d_df is not None:
        if "Case Type" not in _d_df.columns:       # draft saved before Case Type existed
            _d_df = add_case_type_column(_d_df)
        st.session_state._output_df      = _d_df
        st.session_state._draft_sha      = _d_sha
        st.session_state._draft_restored = True
        st.session_state._confirmed_comp = False

if st.session_state._draft_restored and st.session_state._output_df is not None:
    _bc1, _bc2 = st.columns([5, 1])
    _bc1.warning(
        "📋 **Draft recovered** — your previous output is available in **Section 5** "
        "(download) and **Section 6** (confirm). Generate a new preview to replace it, "
        "or discard it."
    )
    if _bc2.button("🗑️ Discard Draft", key="discard_draft_btn", use_container_width=True):
        if _bg_token and _bg_repo:
            delete_draft(_bg_token, _bg_repo, path=DRAFT_PATH)
        st.session_state._output_df      = None
        st.session_state._draft_sha      = None
        st.session_state._draft_restored = False
        st.session_state._confirmed_comp = False
        st.rerun()

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — VALIDATE AGAINST HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("2. Validate Against History")
st.caption(
    "Cases confirmed within the expiration window will be excluded. "
    "Cases outside the window are eligible for inclusion again. "
    "This step is optional."
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

col_val, col_ref, _ = st.columns([1, 1, 3])
validate_clicked = col_val.button(
    "🔎 Validate Against History", key="validate_comp", disabled=not files_ready
)
refresh_clicked = col_ref.button("🔄 Refresh History Cache", key="refresh_comp")

if validate_clicked:
    token, repo = _require_creds()
    with st.spinner("Fetching comparator history from GitHub…"):
        _batches = get_all_batches(token, repo, HISTORY_FOLDER)
    st.session_state._comp_batches = _batches
    st.session_state._sku_excl_map = validate_comparator_history(
        set(sf_df["_case_id"].dropna()), _batches, exp_months, today_comp_dt
    )
    st.session_state._val_params = {
        "months": int(exp_months),
        "today":  today_comp.isoformat(),
        "files":  len(_batches),
    }
    st.session_state._show_excl_tbl = False
    _reset_from("comparison")           # the pool changed -> comparison is stale

if refresh_clicked:
    st.session_state._comp_batches = None
    st.rerun()

excl_map = st.session_state._sku_excl_map
_vp = st.session_state._val_params
if _vp is None:
    st.info("ℹ️ History validation has not been applied — every case stays eligible.")
else:
    if (int(exp_months), today_comp.isoformat()) != (_vp["months"], _vp["today"]):
        st.caption(
            "⚠️ The window or date changed since the last validation — "
            "click **Validate Against History** to apply it."
        )
    if excl_map:
        st.warning(
            f"⚠️ **{len(excl_map)}** case(s) found in history within the last "
            f"{_vp['months']} month(s) — they are excluded from the output pool."
        )
        if _toggle_button(
            f"📋 Show {len(excl_map):,} excluded case(s)", "🙈 Hide excluded cases",
            "_show_excl_tbl", "tog_excl",
        ):
            _names = sf_df.drop_duplicates("_case_id").set_index("_case_id")["Organization Name"]
            _ids = list(excl_map.keys())
            st.dataframe(
                pd.DataFrame({
                    "Case ID":      _ids,
                    "Organization": [_names.get(i, "—") for i in _ids],
                    "Batch(es)":    [", ".join(f"#{n}" for n in excl_map[i]["batch_numbers"]) for i in _ids],
                    "Last Stored":  [excl_map[i]["stored_at"].strftime("%Y-%m-%d %H:%M") for i in _ids],
                    "Times Stored": [excl_map[i]["times_stored"] for i in _ids],
                }),
                width="stretch", hide_index=True,
            )
    else:
        st.success(
            f"✅ No recent repeated cases found across {_vp['files']} history file(s)."
            if _vp["files"] else
            "✅ No history found — this will be the first comparator output."
        )

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — COMPARISON REPORT
# ─────────────────────────────────────────────────────────────────────────────
st.header("3. Comparison Report")

run_clicked = st.button(
    "🔍 Run Comparison", type="primary", key="run_comparison", disabled=not files_ready
)
if run_clicked:
    _pool = _sf_pool_now()
    with st.spinner("Comparing files…"):
        _diff = find_difference_cases(_pool, pl_df)
        _outd = find_outdated_cases(_pool, pl_df)
        _statuses = sorted(outdated_status_report(_outd, pl_df).keys())
        _tags = extract_tags_from_outdated(_outd)
    _mach = _pool["_machines"].dropna()
    _bmin = int(_mach.min()) if len(_mach) else 1
    _bmax = int(_mach.max()) if len(_mach) else 100

    _reset_from("comparison")           # clears widgets + everything downstream
    st.session_state._diff_df      = _diff
    st.session_state._outd_df      = _outd
    st.session_state._cmp_info     = {"pool": len(_pool), "diff": len(_diff), "outd": len(_outd)}
    st.session_state._opt_statuses = _statuses
    st.session_state._opt_tags     = _tags
    st.session_state._mach_bounds  = (_bmin, _bmax)

    # Seed the Section 4 widgets for this comparison (before they are drawn)
    _prefs = st.session_state._tag_prefs or {"include": [], "exclude": []}
    st.session_state["reinv_statuses"] = list(_statuses)
    st.session_state["outd_inc_tags"]  = [t for t in _prefs.get("include", []) if t in _tags]
    st.session_state["outd_exc_tags"]  = [t for t in _prefs.get("exclude", []) if t in _tags]
    st.session_state["comp_mach_min"]  = _bmin
    st.session_state["comp_mach_max"]  = _bmax

diff_df   = st.session_state._diff_df
outd_df   = st.session_state._outd_df
cmp_ready = diff_df is not None and outd_df is not None

if not cmp_ready:
    st.info(
        "Click **Run Comparison** to analyse both files."
        if files_ready else "Process both files first (Section 1)."
    )
else:
    _ci = st.session_state._cmp_info
    m1, m2, m3 = st.columns(3)
    m1.metric("Difference Cases", f"{_ci['diff']:,}", help="In Smartflow, missing from Pleteo")
    m2.metric("Outdated Cases",   f"{_ci['outd']:,}", help="In both; Smartflow Last Event is newer")
    m3.metric("Available Pool",   f"{_ci['pool']:,}")

    tb1, tb2, _ = st.columns([1, 1, 2])
    with tb1:
        _show_diff = _toggle_button(
            f"📋 Show Difference list ({_ci['diff']:,})", "🙈 Hide Difference list",
            "_show_diff_tbl", "tog_diff",
        )
    with tb2:
        _show_outd = _toggle_button(
            f"⏰ Show Outdated list ({_ci['outd']:,})", "🙈 Hide Outdated list",
            "_show_outd_tbl", "tog_outd",
        )

    if _show_diff:
        _dc = ["Case ID", "Organization Name", "Country", "No. ofMachines", "Last Event", "Case Status"]
        st.dataframe(diff_df[[c for c in _dc if c in diff_df.columns]], width="stretch", hide_index=True)
    if _show_outd:
        _od = outd_df.copy()
        _od["Smartflow Last Event"] = _od["_last_event"].dt.strftime("%Y-%m-%d")
        _od["Pleteo Last Event"]    = pd.to_datetime(
            _od["_pleteo_last_event"], errors="coerce"
        ).dt.strftime("%Y-%m-%d")
        _od["Investigation Status"] = _od["_inv_status"].fillna("—")
        _od["Investigator"]         = _od["_investigator"].fillna("—")
        _oc = ["Case ID", "Organization Name", "Country", "No. ofMachines",
               "Smartflow Last Event", "Pleteo Last Event",
               "Investigation Status", "Investigator", "Case Status"]
        st.dataframe(_od[[c for c in _oc if c in _od.columns]], width="stretch", hide_index=True)

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — OUTPUT CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
st.header("4. Output Configuration")
st.caption(
    "Choose your filters, then click **Apply Filters**. "
    "Nothing is calculated until you do."
)

_opt_statuses = st.session_state._opt_statuses
_opt_tags     = st.session_state._opt_tags
_bmin, _bmax  = st.session_state._mach_bounds or (1, 100)

# ── Focus ─────────────────────────────────────────────────────────────────────
st.subheader("Focus")
_focus_choice = st.radio(
    "Select case type to include in output",
    options=["Difference", "Outdated"],
    horizontal=True,
    key="focus_radio",
    disabled=not cmp_ready,
)
focus_outd = _focus_choice == "Outdated"

# ── Outdated filters (always visible; used only when Focus = Outdated) ────────
st.subheader("🔁 Outdated — Investigation Status Filter")
st.caption(
    "Applies only when Focus = **Outdated**. **No Status** is a regular option. "
    "If nothing is selected, all outdated cases are included."
)
include_statuses = st.multiselect(
    "Include cases with these statuses",
    options=_opt_statuses,
    key="reinv_statuses",
    disabled=not cmp_ready,
    help="Deselect any status to exclude those cases from the outdated pool.",
)
if cmp_ready and focus_outd and not include_statuses:
    st.warning("⚠️ No statuses selected — all outdated cases will be included.")

st.subheader("🏷️ Outdated — Tag Filter")
st.caption(
    "Applies only when Focus = **Outdated**. Include and exclude filters can be "
    "combined. Use 💾 to save your selection as the default for future sessions."
)
tf1, tf2 = st.columns(2)
with tf1:
    include_tags = st.multiselect(
        "Include — case must have ANY of these tags",
        options=_opt_tags,
        key="outd_inc_tags",
        disabled=not cmp_ready,
        help="Leave empty to skip this filter.",
    )
with tf2:
    exclude_tags = st.multiselect(
        "Exclude — remove cases with ANY of these tags",
        options=_opt_tags,
        key="outd_exc_tags",
        disabled=not cmp_ready,
        help="Leave empty to skip this filter.",
    )
tc1, tc2 = st.columns([1, 5])
if tc1.button("💾 Save as Default", key="save_tag_prefs", disabled=not cmp_ready):
    _tp_token, _tp_repo = _gh_creds()
    if _tp_token and _tp_repo:
        _ok = save_tag_preferences(
            include_tags, exclude_tags, _tp_token, _tp_repo, path=TAG_PREFS_PATH
        )
        if _ok:
            st.session_state._tag_prefs = {"include": include_tags, "exclude": exclude_tags}
            tc2.success("✅ Tag preferences saved.")
        else:
            tc2.error("❌ Failed to save preferences.")
if cmp_ready and not focus_outd:
    st.caption("ℹ️ Focus is **Difference** — the two Outdated filters above are ignored on Apply.")

st.divider()

# ── Machine filter ────────────────────────────────────────────────────────────
st.subheader("Machine Filter")
st.caption("Keep only cases whose machine count falls inside this range.")
mf1, mf2 = st.columns(2)
with mf1:
    mach_min = st.number_input(
        "Minimum Machines", min_value=_bmin, max_value=_bmax, value=_bmin,
        key="comp_mach_min", disabled=not cmp_ready,
    )
with mf2:
    mach_max = st.number_input(
        "Maximum Machines", min_value=_bmin, max_value=_bmax, value=_bmax,
        key="comp_mach_max", disabled=not cmp_ready,
    )
if cmp_ready:
    if mach_max < mach_min:
        st.warning("⚠️ Maximum is less than minimum — no cases will match.")
    elif mach_max == mach_min:
        st.info(f"Exact match: only cases with **{mach_min}** machine(s).")

st.divider()

# ── Website category filters ──────────────────────────────────────────────────
st.subheader("🌐 Website Category Filters")
st.caption(
    "Exclude cases based on their Smartflow website domain. Applies to both "
    "Difference and Outdated cases — more reliable than Pleteo tags since it "
    "covers cases with no tags at all."
)
_web_cols = st.columns(3)
_excl_edu = _web_cols[0].toggle(
    "🎓 Exclude Educational", value=False, key="excl_edu",
    help="Excludes domains with .edu or .ac. extensions.",
)
_excl_gov = _web_cols[1].toggle(
    "🏛️ Exclude Government", value=False, key="excl_gov",
    help="Excludes domains with .gov, .gob, or .mil extensions.",
)
_excl_npo = _web_cols[2].toggle(
    "🤝 Exclude Non-Profit / Org", value=False, key="excl_npo",
    help="Excludes domains with .org extension.",
)

st.divider()

# ── Apply Filters ─────────────────────────────────────────────────────────────
# The Outdated-only selections are ignored (and must not count as "changes")
# when the focus is Difference.
_snapshot = (
    _focus_choice,
    tuple(sorted(include_statuses)) if focus_outd else (),
    tuple(sorted(include_tags))     if focus_outd else (),
    tuple(sorted(exclude_tags))     if focus_outd else (),
    int(mach_min), int(mach_max),
    bool(_excl_edu), bool(_excl_gov), bool(_excl_npo),
)

ap1, ap2 = st.columns([1, 3])
apply_clicked = ap1.button(
    "✅ Apply Filters", type="primary", key="apply_filters", disabled=not cmp_ready
)

if apply_clicked:
    _bl = st.session_state._blacklist_df
    _bl_ids = set(_bl["case_id"].astype(str)) if (_bl is not None and not _bl.empty) else set()
    with st.spinner("Applying filters…"):
        _pool, _steps = apply_output_filters(
            _sf_pool_now(), diff_df, outd_df, _focus_choice,
            include_statuses=include_statuses,
            include_tags=include_tags, exclude_tags=exclude_tags,
            blacklist_ids=_bl_ids,
            mach_min=int(mach_min), mach_max=int(mach_max),
            excl_edu=_excl_edu, excl_gov=_excl_gov, excl_npo=_excl_npo,
        )
    _vc = _pool["_country"].value_counts()
    _counts = {str(c): int(n) for c, n in _vc.items() if n > 0}

    # Keep country choices that are still valid; clamp their allocations.
    _prev_sel = list(st.session_state.get("out_countries", []))
    st.session_state["out_countries"] = [c for c in _prev_sel if c in _counts]
    for _c in _prev_sel:
        _key = f"alloc_{_c}"
        if _c not in _counts:
            st.session_state.pop(_key, None)
        elif _key in st.session_state and st.session_state[_key] > _counts[_c]:
            st.session_state[_key] = _counts[_c]

    _reset_from("preview")              # a new pool makes the old preview stale
    st.session_state._focused_pool = _pool
    st.session_state._applied_cfg  = {
        "snapshot":       _snapshot,
        "focus":          _focus_choice,
        "steps":          _steps,
        "country_counts": _counts,
    }

applied = st.session_state._applied_cfg
filters_in_sync = applied is not None and applied["snapshot"] == _snapshot

with ap2:
    if not cmp_ready:
        st.caption("Run the comparison first (Section 3).")
    elif applied is None:
        st.caption("Click **Apply Filters** to build the pool.")
    elif filters_in_sync:
        st.success(
            f"✅ Filters applied — **{len(st.session_state._focused_pool):,}** case(s) "
            f"in the pool ({applied['focus']})."
        )
    else:
        st.warning(
            "⚠️ Filter selections changed since the last Apply. "
            "Click **Apply Filters** again — Generate Preview stays disabled until you do."
        )

if applied is not None:
    with st.expander("Filter funnel — cases remaining after each step", expanded=False):
        st.dataframe(
            pd.DataFrame(applied["steps"], columns=["Step", "Cases"]),
            width="stretch", hide_index=True,
        )

st.divider()

# ── Country distribution ──────────────────────────────────────────────────────
st.subheader("Country Distribution")
st.caption(
    "Select which countries to include and how many cases per country. "
    f"Total must not exceed **{MAX_CASES}** cases."
)

country_counts      = applied["country_counts"] if applied is not None else {}
available_countries = sorted(country_counts)

if applied is None:
    st.info("Apply filters to see the available cases per country.")
elif not available_countries:
    st.info("No cases remain after the selected filters.")
else:
    with st.expander("📊 Available cases per country in the applied pool", expanded=True):
        st.dataframe(
            pd.DataFrame(
                [{"Country": c, "Available Cases": country_counts[c]} for c in available_countries]
            ).sort_values("Available Cases", ascending=False),
            width="stretch", hide_index=True,
        )

selected_countries = st.multiselect(
    "Select countries for output",
    options=available_countries,
    key="out_countries",
    disabled=not available_countries,
)

country_alloc   = {}
total_requested = 0

st.markdown("**Cases to include per country:**")
gc1, gc2, _ = st.columns([1.2, 1, 3])
with gc1:
    global_val = st.number_input(
        "Set all countries to", min_value=0, max_value=MAX_CASES, value=10, step=1,
        key="global_alloc_val", disabled=not selected_countries,
        help="Applies to all selected countries. Capped at each country's available cases.",
    )
with gc2:
    st.markdown("<br>", unsafe_allow_html=True)
    apply_global = st.button(
        "Apply to All", key="apply_global_alloc", disabled=not selected_countries
    )
if apply_global:
    for _c in selected_countries:
        st.session_state[f"alloc_{_c}"] = min(global_val, country_counts.get(_c, 0))

if selected_countries:
    alloc_cols = st.columns(min(len(selected_countries), 4))
    for i, country in enumerate(selected_countries):
        available = country_counts.get(country, 0)
        with alloc_cols[i % 4]:
            country_alloc[country] = st.number_input(
                country, min_value=0, max_value=available,
                value=min(10, available), key=f"alloc_{country}",
                help=f"{available} available",
            )
    total_requested = sum(country_alloc.values())
    if total_requested > MAX_CASES:
        st.warning(
            f"⚠️ Total requested: **{total_requested}** cases — exceeds the recommended "
            f"limit of **{MAX_CASES}**. Check the override option below to generate anyway."
        )
    elif total_requested == 0:
        st.warning("⚠️ All country allocations are set to 0.")
    else:
        st.success(f"✅ Total requested: **{total_requested}** / {MAX_CASES} cases.")
else:
    st.caption("Select one or more countries above to set their case counts.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — PREVIEW & GENERATE
# ─────────────────────────────────────────────────────────────────────────────
st.header("5. Preview & Generate")

# Keyed by the total so a consent given for one total never carries to another.
override_limit = st.checkbox(
    f"I understand this exceeds {MAX_CASES} cases — generate anyway ({total_requested} cases)",
    value=False,
    key=f"override_limit_{total_requested}",
    disabled=total_requested <= MAX_CASES,
)
can_generate = bool(
    applied is not None
    and filters_in_sync
    and selected_countries
    and total_requested > 0
    and (total_requested <= MAX_CASES or override_limit)
)

gen_preview_clicked = st.button(
    "👁️ Generate Preview", type="secondary", key="gen_preview", disabled=not can_generate
)
if not can_generate:
    if applied is None:
        st.caption("Apply filters and choose countries to enable the preview.")
    elif not filters_in_sync:
        st.caption("Apply the changed filters to enable the preview.")
    elif not selected_countries or total_requested == 0:
        st.caption("Select countries and set their case counts to enable the preview.")
    else:
        st.caption(f"Tick the override to generate more than {MAX_CASES} cases.")

if gen_preview_clicked:
    _pool = st.session_state._focused_pool
    _ids  = select_by_country_distribution(_pool, country_alloc)
    _pre  = build_output(_pool, _ids)
    # Focus is exclusive, so every case in the applied pool is of that type.
    _pre["Type"] = applied["focus"]
    _pre = add_case_type_column(_pre)

    st.session_state._pre_result      = _pre
    st.session_state._output_df       = None
    st.session_state._confirmed_comp  = False
    st.session_state._excl_preview    = set()
    st.session_state._country_alloc   = dict(country_alloc)
    st.session_state._req_count       = total_requested

pre_result = st.session_state._pre_result

if pre_result is not None:
    excl_preview  = st.session_state._excl_preview
    focused_pool  = st.session_state._focused_pool
    pre_alloc     = st.session_state._country_alloc
    applied_focus = (applied or {}).get("focus", "Difference")

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

    preview_cols = ["Case ID", "Organization Name", "Country", "Case Type",
                    "No. ofMachines", "Last Event", "Type"]
    disp_cols = [c for c in preview_cols if c in active_preview.columns]

    sel_event = st.dataframe(
        active_preview[disp_cols],
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="multi-row",
        key=f"preview_sel_{st.session_state._sel_reset}",
    )
    selected_rows = (
        sel_event.selection.rows
        if sel_event and hasattr(sel_event, "selection") and sel_event.selection
        else []
    )

    btn1, btn2, btn3, btn4 = st.columns(4)

    if selected_rows:
        if btn1.button(f"🚫 Exclude Selected ({len(selected_rows)})", key="excl_sel_btn"):
            st.session_state._excl_preview |= set(
                active_preview.iloc[selected_rows]["Case ID"].tolist()
            )
            st.session_state._sel_reset += 1
            st.rerun()

    if excl_preview and focused_pool is not None:
        if btn2.button("🔄 Refill List", key="refill_btn"):
            all_seen = set(active_preview["Case ID"].tolist()) | excl_preview
            _blk = st.session_state._blacklist_df
            bl_ids = (
                set(_blk["case_id"].astype(str))
                if (_blk is not None and not _blk.empty) else set()
            )
            new_rows = []
            for country, alloc_n in pre_alloc.items():
                kept_n = len(active_preview[active_preview["Country"] == country])
                need   = alloc_n - kept_n
                if need <= 0:
                    continue
                candidates = focused_pool[
                    (focused_pool["_country"] == country)
                    & (~focused_pool["_case_id"].isin(all_seen))
                    & (~focused_pool["_case_id"].isin(bl_ids))
                ].sort_values("_last_event", ascending=False).head(need)
                if not candidates.empty:
                    new_rows.append(build_output(focused_pool, set(candidates["_case_id"])))

            if new_rows:
                additions = pd.concat(new_rows, ignore_index=True)
                additions["Type"] = applied_focus
                additions = add_case_type_column(additions)
                # Keep the excluded rows inside pre_result so they stay visible in
                # the "Excluded" list and can still be added to the black list.
                st.session_state._pre_result = pd.concat(
                    [pre_result, additions], ignore_index=True
                )
                st.session_state._sel_reset += 1
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
                ok, updated_bl = add_to_blacklist(rows_for_bl, token, repo, path=BLACKLIST_PATH)
            if ok:
                st.session_state._blacklist_df  = updated_bl
                st.session_state._blacklist_sha = None
                st.session_state._sel_reset += 1
                st.success(f"✅ {len(rows_for_bl)} case(s) added to the black list.")
            else:
                st.error("❌ Failed to save black list.")

    if excl_preview:
        if btn4.button("↩️ Clear Exclusions", key="clear_excl_btn"):
            st.session_state._excl_preview = set()
            st.session_state._sel_reset += 1
            st.rerun()

    if excl_preview:
        with st.expander(f"🚫 Excluded cases — {len(excl_preview)} case(s)", expanded=False):
            excl_view = pre_result[pre_result["Case ID"].isin(excl_preview)][disp_cols]
            st.dataframe(excl_view.reset_index(drop=True), width="stretch", hide_index=True)

    st.divider()

    if st.button("⚙️ Generate Output File", type="primary", key="gen_output"):
        st.session_state._output_df      = active_preview
        st.session_state._confirmed_comp = False
        st.session_state._draft_restored = False
        # Auto-save a draft so the output survives a page refresh
        if _bg_token and _bg_repo:
            with st.spinner("Saving draft…"):
                save_draft(active_preview, _bg_token, _bg_repo, path=DRAFT_PATH)

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
elif pre_result is None:
    st.caption("The downloadable output file appears here after you generate a preview.")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — CONFIRM & SAVE TO HISTORY
# ─────────────────────────────────────────────────────────────────────────────
st.header("6. Confirm & Save to History")
_agent = st.session_state.get("_agent_name", "Unknown")
_email = st.session_state.get("_agent_email", "")
st.caption(
    f"Confirming as **{_agent}** ({_email}). "
    "This will be recorded on the batch and excluded from future outputs within the expiration window."
)

if st.session_state._confirmed_comp and output_df is not None:
    st.success("✅ This output has already been confirmed and saved to history.")
else:
    if st.button(
        "✅ Confirm & Push to GitHub History",
        type="primary",
        key="confirm_comp",
        disabled=output_df is None,
    ):
        token, repo = _require_creds()
        save_df = output_df.copy()
        save_df.insert(0, "Responsible",  st.session_state.get("_agent_name", "Unknown"))
        save_df.insert(1, "Confirmed By", st.session_state.get("_agent_email", ""))
        save_df.insert(2, "Confirmed At", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        with st.spinner("Pushing to GitHub…"):
            ok, fname = push_batch(save_df, token, repo, HISTORY_FOLDER)
        if ok:
            st.session_state._confirmed_comp = True
            st.session_state._comp_batches   = None
            st.session_state._draft_restored = False
            st.success(f"✅ Saved to history: `{fname}`")
            delete_draft(token, repo, path=DRAFT_PATH)     # confirmed -> draft no longer needed
            st.balloons()
        else:
            st.error("❌ Push failed. Check token permissions.")
    if output_df is None:
        st.caption("Generate an output file (Section 5) to enable confirmation.")

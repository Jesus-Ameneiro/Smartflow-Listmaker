"""
Pleteo Sync Analyzer — Main Router
Ruvixx · LATAM Compliance Operations
"""

import streamlit as st

# ── Agent registry ─────────────────────────────────────────────────────────────
AGENTS = {
    "jesus@ruvixx.com":         "Jesus Ameneiro",
    "luisindriago@ruvixx.com":  "Luis Indriago",
}
DEFAULT_AGENT = "Jesus Ameneiro"   # assigned to all pre-existing history

# ── Session state ──────────────────────────────────────────────────────────────
for k, v in {
    "_agent_name":  None,
    "_agent_email": None,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Login gate ─────────────────────────────────────────────────────────────────
if not st.session_state._agent_name:
    st.set_page_config(
        page_title="Pleteo Sync Analyzer — Sign In",
        page_icon="🔐",
        layout="centered",
    )

    col_logo, _ = st.columns([1, 3])
    st.title("Pleteo Sync Analyzer")
    st.caption("Ruvixx · LATAM Compliance Operations")
    st.divider()

    st.subheader("Sign In")
    st.caption("Select your account to continue. Your identity will be recorded on all confirmed actions.")

    selected_email = st.selectbox(
        "Email address",
        options=list(AGENTS.keys()),
        format_func=lambda e: f"{AGENTS[e]}  ({e})",
        key="login_email",
    )

    if st.button("▶ Continue", type="primary", use_container_width=True):
        st.session_state._agent_name  = AGENTS[selected_email]
        st.session_state._agent_email = selected_email
        st.rerun()

    st.stop()

# ── Navigation (only reached when authenticated) ───────────────────────────────
pg = st.navigation([
    st.Page(
        "pages/1_🔁_Prioritizer.py",
        title="Case Update Prioritizer",
        icon="🔁",
    ),
    st.Page(
        "pages/2_🔄_Comparator.py",
        title="Smartflow Comparator",
        icon="🔄",
    ),
    st.Page(
        "pages/3_📋_History.py",
        title="History & Blacklist",
        icon="📋",
    ),
])

pg.run()

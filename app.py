"""
Pleteo Sync Analyzer — Main Router
Ruvixx · LATAM Compliance Operations
"""

import hashlib

import streamlit as st

from github_manager import load_pins, save_pin

# ── Agent registry ─────────────────────────────────────────────────────────────
AGENTS = {
    "jesus@ruvixx.com":        "Jesus Ameneiro",
    "luisindriago@ruvixx.com": "Luis Indriago",
}
DEFAULT_AGENT    = "Jesus Ameneiro"
MAX_ATTEMPTS     = 3
LOCKOUT_SECONDS  = 30


def _hash(pin: str) -> str:
    return hashlib.sha256(pin.strip().encode()).hexdigest()


def _gh():
    try:
        return st.secrets["GITHUB_TOKEN"], st.secrets["GITHUB_REPO"]
    except KeyError:
        return None, None


# ── Session state ──────────────────────────────────────────────────────────────
for k, v in {
    "_agent_name":    None,
    "_agent_email":   None,
    "_login_step":    "select",   # select | set_pin | verify_pin
    "_login_email":   None,
    "_login_attempts":0,
    "_lockout_until": 0,
    "_pins_cache":    None,
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
    st.title("🔐 Pleteo Sync Analyzer")
    st.caption("Ruvixx · LATAM Compliance Operations")
    st.divider()

    token, repo = _gh()
    if not token or not repo:
        st.error("GitHub credentials not configured. Add GITHUB_TOKEN and GITHUB_REPO to Streamlit secrets.")
        st.stop()

    # Load PIN store once per session
    if st.session_state._pins_cache is None:
        with st.spinner("Loading…"):
            st.session_state._pins_cache = load_pins(token, repo)

    pins = st.session_state._pins_cache

    # ── Step 1: Select agent ──────────────────────────────────────────────────
    if st.session_state._login_step == "select":
        st.subheader("Sign In")
        selected = st.selectbox(
            "Select your account",
            options=list(AGENTS.keys()),
            format_func=lambda e: f"{AGENTS[e]}  —  {e}",
            key="login_email_sel",
        )
        if st.button("Continue ▶", type="primary", use_container_width=True):
            st.session_state._login_email    = selected
            st.session_state._login_attempts = 0
            if selected in pins:
                st.session_state._login_step = "verify_pin"
            else:
                st.session_state._login_step = "set_pin"
            st.rerun()
        st.stop()

    email = st.session_state._login_email
    name  = AGENTS.get(email, email)

    # ── Step 2a: First login — create PIN ────────────────────────────────────
    if st.session_state._login_step == "set_pin":
        st.subheader(f"Welcome, {name}")
        st.info(
            "This is your first login. Create a personal PIN to secure your account. "
            "Your PIN will be stored as a hash — it cannot be retrieved, only reset."
        )
        pin1 = st.text_input("Create PIN (4–8 digits)", type="password", max_chars=8, key="pin_create")
        pin2 = st.text_input("Confirm PIN",             type="password", max_chars=8, key="pin_confirm")

        c1, c2 = st.columns(2)
        if c1.button("Set PIN & Sign In", type="primary", use_container_width=True):
            if not pin1:
                st.warning("Please enter a PIN.")
            elif not pin1.isdigit() or len(pin1) < 4:
                st.warning("PIN must be 4–8 digits.")
            elif pin1 != pin2:
                st.error("PINs do not match.")
            else:
                with st.spinner("Saving PIN…"):
                    ok = save_pin(email, _hash(pin1), token, repo)
                if ok:
                    st.session_state._pins_cache[email] = _hash(pin1)
                    st.session_state._agent_name  = name
                    st.session_state._agent_email = email
                    st.session_state._login_step  = "select"
                    st.rerun()
                else:
                    st.error("Failed to save PIN. Check GitHub credentials.")

        if c2.button("← Back", use_container_width=True):
            st.session_state._login_step = "select"
            st.rerun()
        st.stop()

    # ── Step 2b: Verify PIN ───────────────────────────────────────────────────
    if st.session_state._login_step == "verify_pin":
        import time
        now = time.time()

        # Lockout check
        if st.session_state._lockout_until > now:
            remaining = int(st.session_state._lockout_until - now)
            st.error(f"⛔ Too many failed attempts. Try again in {remaining}s.")
            st.stop()

        attempts_left = MAX_ATTEMPTS - st.session_state._login_attempts
        st.subheader(f"Welcome back, {name}")
        if st.session_state._login_attempts > 0:
            st.warning(f"Incorrect PIN. {attempts_left} attempt(s) remaining.")

        pin = st.text_input("Enter your PIN", type="password", max_chars=8, key="pin_verify")

        c1, c2 = st.columns(2)
        if c1.button("Sign In ▶", type="primary", use_container_width=True):
            if not pin:
                st.warning("Please enter your PIN.")
            elif _hash(pin) == pins.get(email):
                # Success
                st.session_state._agent_name    = name
                st.session_state._agent_email   = email
                st.session_state._login_step    = "select"
                st.session_state._login_attempts= 0
                st.rerun()
            else:
                st.session_state._login_attempts += 1
                if st.session_state._login_attempts >= MAX_ATTEMPTS:
                    st.session_state._lockout_until = time.time() + LOCKOUT_SECONDS
                    st.session_state._login_attempts = 0
                st.rerun()

        if c2.button("← Back", use_container_width=True):
            st.session_state._login_step     = "select"
            st.session_state._login_attempts = 0
            st.rerun()
        st.stop()

# ── Navigation (only reached when authenticated) ───────────────────────────────
pg = st.navigation([
    st.Page("pages/1_🔁_Prioritizer.py", title="Case Update Prioritizer", icon="🔁"),
    st.Page("pages/2_🔄_Comparator.py",  title="Smartflow Comparator",    icon="🔄"),
    st.Page("pages/3_📋_History.py",      title="History & Blacklist",     icon="📋"),
])

pg.run()

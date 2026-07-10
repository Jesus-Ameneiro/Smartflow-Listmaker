"""
Pleteo Sync Analyzer — Main Router
Ruvixx · LATAM Compliance Operations
"""

import streamlit as st

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

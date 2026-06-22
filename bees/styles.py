"""Global Streamlit CSS for Project Oracle."""
import streamlit as st

GLOBAL_CSS = """
<style>
html, body, [class*="css"] {
    font-size: 14px !important;
}
div[data-testid="stMetricValue"] {
    font-size: 1.2rem !important;
}
</style>
"""


def inject_global_css():
    """Shrink metric/global font size to prevent value cutoff."""
    st.markdown(GLOBAL_CSS, unsafe_allow_html=True)

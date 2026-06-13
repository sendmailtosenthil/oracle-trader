import streamlit as st
from database import get_db, User, hash_password

def require_auth():
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        st.title("Project Oracle")
        st.subheader("Login")

        username = st.text_input("Username")
        password = st.text_input("Password", type="password")

        if st.button("Login"):
            db = next(get_db())
            user = db.query(User).filter(User.username == username).first()
            if user and user.password_hash == hash_password(password):
                st.session_state["authenticated"] = True
                st.session_state["username"] = username
                st.rerun()
            else:
                st.error("Invalid username or password")
        
        st.stop() # Stops execution of the rest of the app if not authenticated

def logout():
    st.session_state["authenticated"] = False
    if "username" in st.session_state:
        del st.session_state["username"]
    st.rerun()

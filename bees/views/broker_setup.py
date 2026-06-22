"""Broker Setup page: configure Zerodha / Kite credentials."""
import streamlit as st

from bees.database import BrokerConfig
from bees.services.broker import is_zerodha_token_valid


def render(db):
    st.title("Broker Setup & Integrations")
    st.write("Configure your API keys and tokens for broker integration.")

    st.subheader("Zerodha / Kite")

    # Get existing config or create an empty one
    broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()

    current_user_id = broker_config.user_id if broker_config else 'PC8006'
    current_enctoken = broker_config.enctoken if broker_config else ''

    with st.form(key="zerodha_config_form"):
        z_user_id = st.text_input("Zerodha User ID", value=current_user_id)
        z_enctoken = st.text_input("Kite enctoken", value=current_enctoken, type="password")

        if st.form_submit_button("Save Broker Configuration", type="primary"):
            if z_enctoken.strip() == "":
                st.error("enctoken cannot be empty.")
            else:
                if broker_config:
                    broker_config.user_id = z_user_id
                    broker_config.enctoken = z_enctoken
                else:
                    new_config = BrokerConfig(broker_name='ZERODHA', user_id=z_user_id, enctoken=z_enctoken)
                    db.add(new_config)

                db.commit()
                is_zerodha_token_valid.clear()
                st.success("Zerodha credentials saved successfully!")
                st.rerun()

FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    cron \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Install the cron schedule (must be root-owned, mode 0644)
COPY oracle.cron /etc/cron.d/oracle
RUN chmod 0644 /etc/cron.d/oracle

# Initialize the database on build
RUN python -m common.database

EXPOSE 8501

# Entrypoint script to run both Streamlit and the Bot Daemon
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["bash", "entrypoint.sh"]

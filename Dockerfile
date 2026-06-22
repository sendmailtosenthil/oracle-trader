FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Initialize the database on build
RUN python -m bees.database

EXPOSE 8501

# Entrypoint script to run both Streamlit and the Bot Daemon
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

CMD ["bash", "entrypoint.sh"]

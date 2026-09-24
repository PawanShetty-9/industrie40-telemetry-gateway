# One image for the edge gateway, the sensor simulator and the verification client.
# PYTHON_IMAGE can point to a registry mirror if Docker Hub is rate-limited, e.g.
#   docker compose build --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY sensor_simulator.py opc_ua_server.py edge_gateway.py opc_client.py ./

# Unprivileged service user. The code stays root-owned (read-only for the
# service); only pki/ (certificates) is writable.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin edgegw \
    && mkdir -p /app/pki \
    && chown edgegw:edgegw /app/pki \
    && chmod 700 /app/pki
USER edgegw

EXPOSE 4840
CMD ["python", "edge_gateway.py", "--mqtt-host", "mosquitto", "--opcua-endpoint", "opc.tcp://0.0.0.0:4840"]

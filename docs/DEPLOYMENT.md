# Test and Deployment Guide

This guide takes the project from a first local test run to a hardened deployment.

| Stage | Use it for |
|---|---|
| [1. Local test run](#1-local-test-run) | Development: three terminals, instant feedback |
| [2. Acceptance test](#2-acceptance-test-checklist) | A checklist that proves every feature, like a factory acceptance test (Abnahmetest) |
| [3A. Docker Compose](#3a-deployment-with-docker-compose) | Demo or lab: the whole stack with one command, on any OS with Docker |
| [3B. systemd service](#3b-deployment-as-systemd-services-edge-device) | Edge device (Debian, Ubuntu, Kali, Raspberry Pi OS): starts at boot, restarts on failure, hardened |
| [4. Production checklist](#4-production-checklist) | What a real plant deployment additionally needs |

All commands are for Debian-based Linux and were run while writing this guide.

---

## 1. Local test run

```bash
# One-time setup
sudo apt install -y mosquitto mosquitto-clients python3-venv git
sudo systemctl enable --now mosquitto
git clone https://github.com/PawanShetty-9/industrie40-telemetry-gateway.git
cd industrie40-telemetry-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Automated tests (20 tests, about 25 s)
python -m unittest -v
```

Then use one terminal per process, each with `source .venv/bin/activate`:

| Terminal | Command | You should see |
|---|---|---|
| 1 | `python edge_gateway.py` | `OPC UA server listening on opc.tcp://localhost:4840` and `Connected to MQTT broker` |
| 2 | `python sensor_simulator.py` | One `seq=… T=… n=… v=…` line per second |
| 3 | `python opc_client.py` | The model overview, then live values with status `Good` |

Quick automated check, which exits with code 0 when everything works:

```bash
python opc_client.py --check
```

## 2. Acceptance test checklist

Run these with the three terminals from step 1 open. Keep `python opc_client.py`
running in terminal 3 to watch the effect of each test.

| # | Test | Action | Expected result |
|---|---|---|---|
| 1 | Unit tests | `python -m unittest -v` | `Ran 20 tests … OK` |
| 2 | MQTT data | `mosquitto_sub -h localhost -t 'factory/#' -v` | `status ONLINE` plus one telemetry JSON per second |
| 3 | Information model | `python opc_client.py --duration 5` | Temperature °C 0…120, RPM r/min 0…24000, Vibration mm/s 0…50, type `CNCMachineType` |
| 4 | Smoke test | `python opc_client.py --check; echo $?` | `CHECK PASSED`, exit code `0` |
| 5 | Encryption | `python opc_client.py --check --security Aes256Sha256RsaPss` | `CHECK PASSED` over Sign & Encrypt |
| 6 | Write protection | `uawrite -u opc.tcp://localhost:4840 -n "ns=2;s=Factory.ProductionLine1.CNC_Machine_1.Temperature" -t double 99` | `BadUserAccessDenied` |
| 7 | Publisher stops | Ctrl+C in terminal 2 | All values `UncertainLastUsableValue` at once; last values kept |
| 8 | Publisher returns | Start `python sensor_simulator.py` again | `Sequence restarted`; values `Good` |
| 9 | Silent failure | `kill -STOP $(pgrep -f sensor_simulator.py)`, wait 10 s, then `kill -CONT $(pgrep -f sensor_simulator.py)` | Values `Uncertain` after about 5 s (watchdog), `Good` again after resuming |
| 10 | Broker restart | `sudo systemctl restart mosquitto` | Values `Uncertain`, then automatic reconnect, `Good`, and a logged `Sequence gap` |
| 11 | Malformed input | `mosquitto_pub -t factory/shopfloor/cnc1/telemetry -m 'not json'` | Gateway logs `Rejected message … invalid JSON`; values unaffected |
| 12 | Out of range | Stop the simulator, then `mosquitto_pub -t factory/shopfloor/cnc1/telemetry -m '{"machine_id":"cnc1","temperature_c":150,"spindle_rpm":0,"vibration_mm_s":0}'` | Temperature `UncertainEngineeringUnitsExceeded`; RPM and Vibration `Good` |
| 13 | Clean shutdown | Ctrl+C in terminal 1 | `Gateway stopped`, exit code `0` |

---

## 3A. Deployment with Docker Compose

The stack runs as three containers from one image:
- `mosquitto`: the broker, reachable only inside the Compose network;
- `edge-gateway`: the OPC UA server, published on `127.0.0.1:4840`;
- `sensor-simulator`.

### Install Docker (Kali / Debian)

```bash
sudo apt install -y docker.io docker-compose
sudo systemctl enable --now docker
sudo usermod -aG docker $USER      # then log out and back in
docker compose version             # older packages: use "docker-compose" instead
```

### Start and verify

Stop the local test run first (Ctrl+C in terminals 1 and 2), because port 4840 must be free.

```bash
cd industrie40-telemetry-gateway
docker compose up -d --build

docker compose ps                  # edge-gateway: "Up … (healthy)"
docker compose logs -f edge-gateway
docker compose exec mosquitto mosquitto_sub -t 'factory/#' -v -C 3

# Smoke test from the host (venv active) or from inside the container
python opc_client.py --check
docker compose exec edge-gateway python opc_client.py --check --security Basic256Sha256
```

UaExpert connects to `opc.tcp://localhost:4840` exactly as in the local run.

### Operate

| Task | Command |
|---|---|
| Stop, keeping the certificate | `docker compose down` |
| Stop and delete the certificate volume | `docker compose down -v` |
| Restart one service | `docker compose restart sensor-simulator` |
| Rebuild after `git pull` | `docker compose up -d --build` |
| Encrypted endpoints only | Append `"--secure-only"` to the `edge-gateway` `command` in `docker-compose.yml` |
| Reach OPC UA from the LAN | Change the port mapping to `"4840:4840"`, and use `--secure-only` |
| Docker Hub rate limit (HTTP 429) | `PYTHON_IMAGE=mirror.gcr.io/library/python:3.11-slim MOSQUITTO_IMAGE=mirror.gcr.io/library/eclipse-mosquitto:2 docker compose up -d --build` |

### What the container setup does for you

- **Non-root user:** runs as uid 10001. The code is root-owned, so the service cannot
  modify it.
- **Locked-down container:** read-only root filesystem, **all Linux capabilities
  dropped**, `no-new-privileges`.
- **Certificate volume:** the server certificate lives in the named volume `pki` with
  mode `0700`, so it survives container re-creation.
- **Health check** on the OPC UA port. `restart: unless-stopped` restarts crashed
  containers.
- **Graceful stop:** `docker stop` sends SIGTERM, and the gateway shuts down cleanly with
  exit code 0.

---

## 3B. Deployment as systemd services (edge device)

This installs the gateway like a real service on an industrial PC:
- code in `/opt`, owned by root and read-only for the service;
- a dedicated system user without login;
- the certificate in `/var/lib`;
- automatic start at boot and restart on failure;
- **encrypted OPC UA endpoints only** (`--secure-only`).

Stop the local test run and any Compose stack first, because port 4840 must be free.

```bash
# 1. Packages
sudo apt update
sudo apt install -y mosquitto mosquitto-clients python3 python3-venv git

# 2. Service user: no login shell, no home directory
sudo useradd --system --no-create-home --shell /usr/sbin/nologin edgegw

# 3. Code: owned by root, read-only for the service
sudo git clone https://github.com/PawanShetty-9/industrie40-telemetry-gateway.git /opt/industrie40-telemetry-gateway

# 4. Python environment
sudo python3 -m venv /opt/industrie40-telemetry-gateway/.venv
sudo /opt/industrie40-telemetry-gateway/.venv/bin/pip install -r /opt/industrie40-telemetry-gateway/requirements.txt

# 5. Services
sudo cp /opt/industrie40-telemetry-gateway/deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mosquitto edge-gateway sensor-simulator
```

### Verify

```bash
systemctl status edge-gateway sensor-simulator --no-pager
journalctl -u edge-gateway -f          # Ctrl+C to leave

# Smoke test as your normal user. The server only offers encrypted endpoints, so
# pass --security. The client certificate is stored in your home directory.
/opt/industrie40-telemetry-gateway/.venv/bin/python /opt/industrie40-telemetry-gateway/opc_client.py \
    --check --security Basic256Sha256 --pki-dir ~/.opcua-client

# How well is the service isolated? (lower score = better)
systemd-analyze security edge-gateway
```

In UaExpert, choose the endpoint **Basic256Sha256 – Sign & Encrypt** and trust the
server certificate when prompted.

### Operate

| Task | Command |
|---|---|
| Logs | `journalctl -u edge-gateway -u sensor-simulator -f` |
| Restart | `sudo systemctl restart edge-gateway` |
| Stop, and don't start at boot | `sudo systemctl disable --now edge-gateway sensor-simulator` |
| Update | `sudo git -C /opt/industrie40-telemetry-gateway pull && sudo /opt/industrie40-telemetry-gateway/.venv/bin/pip install -r /opt/industrie40-telemetry-gateway/requirements.txt && sudo cp /opt/industrie40-telemetry-gateway/deploy/systemd/*.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart edge-gateway sensor-simulator` |
| Change options | `sudo systemctl edit edge-gateway`, then add an `ExecStart=` line followed by the full new `ExecStart=…` command |
| Reach OPC UA from the LAN | Use `--opcua-endpoint opc.tcp://0.0.0.0:4840` (via `systemctl edit`), and allow only your OT network: `sudo ufw allow from 192.168.10.0/24 to any port 4840 proto tcp` |
| Real sensors instead of the simulator | `sudo systemctl disable --now sensor-simulator` and publish to the same topic |

### Uninstall

```bash
sudo systemctl disable --now edge-gateway sensor-simulator
sudo rm /etc/systemd/system/edge-gateway.service /etc/systemd/system/sensor-simulator.service
sudo systemctl daemon-reload
sudo rm -rf /opt/industrie40-telemetry-gateway /var/lib/industrie40-edge-gateway
sudo userdel edgegw
```

### What the unit files do for you

- **Unprivileged user** `edgegw` with an empty capability set and `NoNewPrivileges`.
- **Read-only system:** `ProtectSystem=strict` makes the whole filesystem read-only. The
  only writable path is the gateway's `StateDirectory` (`/var/lib/industrie40-edge-gateway`,
  mode 0700). The simulator cannot write anywhere.
- **Kernel and system-call isolation:** kernel tunables, modules, logs, clock, cgroups and
  devices are hidden or protected. System calls are limited to `@system-service` minus
  `@privileged` and `@resources`, and only IP and Unix sockets are allowed.
- **Restart on failure:** the gateway exits with a non-zero code on fatal internal errors,
  and systemd restarts it after 5 s.

---

## 4. Production checklist

This project is a simulation. A real plant deployment would add the items below.

| Area | Measure | Status |
|---|---|---|
| OPC UA transport | `--secure-only` (encrypted endpoints only) | ✅ default in the systemd unit |
| OPC UA clients | Trust list: accept only known client certificates (asyncua `CertificateValidator` + `TrustStore`) | ⬜ not implemented; asyncua currently accepts any client certificate |
| OPC UA certificates | CA-signed certificates (company PKI or a Global Discovery Server) instead of self-signed ones | ⬜ |
| MQTT transport | TLS on port 8883, `allow_anonymous false`, `password_file`, topic `acl_file` in Mosquitto | ⬜ needs TLS and credential options in the scripts |
| Network | Separate IT and OT zones and conduits (IEC 62443); firewall port 4840 to the SCADA/MES hosts only | ⬜ site-specific |
| Time | NTP or chrony on every device: correct `SourceTimestamp`s and the 60 s clock-skew check depend on it | ⬜ site-specific |
| Monitoring | Run `opc_client.py --check` periodically (systemd timer or monitoring agent) and alert on exit code ≠ 0 | ⬜ |
| Logs | journald retention or forwarding to a central log system | ⬜ site-specific |

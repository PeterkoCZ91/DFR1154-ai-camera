# A12 System v2 Docker

Run from the `a12_system` directory:

```bash
cd a12_system
docker compose -p a12_system_v2 build
docker compose -p a12_system_v2 up -d
```

The compose stack uses `network_mode: host` so A12 can reach the ESP32 camera, MQTT broker, and Home Assistant on the LAN without Docker port mapping.

## Runtime Data

The compose file reads `A12_DATA_DIR` from `a12_system/.env` and mounts that host directory into the container as `/data`. Create the data directory and set the variable before first run:

```bash
mkdir -p /opt/a12-data
echo "A12_DATA_DIR=/opt/a12-data" > a12_system/.env
```

Copy the example config and edit it:

```bash
cp a12_system/config.env.example /opt/a12-data/config.env
```

The runtime directory stores secrets, logs, databases, screenshots, and model files outside the git repo:

- `config.env`
- `config.json`
- `known_faces.pkl` when face recognition is enabled
- `events.db`, `events.db-wal`, `events.db-shm`
- `a12.log`, `events.log`
- `stats.json`
- `screenshots/`
- `yolo11n.onnx`

The model is mounted into the container as `/data/yolo11n.onnx`; runtime config should use `YOLO_WEIGHTS=/data/yolo11n.onnx`.

Do not commit runtime secrets, logs, databases, screenshots, model weights, or face encodings.

## Commands

```bash
cd a12_system
docker compose -p a12_system_v2 ps
docker compose -p a12_system_v2 logs -f a12
docker compose -p a12_system_v2 restart a12
docker compose -p a12_system_v2 down
```

Container log shortcut:

```bash
docker logs -f a12-system-v2
```

Application log (adjust path to your `A12_DATA_DIR`):

```bash
tail -f /opt/a12-data/a12.log
```

Recent database events:

```bash
sqlite3 -header -column /opt/a12-data/events.db \
  "SELECT id, datetime, type, label, value, media_path FROM events ORDER BY id DESC LIMIT 30;"
```

## Resource Limits

The service is capped in `docker-compose.yml`:

- CPU: `1.0`
- RAM: `1g`
- swap: `1g`
- process limit: `64`
- native math/OpenCV threads: `1`

Typical idle usage is around 170–200 MB; peak during YOLO + MP4 encoding is around 250–300 MB. The 1 GB limit gives comfortable headroom. If detection becomes too slow, raise `cpus`, `mem_limit`, and `A12_CV_THREADS` together and recreate the container.

## Switching From Manual Run

If A12 is already running manually, stop it before starting Docker:

```bash
pgrep -af 'A12_System_v2|python3 -m A12|python -m A12|a12.py'
kill -TERM <pid>
docker compose -p a12_system_v2 up -d
```

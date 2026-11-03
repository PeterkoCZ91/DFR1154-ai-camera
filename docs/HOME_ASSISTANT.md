# Home Assistant: connecting the camera to automations

This guide takes you from a freshly flashed camera to working Home Assistant
automations. It assumes Home Assistant with the
[MQTT integration](https://www.home-assistant.io/integrations/mqtt/) and a broker
(the Mosquitto add-on is fine). Nothing here needs A12. A12 is an optional second
step that adds better person detection, face recognition and door unlocking.

Two different programs talk to Home Assistant, and each creates its own device:

| | Firmware (the camera itself) | A12 (the optional server companion) |
|---|---|---|
| Device in HA | named after the camera's `device_name`, default `ESP32-Camera` | named after `CAMERA_NAME`, default `ESP32 AI Camera` |
| MQTT topics | `esp32cam/<device_name>/…` | `esp32_camera/…` (`MQTT_BASE_TOPIC`) |
| Person detection | on-device FOMO model, fast, more false alarms | YOLO on the server, more accurate |
| Use it for | snapshots, motion, switching recording on/off | person alerts, face results, the door |

If you run both, build person automations on the **A12** entity and keep the
firmware for motion and commands.

## Part 1: the camera alone

### 1. Point the camera at the broker

Open `http://<camera-ip>/settings`, section **MQTT (Home Assistant)**, turn
**MQTT enabled** on, and fill in the broker address and port. Or use the API:

```bash
curl -u admin:<http-password> -X POST http://<camera-ip>/settings \
  -H 'Content-Type: application/json' \
  -d '{"mqtt_enabled": true, "mqtt_server": "192.0.2.10", "mqtt_port": 1883}'
```

The broker user and password are not on the settings page. They are stored in NVS
through `/credentials`:

```bash
curl -u admin:<http-password> -X POST http://<camera-ip>/credentials \
  -H 'Content-Type: application/json' \
  -d '{"mqtt_user": "camera", "mqtt_pass": "<broker-password>"}'
```

### 2. Reboot once

The MQTT client is created at boot. If MQTT was off when the camera started,
turning it on does nothing until a reboot
([why](DFROBOT_HARDWARE_GUIDE.md#enabling-mqtt-over-settings-does-nothing-until-a-reboot)):

```bash
curl -u admin:<http-password> -X POST http://<camera-ip>/reboot
```

### 3. Check that it connected

`GET http://<camera-ip>/status` should show `"mqtt_connected": true`. If it shows
`false`, `mqtt_state` holds the PubSubClient error code (`-2` cannot reach the
broker, `4` or `5` wrong user or password). The MQTT entry under Troubleshooting in the
[README](../README.md) lists the usual causes.

To see the traffic yourself:

```bash
mosquitto_sub -h 192.0.2.10 -u <user> -P <password> -t 'esp32cam/#' -v
```

### 4. Find the device in Home Assistant

The camera announces itself through MQTT discovery as soon as it connects. In
**Settings → Devices & services → MQTT → Devices** a device named after
`device_name` appears with these entities. The entity IDs below are for the default
name `ESP32-Camera`. HA builds them from the device name and the entity name:

| Entity | Example ID | Meaning |
|---|---|---|
| Motion | `binary_sensor.esp32_camera_motion` | `on` while the camera sees motion |
| Person | `binary_sensor.esp32_camera_person` | `on` for a confident on-device person detection |
| Motion Score, Motion Percent | `sensor.esp32_camera_motion_score`, `…_motion_percent` | how strong the motion was |
| Brightness | `sensor.esp32_camera_brightness` | mean frame brightness |
| WiFi Signal, Free Heap, Uptime | `sensor.esp32_camera_wifi_signal`, `…_free_heap`, `…_uptime` | health, sent every 60 s |
| Recording | `switch.esp32_camera_recording` | SD recording on/off |
| Person Detection | `switch.esp32_camera_person_detection` | on-device person model on/off |
| Snapshot | `button.esp32_camera_snapshot` | take a snapshot |

**Always copy the entity ID from the device page**, not from this table. Three
things change it:

- **A second board.** Two boards with the same `device_name` share MQTT topics and
  fight over them. Give each its own name first, see
  [Running two boards on one network](DFROBOT_HARDWARE_GUIDE.md#5-running-two-boards-on-one-network).
  `device_name` lives in the camera's `/config.json` on LittleFS. It is not on the
  settings page.
- **Renaming later.** HA keeps the entity IDs it created first, so a renamed or
  swapped board can end up with `_2` IDs. Rename them on the device page, or delete
  the old device in HA before connecting the new one.
- **The state after an HA restart.** Motion and Person are not retained. They read
  `unknown` until the next event, so trigger on `to: "on"`, not on a change from
  `off`.

The firmware entities never turn `unavailable`, because the camera publishes no
availability topic. To find out that the camera is gone, watch `Uptime` stop
changing (example 4), or use A12's **Camera Connection**.

### 5. First automations

Paste these into **Settings → Automations → ⋮ → Edit in YAML**, then replace the
entity IDs with your own.

**Example 1: phone notification with a snapshot when a person is detected.** The
picture comes from the camera's `/frame` endpoint (the latest JPEG), which needs no login:

```yaml
alias: Camera - person at the door
triggers:
  - trigger: state
    entity_id: binary_sensor.esp32_camera_person
    to: "on"
actions:
  - action: notify.mobile_app_<your_phone>
    data:
      message: "Person at the door"
      data:
        image: "http://<camera-ip>/frame"
mode: single
max_exceeded: silent
```

`image` works when the phone can reach the camera, i.e. on the home network or
VPN. To reach the camera from outside, save the picture through HA first
(`downloader` or a `camera` entity) and send that file instead.

**Example 2: light on at night when there is motion:**

```yaml
alias: Camera - hallway light on motion after dark
triggers:
  - trigger: state
    entity_id: binary_sensor.esp32_camera_motion
    to: "on"
conditions:
  - condition: sun
    after: sunset
    before: sunrise
actions:
  - action: light.turn_on
    target:
      entity_id: light.hallway
  - delay: "00:03:00"
  - action: light.turn_off
    target:
      entity_id: light.hallway
mode: restart
```

**Example 3: no person alerts while someone is home.** Turns the on-device person
model off when the first person arrives and back on when the house is empty:

```yaml
alias: Camera - person detection only when away
triggers:
  - trigger: state
    entity_id: zone.home
actions:
  - action: >-
      {{ 'switch.turn_off' if states('zone.home') | int(0) > 0 else 'switch.turn_on' }}
    target:
      entity_id: switch.esp32_camera_person_detection
```

**Example 4: tell me when the camera stops reporting.** The health sensors update
every 60 s, so five minutes without a new `Uptime` value means the camera, its
WiFi or the broker is down:

```yaml
alias: Camera - stopped reporting
triggers:
  - trigger: time_pattern
    minutes: "/5"
conditions:
  - condition: template
    value_template: >-
      {{ (now() - states.sensor.esp32_camera_uptime.last_reported).total_seconds() > 300 }}
actions:
  - action: notify.mobile_app_<your_phone>
    data:
      message: "The camera has not reported for 5 minutes"
mode: single
```

**Commands without an entity.** Every command is a plain MQTT message, so a script,
Node-RED or another automation can send them directly:

```yaml
action: mqtt.publish
data:
  topic: esp32cam/ESP32-Camera/cmd/recording   # or cmd/person_detection
  payload: "ON"                               # or OFF
```

`cmd/snapshot` takes any payload. The full topic list is under
[MQTT Topics](../README.md#mqtt-topics) in the README.

## Part 2: adding A12

A12 talks to Home Assistant in two directions: it **publishes** its results over
MQTT, and it **reads** HA sensors over the REST API. Setup and Docker are in
[`A12_COMPANION.md`](A12_COMPANION.md). Below are only the HA parts.

### A12 → Home Assistant (MQTT)

In A12's `config.env`:

```bash
MQTT_BROKER=192.0.2.10
MQTT_PORT=1883
MQTT_USERNAME=a12
MQTT_PASSWORD=<broker-password>
CAMERA_NAME="Front Door"     # becomes the HA device name and the entity ID prefix
```

Changes to `config.env` take effect only when the container is recreated
(`docker compose up -d --force-recreate`). A plain restart keeps the old values.

A12 then creates its own device. With `CAMERA_NAME="Front Door"`:

| Entity | Example ID | Meaning |
|---|---|---|
| Person Detected | `binary_sensor.front_door_person_detected` | `on` when YOLO confirmed a person |
| Camera Motion | `binary_sensor.front_door_camera_motion` | motion as A12 sees it |
| Camera Connection | `binary_sensor.front_door_camera_connection` | `off` when the camera stream is lost: use this to alert on a dead camera |
| Camera Profile | `sensor.front_door_camera_profile` | `DAY` / `DUSK` / `NIGHT` |
| Last Event Score, Pipeline State, Stream FPS | `sensor.front_door_…` | diagnostics |
| Animal Detected | `sensor.front_door_animal_detected` | last animal class (`dog`, `cat`, …) |
| IR LED Control, IR Auto Mode | `switch.front_door_ir_led_control`, `…_ir_auto_mode` | the IR LED |

Swap `binary_sensor.esp32_camera_person` in example 1 for
`binary_sensor.front_door_person_detected` and you get fewer false alarms.

**Face results** have no entity. A12 sends one message per visit to
`esp32_camera/face`: the resident's name, or `stranger`, `undecided`, `no_face`,
`unavailable` or `error`. It needs `FACE_RECOGNITION_ENABLED=true` and an enrolled
gallery ([A12 companion](A12_COMPANION.md#detection-face-recognition-and-shared-scorer)).
Use it straight from an MQTT trigger:

```yaml
alias: Camera - stranger at the door
triggers:
  - trigger: mqtt
    topic: esp32_camera/face
    payload: stranger
actions:
  - action: notify.mobile_app_<your_phone>
    data:
      message: "Someone I don't know is at the door"
```

To get no Telegram message at all for residents, list their names under
`face_recognition.whitelisted_names` in A12's `config.json`.

### Home Assistant → A12 (sensors that wake YOLO)

A PIR, a door contact or any other `binary_sensor` in HA can tell A12 to look. When
one of them turns `on`, A12 runs YOLO for `EXTERNAL_TRIGGER_YOLO_WINDOW_SECONDS`,
and keeps going while the sensor stays on (up to
`EXTERNAL_TRIGGER_YOLO_HOLD_SECONDS`).

1. In HA, create a long-lived access token: click your user name (bottom left) →
   **Security** → **Long-lived access tokens**.
2. In A12's `config.env`:

   ```bash
   HOME_ASSISTANT_URL=http://homeassistant.local:8123
   HOME_ASSISTANT_TOKEN=<the token>
   ```

3. In A12's `config.json` (in the same data directory as `config.env`), list the
   entities:

   ```json
   {
     "ha_monitored_sensors": [
       "binary_sensor.hallway_pir",
       "binary_sensor.front_door_contact"
     ]
   }
   ```

4. Recreate the container. The log shows `HA Monitor started` and one
   `Sensor: <name>: <state>` line per entity. Every trigger after that logs
   `HA Sensor: <name> -> YOLO window opened for …s`.

A12 polls `/api/states` every second (`ha_poll_interval`), so expect up to a second
of delay. Only the state `on` triggers. A sensor that reports `open` or `detected`
instead needs a template `binary_sensor` in HA first. If HA stops answering, A12
forgets a sensor's `on` state after 30 s, so a lost connection cannot keep YOLO
running.

The same token also lets A12 open a Nuki lock through HA when it recognises a
resident. That feature starts as a dry run and has its own safety conditions, see
[Detection, Face Recognition and Shared Scorer](A12_COMPANION.md#detection-face-recognition-and-shared-scorer) in `A12_COMPANION.md`.

## Checklist when something does not work

| Symptom | Check |
|---|---|
| No device appears in HA | `/status` → `mqtt_connected`. Reboot after enabling MQTT. The discovery prefix in HA must be `homeassistant` |
| Device is there, entities `unknown` | Normal after an HA restart until the next event. `mosquitto_sub -t 'esp32cam/#' -v` shows whether anything arrives |
| Entity IDs end in `_2` | An older device with the same name is still registered. Delete it, or rename the entities |
| Two cameras overwrite each other | Same `device_name`. Give each board its own |
| A12 entities missing | `MQTT_BROKER` set? Container recreated, not just restarted? A12 log: `MQTT broker unavailable` |
| A12 ignores the HA sensor | `HOME_ASSISTANT_TOKEN` set, entity ID exactly as in HA, state really `on` (Developer tools → States) |

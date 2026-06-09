#!/usr/bin/env python3
"""
HA Bridge — subscribes to MQTT topics for Mi lamp commands,
translates them to Home Assistant REST API calls, and publishes status back.
"""

import asyncio
import json
import logging
import urllib.request
import urllib.error

from config import (
    MQTT_HOST, MQTT_PORT, HA_BASE, read_ha_token,
    MI_LAMP,
    TOPIC_MI_COMMAND, TOPIC_ALL_COMMAND, TOPIC_MI_STATUS,
)

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="[HA-Bridge] %(message)s")
log = logging.getLogger("ha-bridge")


def ha_api(method, endpoint, data=None):
    """Call Home Assistant REST API."""
    token = read_ha_token()
    url = f"{HA_BASE}{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        log.error(f"HA API error {e.code}: {e.read().decode()[:200]}")
        return None


def handle_command(payload: dict):
    """Parse a command dict and call HA services."""
    state = payload.get("state", "").upper()
    brightness = payload.get("brightness")
    color_temp = payload.get("color_temp")  # Kelvin
    effect = payload.get("effect")

    # Build HA service data
    service_data = {"entity_id": MI_LAMP}

    if state == "ON":
        service = "turn_on"
        if brightness is not None:
            # Convert 0-100 percentage to 0-255 if needed
            if brightness <= 100 and brightness > 1:
                service_data["brightness"] = int(brightness * 255 / 100)
            else:
                service_data["brightness"] = brightness
        if color_temp is not None:
            service_data["color_temp_kelvin"] = color_temp
        if effect:
            service_data["effect"] = effect
    elif state == "OFF":
        service = "turn_off"
    elif state == "TOGGLE":
        service = "toggle"
    else:
        log.warning(f"Unknown state: {state}")
        return

    result = ha_api("POST", f"/api/services/light/{service}", service_data)
    log.info(f"{service} → {result}")
    return result


def on_connect(client, userdata, flags, rc, properties=None):
    log.info(f"Connected to broker (rc={rc})")
    client.subscribe([(TOPIC_MI_COMMAND, 0), (TOPIC_ALL_COMMAND, 0)])


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning(f"Invalid JSON on {topic}: {e}")
        return

    log.info(f"← {topic}: {payload}")
    if topic == TOPIC_ALL_COMMAND or topic == TOPIC_MI_COMMAND:
        result = handle_command(payload)
        client.publish(TOPIC_MI_STATUS, json.dumps({
            "entity_id": MI_LAMP,
            "state": "unknown",
            "command": payload,
            "ha_response": result if result else "no_response",
            "source": topic,
        }))


async def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ha-bridge")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    log.info("HA Bridge started. Waiting for commands...")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        client.loop_stop()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
ADB Bridge — subscribes to MQTT topics for Huawei Smart Life.
Supports:
  - Light ON/OFF/TOGGLE (via main page quick toggle)
  - Sensor reading (豪恩空气质量检测仪 via detail page reverse-swipe)
  - Auto sensor poll (via cron trigger)
"""

import asyncio
import json
import logging
import subprocess
import time
import xml.etree.ElementTree as ET

from config import (
    MQTT_HOST, MQTT_PORT, ADB_SERIAL,
    TOPIC_HUAWEI_COMMAND, TOPIC_HUAWEI_STATUS, TOPIC_HUAWEI_SENSOR,
)

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="[ADB-Bridge] %(message)s")
log = logging.getLogger("adb-bridge")

# ── Coordinates (Vivo V2546A, 1440×3168) ──
TAP_RIGHT_CARD = (1068, 1680)   # 右列设备卡（达伦智能台灯5i）
TAP_LEFT_CARD = (372, 1680)     # 左列设备卡（豪恩空气质量检测仪）
TAP_QUICK_TOGGLE = (1280, 1784) # 主页面快捷开关
TAP_BACK = (112, 264)           # 详情页返回按钮


def adb(*args, timeout=30):
    """Run an ADB command on the Vivo phone."""
    cmd = ["adb", "-s", ADB_SERIAL] + list(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.stdout, result.returncode
    except subprocess.TimeoutExpired:
        log.error(f"ADB timeout: {' '.join(cmd)}")
        return "", -1


def adb_shell(cmd, timeout=30):
    return adb("shell", cmd, timeout=timeout)


def tap(x, y):
    adb_shell(f"input tap {x} {y}")
    time.sleep(0.5)


def dump_ui(path="/sdcard/ui_temp.xml"):
    """Dump uiautomator XML and return parsed text nodes."""
    adb_shell(f"uiautomator dump {path}", timeout=10)
    time.sleep(0.5)
    xml_out, rc = adb_shell(f"cat {path}", timeout=10)
    if rc != 0 or not xml_out.strip():
        return []
    try:
        root = ET.fromstring(xml_out)
        return [
            e.get("text", "").strip()
            for e in root.iter()
            if e.get("text", "").strip()
        ]
    except ET.ParseError:
        return []


# ── Light control ──

def get_light_state() -> str:
    """Check toggle button state on main page."""
    stdout, _ = adb_shell(
        'uiautomator dump /sdcard/ui_state.xml 2>/dev/null; '
        'grep -oP \'content-desc="[^"]*"\' /sdcard/ui_state.xml',
        timeout=10,
    )
    if "关闭" in stdout:
        return "OFF"
    elif "开启" in stdout:
        return "ON"
    return "UNKNOWN"


def force_stop_app():
    adb_shell("am force-stop com.huawei.smarthome")
    time.sleep(0.5)


def ensure_app_open():
    adb_shell(
        "am start -W -n com.huawei.smarthome/.login.LauncherActivity 2>/dev/null"
    )
    time.sleep(6)


def handle_light_command(state: str) -> dict:
    """Light ON/OFF/TOGGLE via main page quick toggle."""
    force_stop_app()
    ensure_app_open()

    if state == "TOGGLE":
        current = get_light_state()
        log.info(f"Current Huawei light state: {current}")
        state = "ON" if current == "OFF" else "OFF"

    tap(*TAP_QUICK_TOGGLE)
    time.sleep(2)

    new_state = get_light_state()
    log.info(f"Huawei light toggled → detected: {new_state}")
    adb_shell("input keyevent KEYCODE_HOME")
    return {"state": new_state}


# ── Sensor reading ──

def read_sensors() -> dict:
    """
    Navigate to 豪恩 sensor detail page, reverse-swipe to expose WebView content,
    parse uiautomator XML, and return structured sensor data.
    """
    force_stop_app()
    ensure_app_open()

    # Tap left column device card (豪恩空气质量检测仪)
    tap(*TAP_LEFT_CARD)
    time.sleep(5)  # Wait for WebView to start loading

    # Reverse swipe technique (MUST: swipe → dump → swipe → dump)
    adb_shell("input swipe 720 2200 720 500 1000")   # swipe down (≥800ms)
    time.sleep(1)
    adb_shell("uiautomator dump /sdcard/sensor_step1.xml")  # step1: required pre-step
    time.sleep(0.5)

    adb_shell("input swipe 720 500 720 2200 1000")   # swipe up — triggers data exposure
    time.sleep(1)
    adb_shell("uiautomator dump /sdcard/sensor_step2.xml")  # step2: has full data
    time.sleep(0.5)

    # Parse step2 XML
    xml_out, rc = adb_shell("cat /sdcard/sensor_step2.xml", timeout=10)
    if rc != 0 or not xml_out.strip():
        log.warning("Failed to read sensor XML")
        adb_shell("input keyevent KEYCODE_HOME")
        return {"error": "no_xml"}

    try:
        root = ET.fromstring(xml_out)
        texts = [e.get("text", "").strip() for e in root.iter() if e.get("text", "").strip()]
    except ET.ParseError:
        log.warning("Failed to parse sensor XML")
        adb_shell("input keyevent KEYCODE_HOME")
        return {"error": "parse_failed"}

    # Extract sensor values by label lookup (pattern: value → unit → label)
    result = {}
    for i, t in enumerate(texts):
        if t == "二氧化碳" and i >= 2:
            result["co2"] = f"{texts[i-2]} {texts[i-1]}"
            try: result["co2_value"] = float(texts[i-2])
            except ValueError: pass
        elif t == "甲醛" and i >= 2:
            result["甲醛"] = f"{texts[i-2]} {texts[i-1]}"
        elif t == "当前温度" and i >= 2:
            result["温度"] = f"{texts[i-2]} {texts[i-1]}"
            try: result["温度_value"] = float(texts[i-2])
            except ValueError: pass
        elif t == "当前湿度" and i >= 2:
            result["湿度"] = f"{texts[i-2]} {texts[i-1]}"
            try: result["湿度_value"] = float(texts[i-2])
            except ValueError: pass

    # Go back to home
    adb_shell("input keyevent KEYCODE_HOME")
    log.info(f"Sensor data: {result}")
    return result


# ── Command dispatcher ──

def handle_command(payload: dict) -> dict:
    action = payload.get("action", "")

    if action == "read_sensor":
        log.info("Action: read_sensor")
        return read_sensors()

    # Default: light command
    state = payload.get("state", "").upper()
    if state in ("ON", "OFF", "TOGGLE"):
        return handle_light_command(state)

    log.warning(f"Unknown command: {payload}")
    return {"error": f"unknown_command: {payload}"}


# ── MQTT ──

def on_connect(client, userdata, flags, rc, properties=None):
    log.info(f"Connected to broker (rc={rc})")
    client.subscribe([(TOPIC_HUAWEI_COMMAND, 0)])


def on_message(client, userdata, msg):
    topic = msg.topic
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log.warning(f"Invalid JSON on {topic}: {e}")
        return

    log.info(f"← {topic}: {payload}")
    if topic == TOPIC_HUAWEI_COMMAND:
        result = handle_command(payload)
        if result:
            # Publish light status or sensor data to appropriate topic
            if "co2" in result or "温度" in result or "action" in payload:
                client.publish(TOPIC_HUAWEI_SENSOR, json.dumps(result))
            else:
                client.publish(TOPIC_HUAWEI_STATUS, json.dumps(result))


async def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="adb-bridge")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    log.info("ADB Bridge started. Waiting for commands...")
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        client.loop_stop()


if __name__ == "__main__":
    asyncio.run(main())

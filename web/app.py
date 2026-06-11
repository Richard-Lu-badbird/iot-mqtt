#!/usr/bin/env python3
"""
IoT-MQTT Web Dashboard — FastAPI 后端 v2
加 SQLite 存储、4 图表数据、HA 0亮度=关灯、分区域展示。
"""

import asyncio
import json
import re
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import paho.mqtt.client as mqtt
import uvicorn

from sensor_db import save_reading, get_latest, get_history, get_chart_data

# ── Config ──
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
HA_BASE = "http://localhost:8123"
HA_TOKEN_PATH="/home/aidlux/Desktop/New Folder/long_time.txt"
HA_LIGHT = "light.yeelink_cn_649928373_lamp27_s_2_light"

BASE_DIR = Path(__file__).parent

TOPIC_MI_CMD = "home/light/mi/command"
TOPIC_MI_ST = "home/light/mi/status"
TOPIC_HW_CMD = "home/light/huawei/command"
TOPIC_HW_ST = "home/light/huawei/status"
TOPIC_SENSOR = "home/sensor/huawei/status"
TOPIC_YS_CMD = "home/camera/ys/command"
TOPIC_YS_SNAP = "home/camera/ys/snapshot"
TOPIC_YS_DET = "home/camera/ys/detection"

YS_IMAGE_DIR = Path("/home/aidlux/Desktop/ys")

app = FastAPI(title="IoT-MQTT Dashboard")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
# Mount camera snapshot directory for direct image access
if YS_IMAGE_DIR.exists():
    app.mount("/ys", StaticFiles(directory=str(YS_IMAGE_DIR)), name="ys")

TEMPLATE_HTML = (BASE_DIR / "templates" / "index.html").read_text("utf-8")


# ── MQTT Client ──
class MQTTClient:
    def __init__(self):
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="web-dash")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self._waiters = {}
        self._event_queue = asyncio.Queue()

    def _on_connect(self, c, u, flags, rc, props=None):
        print(f"[MQTT] Connected (rc={rc})")
        c.subscribe([(TOPIC_MI_ST, 0), (TOPIC_HW_ST, 0), (TOPIC_SENSOR, 0),
                     (TOPIC_YS_SNAP, 0), (TOPIC_YS_DET, 0)])

    def _on_message(self, c, u, msg):
        topic = msg.topic
        payload = msg.payload.decode("utf-8", errors="replace")
        if topic in self._waiters:
            ev, holder = self._waiters.pop(topic)
            holder["data"] = payload
            ev.set()
        try:
            asyncio.run_coroutine_threadsafe(
                self._event_queue.put({"topic": topic, "payload": payload}),
                asyncio.get_event_loop(),
            )
        except RuntimeError:
            pass

    def start(self):
        self.client.connect_async(MQTT_HOST, MQTT_PORT, 60)
        self.client.loop_start()

    async def publish_and_wait(self, topic, payload, wait_topic=None, timeout=30):
        if wait_topic:
            ev = asyncio.Event()
            holder = {}
            self._waiters[wait_topic] = (ev, holder)
        self.client.publish(topic, json.dumps(payload, ensure_ascii=False))
        if wait_topic:
            try:
                await asyncio.wait_for(ev.wait(), timeout=timeout)
                return json.loads(holder.get("data", "{}"))
            except asyncio.TimeoutError:
                return {"error": "timeout"}
        return {"sent": True}

    async def wait_for_sensor(self, timeout=35):
        ev = asyncio.Event()
        holder = {}
        self._waiters[TOPIC_SENSOR] = (ev, holder)
        self.client.publish(TOPIC_HW_CMD, json.dumps({"action": "read_sensor"}))
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
            data = json.loads(holder.get("data", "{}"))
            # 自动保存到 SQLite
            if "co2_value" in data:
                save_reading(data, source="manual")
            return data
        except asyncio.TimeoutError:
            return {"error": "timeout"}


mqtt_client = MQTTClient()


# ── Helpers ──
def read_ha_token():
    try:
        with open(HA_TOKEN_PATH) as f:
            return f.read().strip()
    except Exception:
        return None


def ha_api(method, endpoint, data=None):
    """Call HA REST API."""
    token = read_ha_token()
    if not token:
        return None
    url = f"{HA_BASE}{endpoint}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        return {"error": str(e)}


# ── Routes ──
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=TEMPLATE_HTML)


@app.get("/api/command")
async def api_command(q: str = Query(...)):
    """发送自然语言指令。"""
    payload = parse_natural(q)
    target = extract_target(q) or "全部"

    if target == "米家":
        topic, wait_topic = TOPIC_MI_CMD, TOPIC_MI_ST
        label = "米家台灯"
    elif target == "华为":
        topic = TOPIC_HW_CMD
        label = "华为设备"
        wait_topic = TOPIC_SENSOR if payload.get("action") == "read_sensor" else TOPIC_HW_ST
    else:
        for t in [TOPIC_MI_CMD, TOPIC_HW_CMD]:
            mqtt_client.client.publish(t, json.dumps(payload, ensure_ascii=False))
        return {"label": "全部设备", "command": payload, "response": {"sent": True}}

    result = await mqtt_client.publish_and_wait(topic, payload, wait_topic)
    return {"label": label, "command": payload, "response": result}


@app.get("/api/command/raw")
async def api_command_raw(topic: str = Query(...), payload: str = Query(...), wait: str = ""):
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    result = await mqtt_client.publish_and_wait(topic, data, wait if wait else None)
    return result


@app.get("/api/ha/state")
async def ha_light_state():
    result = ha_api("GET", f"/api/states/{HA_LIGHT}")
    if not result or "error" in (result or {}):
        return result or {"error": "no response"}
    attrs = result.get("attributes", {})
    return {
        "state": result["state"],
        "brightness": attrs.get("brightness"),
        "color_temp": attrs.get("color_temp_kelvin"),
        "effect": attrs.get("effect"),
        "friendly_name": attrs.get("friendly_name"),
    }


@app.get("/api/ha/command")
async def ha_command(action: str = Query(...), brightness: int = None, color_temp: int = None, effect: str = None):
    """直接控制米家台灯（不走 MQTT，直接调 HA API）。"""
    service = "turn_off" if action == "turn_off" else "turn_on"
    data = {"entity_id": HA_LIGHT}
    if action == "turn_on":
        if brightness is not None:
            if brightness == 0:
                # 亮度0 = 关灯
                service = "turn_off"
            else:
                data["brightness"] = brightness
        if color_temp is not None:
            data["color_temp_kelvin"] = color_temp
        if effect:
            data["effect"] = effect
    result = ha_api("POST", f"/api/services/light/{service}", data)
    return {"service": service, "result": result}


@app.get("/api/sensor/latest")
async def sensor_latest():
    return get_latest() or {"error": "no data"}


@app.get("/api/sensor/history")
async def sensor_history():
    """返回图表的完整数据。"""
    return get_chart_data(limit=100)


@app.get("/api/sensor/read")
async def sensor_read():
    """触发传感器读取 → 自动存 SQLite → 返回。"""
    result = await mqtt_client.wait_for_sensor()
    return result


@app.get("/api/camera/capture")
async def camera_capture():
    """Trigger Ezviz snapshot capture."""
    result = await mqtt_client.publish_and_wait(
        TOPIC_YS_CMD, {"action": "capture"}, wait_topic=TOPIC_YS_SNAP
    )
    return result


@app.get("/api/camera/detect")
async def camera_detect():
    """Trigger Ezviz capture + YOLOv11 person detection."""
    result = await mqtt_client.publish_and_wait(
        TOPIC_YS_CMD, {"action": "detect"}, wait_topic=TOPIC_YS_DET
    )
    return result


@app.get("/api/camera/status")
async def camera_status():
    """Check camera device status."""
    result = await mqtt_client.publish_and_wait(
        TOPIC_YS_CMD, {"action": "status"}, wait_topic=TOPIC_YS_SNAP
    )
    return result


@app.get("/api/camera/latest")
async def camera_latest():
    """Return the latest snapshot filename from the ys directory."""
    if not YS_IMAGE_DIR.exists():
        return {"error": "no directory"}
    jpgs = sorted(YS_IMAGE_DIR.glob("ys_*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jpgs:
        return {"error": "no snapshots"}
    latest = jpgs[0]
    # Check for a corresponding detected image
    detected = YS_IMAGE_DIR / f"detected_{latest.name}"
    return {
        "filename": latest.name,
        "url": f"/ys/{latest.name}",
        "detected_url": f"/ys/detected_{latest.name}" if detected.exists() else None,
        "timestamp": latest.stat().st_mtime,
        "size_kb": round(latest.stat().st_size / 1024, 1),
    }


@app.get("/api/camera/history")
async def camera_history(limit: int = 20):
    """List recent snapshots."""
    if not YS_IMAGE_DIR.exists():
        return {"error": "no directory"}
    jpgs = sorted(YS_IMAGE_DIR.glob("ys_*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
    results = []
    for p in jpgs[:limit]:
        detected = YS_IMAGE_DIR / f"detected_{p.name}"
        results.append({
            "filename": p.name,
            "url": f"/ys/{p.name}",
            "detected_url": f"/ys/detected_{p.name}" if detected.exists() else None,
            "timestamp": p.stat().st_mtime,
            "size_kb": round(p.stat().st_size / 1024, 1),
        })
    return results


@app.get("/api/status")
async def system_status():
    connected = mqtt_client.client.is_connected()
    return {
        "broker": connected,
        "ha_bridge": connected,
        "adb_bridge": connected,
        "camera_bridge": connected,
        "sensor_count": len(get_history(9999)),
    }


@app.on_event("startup")
async def startup():
    await asyncio.sleep(0.5)  # let SQLite init finish
    mqtt_client.start()


# ── Natural language parsers (same as pub.py) ──
def parse_natural(text: str) -> dict:
    text = text.strip()
    payload = {}
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    is_sensor = bool(re.search(r"传感器|空气质量|读数|检测|数据|pm|co2", text, re.I))
    if is_sensor:
        payload["action"] = "read_sensor"
    if not is_sensor:
        if re.search(r"开|亮|打开|开启|on", text, re.I):
            payload["state"] = "ON"
        elif re.search(r"关|灭|关闭|off", text, re.I):
            payload["state"] = "OFF"
        elif re.search(r"切换|toggle|翻转", text, re.I):
            payload["state"] = "TOGGLE"
        else:
            payload["state"] = "TOGGLE"
    bm = re.search(r"亮度.*?(\d+)", text)
    if bm:
        payload["brightness"] = min(int(bm.group(1)), 100)
    tm = re.search(r"色温.*?(\d+)", text)
    if tm:
        payload["color_temp"] = int(tm.group(1))
    for mode in ["阅读", "电脑", "温馨", "办公", "休闲", "娱乐", "自由调节"]:
        if mode in text:
            payload["effect"] = f"{mode}模式"
            break
    return payload


def extract_target(text: str):
    targets = [("小米", "米家"), ("米家", "米家"), ("yeelight", "米家"),
               ("台灯", "米家"), ("华为", "华为"), ("huawei", "华为"), ("智慧生活", "华为")]
    for kw, resolved in targets:
        if kw in text.lower():
            return resolved
    return None


if __name__ == "__main__":
    print("🚀 IoT-MQTT Dashboard v2 starting...")
    print("   http://localhost:8765")
    uvicorn.run(app, host="0.0.0.0", port=8765)

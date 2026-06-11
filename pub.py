#!/usr/bin/env python3
"""
一句话控制 — 统一 MQTT 命令发送器
发送一条命令到 MQTT, 由 Bridge 自动分发给对应设备。

用法：
  python3 pub.py "开灯"
  python3 pub.py "关灯" "全部"
  python3 pub.py '{"state":"ON","brightness":80}' "米家"
  python3 pub.py '{"state":"OFF"}' "华为"
"""

import json
import sys
import re
import time

import paho.mqtt.client as mqtt

from config import (
    MQTT_HOST, MQTT_PORT,
    TOPIC_MI_COMMAND, TOPIC_HUAWEI_COMMAND, TOPIC_ALL_COMMAND,
    TOPIC_MI_STATUS, TOPIC_HUAWEI_STATUS, TOPIC_HUAWEI_SENSOR,
    TOPIC_YS_COMMAND, TOPIC_YS_SNAPSHOT, TOPIC_YS_DETECTION,
)

# ── 自然语言 → 结构化命令 (简易版) ──

def parse_natural(text: str) -> dict:
    """Convert simple Chinese commands to structured payload."""
    text = text.strip()
    payload = {}

    # 先尝试 JSON
    if text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

    # 传感器读取优先
    is_sensor = bool(re.search(r"传感器|空气质量|读数|检测|数据|pm|co2", text, re.I))
    if is_sensor:
        payload["action"] = "read_sensor"

    # 摄像头指令
    is_camera = bool(re.search(r"萤石|拍照|截图|摄像头|抓拍|ys|detect|capture|监控", text, re.I))
    if is_camera:
        if re.search(r"检测|detect|人", text, re.I):
            payload["action"] = "detect"
        elif re.search(r"连拍|多张|连续", text, re.I):
            m = re.search(r"(\d+)张", text)
            payload["action"] = "capture_n"
            payload["count"] = int(m.group(1)) if m else 5
        else:
            payload["action"] = "capture"

    # 状态（传感器/摄像头指令不设默认 state）
    if not is_sensor and not is_camera:
        if re.search(r"开|亮|打开|开启|on", text, re.I):
            payload["state"] = "ON"
        elif re.search(r"关|灭|关闭|off", text, re.I):
            payload["state"] = "OFF"
        elif re.search(r"切换|toggle|翻转", text, re.I):
            payload["state"] = "TOGGLE"
        else:
            payload["state"] = "TOGGLE"

    # 亮度
    brightness_match = re.search(r"亮度.*?(\d+)", text)
    if brightness_match:
        val = int(brightness_match.group(1))
        payload["brightness"] = min(val, 100)

    # 色温
    temp_match = re.search(r"色温.*?(\d+)", text)
    if temp_match:
        payload["color_temp"] = int(temp_match.group(1))

    # 模式
    for mode in ["阅读", "电脑", "温馨", "办公", "休闲", "娱乐", "自由调节"]:
        if mode in text:
            payload["effect"] = f"{mode}模式"
            break

    return payload


def extract_target(text: str) -> str:
    """Extract device target from natural language text."""
    targets = [
        ("米家", "米家"),
        ("小米", "米家"),
        ("yeelight", "米家"),
        ("萤石", "萤石"),
        ("ys", "萤石"),
        ("摄像头", "萤石"),
        ("拍照", "萤石"),
        ("截图", "萤石"),
        ("监控", "萤石"),
        ("台灯", "米家"),
        ("华为", "华为"),
        ("huawei", "华为"),
        ("智慧生活", "华为"),
    ]
    for keyword, resolved in targets:
        if keyword in text.lower():
            return resolved
    return None


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    text = sys.argv[1]
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    wait_mode = "--wait" in sys.argv

    target = args[0] if args else "全部"

    payload = parse_natural(text)
    payload_str = json.dumps(payload, ensure_ascii=False)

    # Auto-detect target from text if not explicitly given
    if target == "全部":
        detected = extract_target(text)
        if detected:
            target = detected

    # 确定 topic
    target_lower = target.lower()
    if target_lower in ("米家", "小米", "台灯", "mi", "yeelight"):
        topics = [TOPIC_MI_COMMAND]
        label = "米家台灯"
        wait_topic = TOPIC_MI_STATUS
    elif target_lower in ("华为", "huawei", "智慧生活"):
        topics = [TOPIC_HUAWEI_COMMAND]
        label = "华为智慧生活灯"
        wait_topic = TOPIC_HUAWEI_SENSOR if payload.get("action") == "read_sensor" else TOPIC_HUAWEI_STATUS
    elif target_lower in ("萤石", "ys", "摄像头", "拍照", "截图", "监控"):
        topics = [TOPIC_YS_COMMAND]
        label = "萤石摄像头"
        # camera actions: capture → wait for snapshot, detect → wait for detection
        action = payload.get("action", "")
        wait_topic = TOPIC_YS_DETECTION if action == "detect" else TOPIC_YS_SNAPSHOT
    else:
        topics = [TOPIC_ALL_COMMAND, TOPIC_MI_COMMAND, TOPIC_HUAWEI_COMMAND]
        label = "全部设备"
        wait_topic = None

    # 发送
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iot-ctl")
    client.connect(MQTT_HOST, MQTT_PORT, 10)

    for topic in topics:
        client.publish(topic, payload_str)
        print(f"  📤 → {topic}: {payload_str}")

    print(f"\n✅ 已发送指令到「{label}」: {payload_str}")

    # ── Wait mode: 发完后等待响应 ──
    if wait_mode and wait_topic:
        print(f"\n⏳ 等待响应 (topic: {wait_topic})...")
        result = [None]
        def on_msg(c, u, msg):
            if result[0] is None:
                result[0] = msg.payload.decode()

        waiter = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iot-ctl-wait")
        waiter.on_message = on_msg
        waiter.connect(MQTT_HOST, MQTT_PORT, 10)
        waiter.loop_start()
        waiter.subscribe(wait_topic)

        waited = 0
        while result[0] is None and waited < 30:
            time.sleep(0.5)
            waited += 0.5

        waiter.loop_stop()

        if result[0]:
            print(f"\n📡 响应:")
            try:
                parsed = json.loads(result[0])
                print(json.dumps(parsed, ensure_ascii=False, indent=2))
            except json.JSONDecodeError:
                print(result[0])
        else:
            print("\n⚠️  超时，未收到响应")

    client.disconnect()


if __name__ == "__main__":
    main()

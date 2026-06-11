#!/usr/bin/env python3
"""
华为空气传感器定时采集脚本。
读取传感器数据并保存到 SQLite，同时发布到 MQTT。
"""

import json
import sys
import time

import paho.mqtt.client as mqtt

# Same config as iot-mqtt
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
TOPIC_HUAWEI_COMMAND = "home/light/huawei/command"
TOPIC_HUAWEI_SENSOR = "home/sensor/huawei/status"

# Use SQLite instead of JSONL
import sys
sys.path.insert(0, "/home/aidlux/iot-mqtt/web")
from sensor_db import save_reading

sensor_result = None


def on_sensor_msg(client, userdata, msg):
    global sensor_result
    sensor_result = msg.payload.decode()
    print(f"[poll] Received: {sensor_result}")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sensor-poll")
    client.on_message = on_sensor_msg
    client.connect(MQTT_HOST, MQTT_PORT, 10)
    client.loop_start()
    client.subscribe(TOPIC_HUAWEI_SENSOR, qos=0)

    cmd = json.dumps({"action": "read_sensor"})
    print(f"[poll] Sending: {cmd}")
    client.publish(TOPIC_HUAWEI_COMMAND, cmd)

    waited = 0
    while sensor_result is None and waited < 35:
        time.sleep(1)
        waited += 1

    client.loop_stop()

    if sensor_result:
        try:
            data = json.loads(sensor_result)
            if "co2_value" in data:
                save_reading(data, source="cron")
                print(f"[poll] Saved to SQLite")
            print(f"[poll] {sensor_result}")
        except json.JSONDecodeError:
            print(f"[poll] Parse error: {sensor_result}")
            sys.exit(1)
    else:
        print("[poll] FAILED: No sensor data received within timeout")
        sys.exit(1)


if __name__ == "__main__":
    main()

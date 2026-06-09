#!/usr/bin/env python3
"""
华为空气传感器定时采集脚本。
读取传感器数据并发布到 home/sensor/huawei/status，
同时保存到文件供历史查看。
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

LOG_FILE = "/home/aidlux/iot-mqtt/sensor_history.jsonl"

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

    # Subscribe to sensor topic
    client.subscribe(TOPIC_HUAWEI_SENSOR, qos=0)

    # Send read command
    cmd = json.dumps({"action": "read_sensor"})
    print(f"[poll] Sending: {cmd}")
    client.publish(TOPIC_HUAWEI_COMMAND, cmd)

    # Wait for result (ADB takes ~20s)
    waited = 0
    while sensor_result is None and waited < 35:
        time.sleep(1)
        waited += 1
        print(f"[poll] Waiting... {waited}s")

    client.loop_stop()

    if sensor_result:
        # Append to history
        entry = json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "data": json.loads(sensor_result),
        })
        with open(LOG_FILE, "a") as f:
            f.write(entry + "\n")
        print(f"[poll] Logged to {LOG_FILE}")
        print(f"[poll] {sensor_result}")
    else:
        print("[poll] FAILED: No sensor data received within timeout")
        sys.exit(1)


if __name__ == "__main__":
    main()

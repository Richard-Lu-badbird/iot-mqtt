#!/usr/bin/env python3
"""Shared configuration for IoT-MQTT ecosystem."""

# ── MQTT Broker ──
MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883

# ── Home Assistant ──
HA_BASE = "http://localhost:8123"
HA_TOKEN_PATH = "/home/aidlux/Desktop/New Folder/long_time.txt"

def read_ha_token():
    with open(HA_TOKEN_PATH) as f:
        return f.read().strip()

# ── Device entity IDs ──
MI_LAMP = "light.yeelink_cn_649928373_lamp27_s_2_light"

# ── ADB (Vivo 手机) ──
ADB_SERIAL = "10AFCN0T0G0012D"

# ── Topic hierarchy ──
TOPIC_MI_COMMAND = "home/light/mi/command"      # commands for Mi lamp
TOPIC_HUAWEI_COMMAND = "home/light/huawei/command"  # commands for Huawei light
TOPIC_ALL_COMMAND = "home/light/all/command"    # commands for all
TOPIC_MI_STATUS = "home/light/mi/status"        # status feedback
TOPIC_HUAWEI_STATUS = "home/light/huawei/status"
TOPIC_HUAWEI_SENSOR = "home/sensor/huawei/status"  # 传感器数据

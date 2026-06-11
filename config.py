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

# ── Ezviz (萤石) Camera ──
EVIZ_APP_KEY = "15995db1c35e41eebd47c73867db1e53"
EVIZ_APP_SECRET = "37c9d7a3003447f0b8fe0bcd8869af06"
EVIZ_ACCESS_TOKEN = "at.7vj0csftdvqgsnnic1cfurh3a4hy2z9i-4jorjc98ol-0ljzp1l-6esgt95gd"
EVIZ_BASE_URL = "https://open.ys7.com/api/lapp"
EVIZ_DEVICE_SERIAL = "BG9434316"  # C6c 摄像头序列号

# ── YOLOv11 Model ──
YOLO11_MODEL_PATH = "/home/aidlux/Desktop/ys/models/models/QCS8550/W8A8/cutoff_yolo11n_qcs8550_w8a8.qnn236.ctx.bin"
YOLO11_SAVE_DIR = "/home/aidlux/Desktop/ys"

# ── Topic hierarchy ──
TOPIC_MI_COMMAND = "home/light/mi/command"      # commands for Mi lamp
TOPIC_HUAWEI_COMMAND = "home/light/huawei/command"  # commands for Huawei light
TOPIC_ALL_COMMAND = "home/light/all/command"    # commands for all
TOPIC_MI_STATUS = "home/light/mi/status"        # status feedback
TOPIC_HUAWEI_STATUS = "home/light/huawei/status"
TOPIC_HUAWEI_SENSOR = "home/sensor/huawei/status"  # 传感器数据
TOPIC_YS_COMMAND = "home/camera/ys/command"     # 萤石摄像头命令
TOPIC_YS_SNAPSHOT = "home/camera/ys/snapshot"   # 萤石截图结果
TOPIC_YS_DETECTION = "home/camera/ys/detection" # 萤石检测结果

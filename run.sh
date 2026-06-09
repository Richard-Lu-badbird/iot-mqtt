#!/usr/bin/env bash
# IoT-MQTT 启动器 — 一键启动整个 MQTT 智能家居系统
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

case "${1:-start}" in
  start)
    echo "🚀 启动 MQTT Broker + Bridges..."
    
    # Start broker
    python3 -u broker.py > /tmp/iot-mqtt-broker.log 2>&1 &
    BROKER_PID=$!
    echo "  📡 Broker PID: $BROKER_PID" | tee -a /tmp/iot-mqtt-broker.log
    sleep 2
    
    # Start bridges
    python3 -u ha_bridge.py > /tmp/iot-mqtt-ha.log 2>&1 &
    HA_PID=$!
    echo "  💡 HA Bridge PID: $HA_PID"
    
    python3 -u adb_bridge.py > /tmp/iot-mqtt-adb.log 2>&1 &
    ADB_PID=$!
    echo "  📱 ADB Bridge PID: $ADB_PID"
    
    echo ""
    echo "✅ 全部启动完毕！"
    echo "   发送命令: python3 pub.py '开灯'"
    echo "   发送命令: python3 pub.py '关灯' '华为'"
    echo "   发送命令: python3 pub.py '{\"state\":\"ON\",\"brightness\":80}' '米家'"
    echo ""
    echo "   查看日志: less /tmp/iot-mqtt-broker.log"
    echo "   停止: $0 stop"
    
    echo "$BROKER_PID" > /tmp/iot-mqtt-broker.pid
    echo "$HA_PID" > /tmp/iot-mqtt-ha.pid
    echo "$ADB_PID" > /tmp/iot-mqtt-adb.pid
    ;;
    
  stop)
    echo "🛑 停止所有服务..."
    for pid_file in /tmp/iot-mqtt-broker.pid /tmp/iot-mqtt-ha.pid /tmp/iot-mqtt-adb.pid; do
      if [ -f "$pid_file" ]; then
        kill $(cat "$pid_file") 2>/dev/null || true
        rm -f "$pid_file"
      fi
    done
    pkill -f "python3 .*broker.py" 2>/dev/null || true
    pkill -f "python3 .*ha_bridge" 2>/dev/null || true
    pkill -f "python3 .*adb_bridge" 2>/dev/null || true
    echo "✅ 已停止"
    ;;
    
  status)
    echo "📊 状态检查："
    for pair in "broker:broker" "ha:ha_bridge" "adb:adb_bridge"; do
      label="${pair%%:*}"
      script="${pair##*:}"
      pid_file="/tmp/iot-mqtt-${label}.pid"
      if [ -f "$pid_file" ] && kill -0 $(cat "$pid_file") 2>/dev/null; then
        echo "  ✅ ${label} — 运行中 (PID: $(cat $pid_file))"
      else
        echo "  ❌ ${label} — 未运行"
      fi
    done
    ;;
    
  test)
    echo "🧪 测试 MQTT Broker + Bridges..."
    python3 << 'PYEOF'
import paho.mqtt.client as mqtt
import time, json, sys

errors = []

# --- Test 1: Broker pub/sub ---
print("  Test 1: Broker pub/sub...", end=" ", flush=True)
received = []
def on_msg(c, u, msg):
    received.append((msg.topic, msg.payload.decode()))

sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-sub")
sub.on_message = on_msg
try:
    sub.connect("127.0.0.1", 1883, 10)
    sub.loop_start()
    sub.subscribe("test/#")
    time.sleep(0.3)

    pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-pub")
    pub.connect("127.0.0.1", 1883, 10)
    pub.loop_start()
    time.sleep(0.3)
    pub.publish("test/hello", "MQTT alive!")
    time.sleep(1)

    if received:
        print("✅")
    else:
        print("❌ (no message)")
        errors.append("Broker pub/sub failed")
    pub.loop_stop()
    sub.loop_stop()
except Exception as e:
    print(f"❌ ({e})")
    errors.append(str(e))

# --- Test 2: HA Bridge alive ---
print("  Test 2: HA Bridge...", end=" ", flush=True)
try:
    ha_pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-ha")
    ha_pub.connect("127.0.0.1", 1883, 10)
    ha_pub.loop_start()
    time.sleep(0.3)
    ha_pub.publish("home/light/mi/command", json.dumps({"state":"ON"}))
    time.sleep(0.5)
    print("✅ (message sent)")
    ha_pub.loop_stop()
except Exception as e:
    print(f"❌ ({e})")
    errors.append(str(e))

# --- Test 3: ADB Bridge alive ---
print("  Test 3: ADB Bridge...", end=" ", flush=True)
try:
    adb_pub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="test-adb")
    adb_pub.connect("127.0.0.1", 1883, 10)
    adb_pub.loop_start()
    time.sleep(0.3)
    adb_pub.publish("home/light/huawei/command", json.dumps({"state":"TOGGLE"}))
    time.sleep(0.5)
    print("✅ (message sent)")
    adb_pub.loop_stop()
except Exception as e:
    print(f"❌ ({e})")
    errors.append(str(e))

# Summary
print()
if errors:
    print(f"❌ {len(errors)} test(s) failed:")
    for e in errors:
        print(f"   - {e}")
    sys.exit(1)
else:
    print("✅ All tests passed!")
PYEOF
    ;;
    
  *)
    echo "用法: $0 {start|stop|status|test}"
    exit 1
    ;;
esac

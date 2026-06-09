# IoT-MQTT 智能家居命令总线

> 一个纯 Python 的 MQTT 智能家居控制系统，通过一句话语音/文字指令统一控制**米家台灯**（Home Assistant）和**华为智慧生活灯**（ADB + uiautomator）。支持空气质量传感器定时采集。

---

## 目录

- [背景：MQTT 是什么](#背景mqtt-是什么)
- [架构总览](#架构总览)
- [项目结构](#项目结构)
- [组件详解](#组件详解)
  - [Broker — 消息枢纽](#broker--消息枢纽)
  - [HA Bridge — 米家通道](#ha-bridge--米家通道)
  - [ADB Bridge — 华为通道](#adb-bridge--华为通道)
  - [pub.py — 统一命令入口](#pubpy--统一命令入口)
- [Topic 设计](#topic-设计)
- [使用指南](#使用指南)
- [命令参考](#命令参考)
- [自动采集](#自动采集)
- [扩展新设备](#扩展新设备)
- [常见问题](#常见问题)

---

## 背景：MQTT 是什么

**MQTT** 全称是 **MQ Telemetry Transport**，不是 Message Queue。

这是一个常见误解。MQTT 是**发布/订阅（Pub/Sub）模型**，消息发出去后 Broker 立即推送给所有订阅者，**不排队、不持久化**。

| | MQTT | 消息队列（RabbitMQ/Kafka） |
|---|---|---|
| 模型 | **发布/订阅** | **队列** 或 **日志** |
| 消息留存 | 默认不存，发完即弃 | 持久化到磁盘 |
| 消费模式 | 所有订阅者收到同一份 | 消息被一个消费者拿走 |
| 先后顺序 | 不保证 | 队列顺序消费 |

要保证"先做 A 再做 B"，本项目通过 `--wait` 参数 + shell 的 `&&` 串联实现：

```bash
python3 pub.py '开华为的灯' --wait && python3 pub.py '开小米的灯' --wait
```

---

## 架构总览

```
                ┌──────────────────────────────────┐
                │    一句话指令                      │
                │  python3 pub.py '开小米的灯'       │
                │  python3 pub.py '读华为数据' --wait │
                └──────────────┬───────────────────┘
                               │ JSON @ TCP:1883
                               ▼
                ┌──────────────────────────────────┐
                │   Broker (localhost:1883)         │
                │   纯 Python MQTT v3.1.1           │
                │   asyncio + 通配符 topic 匹配     │
                └────┬──────────────────────┬───────┘
                     │                      │
           subscribe │                      │ subscribe
                     ▼                      ▼
  ┌────────────────────────┐    ┌──────────────────────────┐
  │ HA Bridge              │    │ ADB Bridge                │
  │ MQTT → HA REST API     │    │ MQTT → ADB + uiautomator  │
  ├────────────────────────┤    ├──────────────────────────┤
  │ 订阅:                   │    │ 订阅:                     │
  │ home/light/mi/command   │    │ home/light/huawei/command │
  │ home/light/all/command  │    │                          │
  │                         │    │ 功能:                     │
  │ 功能:                    │    │ • 灯 ON/OFF/TOGGLE       │
  │ • ON/OFF/TOGGLE        │    │ • 读取传感器数据          │
  │ • 亮度 0-255            │    │                          │
  │ • 色温 2600-5100K      │    │ 发布:                     │
  │ • 情景模式              │    │ home/light/huawei/status  │
  │                         │    │ home/sensor/huawei/status │
  │ 发布:                    │    └────────┬─────────────────┘
  │ home/light/mi/status   │             │
  └────────┬───────────────┘             │
           │ REST API                    │ ADB shell
           ▼                             ▼
  ┌──────────────────┐     ┌──────────────────────────┐
  │ Home Assistant    │     │ Vivo V2546A 手机          │
  │ localhost:8123    │     │ 华为智慧生活 app           │
  ├──────────────────┤     ├──────────────────────────┤
  │ 米家台灯1S 增强版 │     │ 达伦智能台灯5i (右列)     │
  │ (Yeelight 集成)  │     │ 豪恩空气质量检测仪 (左列)  │
  └──────────────────┘     └──────────────────────────┘
```

---

## 项目结构

```
~/iot-mqtt/
├── broker.py        # 轻量 MQTT Broker（asyncio，纯 Python）
├── ha_bridge.py     # HA 桥接（MQTT ↔ HA REST API）
├── adb_bridge.py    # ADB 桥接（MQTT ↔ 手机 uiautomator）
├── pub.py           # 统一命令入口（自然语言 → JSON → MQTT）
├── sensor_poll.py   # 传感器定时采集脚本
├── config.py        # 共享配置
├── run.sh           # 进程管理器（start / stop / status / test）
└── sensor_history.jsonl  # 传感器历史数据（JSONL 格式）
```

---

## 组件详解

### Broker — 消息枢纽

`broker.py` 是一个从零手写的 MQTT v3.1.1 Broker，**无外部依赖**。

**支持的功能：**
- CONNECT / CONNACK — 客户端接入
- SUBSCRIBE / SUBACK — topic 订阅（支持 `+` 和 `#` 通配符）
- PUBLISH (QoS 0) — 消息发布与转发
- PINGREQ / PINGRESP — 心跳保活
- DISCONNECT — 正常断开
- 自过滤：不将消息转发回发布者自身

**原理：** 基于 Python `asyncio.start_server`，每个客户端对应一个协程，解析 MQTT 二进制协议包，按 topic 匹配规则转发消息。

```python
# 启动 Broker
python3 broker.py
# 默认监听 0.0.0.0:1883
```

---

### HA Bridge — 米家通道

`ha_bridge.py` 连接 MQTT 和 Home Assistant REST API。

**订阅：**
- `home/light/mi/command` — 米家台灯专属指令
- `home/light/all/command` — 全设备广播指令

**支持的命令：**
| 字段 | 说明 | 示例 |
|---|---|---|
| `state` | ON / OFF / TOGGLE | `"state": "ON"` |
| `brightness` | 亮度 0-255（或 0-100% 自动转换） | `"brightness": 128` |
| `color_temp` | 色温 2600-5100K | `"color_temp": 4000` |
| `effect` | 情景模式 | `"effect": "阅读模式"` |

**支持的 11 种情景模式：**
电脑模式、温馨、休闲模式、办公模式、阅读模式、娱乐模式、自由调节模式、我的模式1~4

**发布：**
- `home/light/mi/status` — 操作结果反馈（包含 HA API 返回的完整设备状态）

---

### ADB Bridge — 华为通道

`adb_bridge.py` 通过 ADB 控制手机上的华为智慧生活 app。

**订阅：**
- `home/light/huawei/command`

**功能 1：灯控制**

使用主页面快捷开关控制灯，无需进入详情页：

```python
# Vivo V2546A (1440×3168) 坐标
TAP_QUICK_TOGGLE = (1280, 1784)  # 右列设备卡中的开关按钮
```

流程：
1. `am force-stop` 杀掉 app
2. `am start` 启动 app，等待 6 秒 UI 加载
3. 读取控件树判断当前开关状态
4. 点击快捷开关
5. 验证新状态

**功能 2：传感器读取**

进豪恩空气质量检测仪详情页，通过**反向滑动法**暴露 WebView 内容：

```
1. 点击左列设备卡 (372, 1680)
2. 等待 5s → WebView 开始加载
3. swipe down (720,2200 → 720,500, 1000ms)
4. uiautomator dump (step1 — 必需前置步骤)
5. swipe up (720,500 → 720,2200, 1000ms)
6. uiautomator dump (step2 — 有完整数据)
7. 解析 XML 提取传感器值
```

数据排列规律：**值 → 单位 → 标签**
```
1077 → ppm → 二氧化碳
0.02 → mg/m³ → 甲醛
30.3 → ℃ → 当前温度
41.6 → % → 当前湿度
```

**发布：**
- `home/light/huawei/status` — 灯状态
- `home/sensor/huawei/status` — 传感器数据

---

### pub.py — 统一命令入口

负责将自然语言或 JSON 转换为 MQTT 消息。

**参数：**
```bash
python3 pub.py "<命令>" [目标设备] [--wait]
```

**自然语言解析规则：**

| 你说 | 解析结果 |
|---|---|
| `开灯` / `打开` | `{"state": "ON"}` |
| `关灯` / `关闭` | `{"state": "OFF"}` |
| `亮度80` | `{"state":"ON", "brightness":80}` |
| `色温4000` | `{"state":"ON", "color_temp":4000}` |
| `阅读模式` | `{"state":"ON", "effect":"阅读模式"}` |
| `读空气质量` / `传感器` | `{"action": "read_sensor"}` |

**目标设备自动识别：**

| 文本里的关键词 | 路由到 |
|---|---|
| `小米` / `米家` / `yeelight` / `台灯` | 米家台灯 |
| `华为` / `huawei` / `智慧生活` | 华为设备 |
| 无关键词 | 全部设备（广播） |

**--wait 模式：**

发完命令后等待响应再退出，用于链式调用：

```bash
# 先开华为，等确认后开小米
python3 pub.py '开华为的灯' --wait && python3 pub.py '开小米的灯' --wait
```

---

## Topic 设计

```
home/
├── light/
│   ├── mi/command        ← [sub] 米家台灯指令
│   ├── mi/status         ← [pub] 米家台灯状态反馈
│   ├── huawei/command    ← [sub] 华为设备指令
│   ├── huawei/status     ← [pub] 华为灯状态反馈
│   └── all/command       ← [sub] 全部设备广播指令
└── sensor/
    └── huawei/status     ← [pub] 空气质量传感器数据
```

---

## 使用指南

### 启动系统

```bash
cd ~/iot-mqtt

# 一键启动（Broker + HA Bridge + ADB Bridge）
bash run.sh start

# 查看运行状态
bash run.sh status

# 一键停止
bash run.sh stop

# 运行测试
bash run.sh test
```

### 场景一：米家台灯 1S 增强版

通过 Home Assistant REST API 控制，响应速度快（毫秒级）。

```bash
# 基本的开/关
python3 pub.py '开小米的灯'
python3 pub.py '关米家的灯'
python3 pub.py '开台灯'

# 调亮度（0-100）
python3 pub.py '亮度调到80' '米家'
python3 pub.py '亮度50'

# 调色温（2600K暖光 ~ 5100K冷光）
python3 pub.py '暖光' '小米'
python3 pub.py '冷光'
python3 pub.py '色温4000' '米家'

# 情景模式
python3 pub.py '阅读模式'
python3 pub.py '电脑模式' '小米'
python3 pub.py '温馨模式'
python3 pub.py '办公模式'

# 组合命令（亮度 + 色温同时设置）
python3 pub.py '{"state":"ON","brightness":180,"color_temp":4000}' '米家'

# 带等待确认
python3 pub.py '开小米的灯' --wait
```

支持的 11 种情景模式：电脑模式、温馨、休闲模式、办公模式、阅读模式、娱乐模式、自由调节模式、我的模式1~4

### 场景二：华为智慧生活灯（达伦智能台灯5i）

通过 ADB 控制手机 app 的快捷开关，只能用 ON/OFF/TOGGLE，不支持亮度/色温调节（WebView 滑块无法通过 uiautomator 定位）。

```bash
# 开/关
python3 pub.py '开华为的灯'
python3 pub.py '关华为的灯'
python3 pub.py '开智慧生活的灯'

# 切换（不管当前状态，取反）
python3 pub.py '切换华为的灯'

# 带等待确认（ADB 需要 ~15 秒）
python3 pub.py '开华为的灯' --wait
# 输出：
# ⏳ 等待响应...
# 📡 响应:
# {
#   "state": "ON"     # 或 "OFF"
# }
```

### 场景三：豪恩空气质量检测仪

通过 ADB 进入详情页，反向滑动法暴露 WebView 控件树，解析 XML 提取传感器数值。

```bash
# 读取全部传感器数据
python3 pub.py '读华为的空气质量数据' --wait
# 输出：
# ⏳ 等待响应...
# 📡 响应:
# {
#   "co2": "848 ppm",
#   "co2_value": 848.0,
#   "甲醛": "0.02 mg/m³",
#   "温度": "27.1 ℃",
#   "温度_value": 27.1,
#   "湿度": "37.3 %",
#   "湿度_value": 37.3
# }

# 其他说法一样有效
python3 pub.py '读传感器数据' '华为' --wait
python3 pub.py '查看空气质量' --wait
python3 pub.py '检测华为的空气质量' --wait

# 不等待的结果会发到 MQTT，订阅就能收到
python3 -c "
import paho.mqtt.client as mqtt, json, time
def on_msg(c,u,m):
    print(json.dumps(json.loads(m.payload), ensure_ascii=False, indent=2))
sub = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
sub.on_message = on_msg
sub.connect('127.0.0.1',1883,10)
sub.subscribe('home/sensor/+/status')
sub.loop_forever()
" &
python3 pub.py '读华为的空气质量数据'

# 查历史采集记录
tail -5 ~/iot-mqtt/sensor_history.jsonl | python3 -m json.tool
```

### 场景四：多设备联动

```bash
# 全部开 / 全部关
python3 pub.py '开灯'       # 发到 mi + huawei 两个 topic
python3 pub.py '关灯'       # 同上

# 链式调用：先做 A，等完成，再做 B
python3 pub.py '开华为的灯' --wait && python3 pub.py '开小米的灯' --wait

python3 pub.py '关小米的灯' --wait && python3 pub.py '关华为的灯' --wait

# 先关灯，再读空气质量
python3 pub.py '关华为的灯' --wait && python3 pub.py '读华为的空气质量数据' --wait

# 配合 shell 脚本做场景（如"晚安模式"）
# 晚安模式：关小米灯 → 关华为灯 → 读空气质量
python3 pub.py '关小米的灯' --wait && \
python3 pub.py '关华为的灯' --wait && \
python3 pub.py '读华为的空气质量数据' --wait
```

### 日常命令速查表

| 你说 | 效果 |
|------|------|
| `python3 pub.py '开灯'` | 米家 + 华为全开 |
| `python3 pub.py '关灯'` | 全关 |
| `python3 pub.py '开小米的灯'` | 只开米家台灯 |
| `python3 pub.py '亮度80' '米家'` | 米家台灯亮度 80% |
| `python3 pub.py '阅读模式'` | 米家台灯阅读模式 |
| `python3 pub.py '开华为的灯' --wait` | 开华为灯并等确认 |
| `python3 pub.py '读华为的空气质量数据' --wait` | 读传感器并等数据 |
| `python3 pub.py '{"state":"ON","brightness":128}' '米家' ` | 直接发 JSON |

---

## 自动采集

系统通过 Hermes cron 定时采集空气质量数据，每 30 分钟一次。

```
🔁 华为空气传感器定时采集 (3:00 PM, 3:30 PM, 4:00 PM ...)
    ↓
sensor_poll.py → MQTT → ADB Bridge → 读手机 → 发布数据
    ↓
发布到 home/sensor/huawei/status
同时写入 sensor_history.jsonl
```

查历史数据：

```bash
tail -5 ~/iot-mqtt/sensor_history.jsonl | python3 -m json.tool
```

---

## 扩展新设备

增加一个新设备只需三步：

### 1. `config.py` 加 Topic

```python
TOPIC_CURTAIN_COMMAND = "home/curtain/command"
TOPIC_CURTAIN_STATUS  = "home/curtain/status"
```

### 2. 写一个 Bridge 脚本

```python
# curtain_bridge.py（~50 行）
# 订阅 TOPIC_CURTAIN_COMMAND
# 收到命令后执行设备控制
# 发布状态到 TOPIC_CURTAIN_STATUS
```

### 3. `pub.py` 加映射

```python
# extract_target() 里加关键词
("窗帘", "窗帘"),
("curtain", "窗帘"),

# main() 里加目标路由
elif target_lower in ("窗帘", "curtain"):
    topics = [TOPIC_CURTAIN_COMMAND]
    label = "窗帘"
```

搞定。一句话控制新设备：

```bash
python3 pub.py '关窗帘' --wait
```

---

## 常见问题

**Q：MQTT 和消息队列有什么区别？**

A：MQTT 是 Pub/Sub（发布/订阅）模型，消息不排队，所有订阅者同时收到。真正的消息队列（如 RabbitMQ）是点对点消费，消息被一个消费者取走。

**Q：为什么 Broker 是纯 Python 实现的，不用 Mosquitto？**

A：AidLux（ARM64 Linux 容器）上的 apt/Docker 环境依赖冲突较多，纯 Python 实现零外部依赖，部署简单可靠。

**Q：如何保证命令的先后顺序？**

A：用 `--wait` + `&&`。`pub.py --wait` 会等待 Bridge 返回执行结果后再退出，shell 的 `&&` 保证下一条命令在前一条成功后才执行。

**Q：传感器数据准吗？**

A：数据来自华为智慧生活 app 的 WebView 控件树，与 app 显示一致。读取方式是滑动暴露 → uiautomator dump → XML 解析，**不使用 OCR**。

**Q：手机必须一直亮屏吗？**

A：ADB 命令不需要手机亮屏，`input tap` 在息屏状态下也能执行。但如果 app 被杀或手机锁屏后 app 未运行，Bridge 会自动 `am start` 重新启动 app。

#!/usr/bin/env python3
"""传感器数据存储 — SQLite 封装。"""

import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "sensor.db"
JSONL_PATH = Path("/home/aidlux/iot-mqtt/sensor_history.jsonl")


def get_conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sensor_readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            co2 REAL,
            temperature REAL,
            humidity REAL,
            formaldehyde REAL,
            source TEXT DEFAULT 'manual'
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_readings(ts)
    """)
    conn.commit()
    conn.close()


def migrate_jsonl():
    """将现有的 JSONL 数据迁移到 SQLite（只跑一次）。"""
    if not JSONL_PATH.exists():
        return 0
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0]
    if count > 0:
        conn.close()
        return 0  # Already migrated

    migrated = 0
    with open(JSONL_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                data = record.get("data", {})
                conn.execute(
                    "INSERT INTO sensor_readings (ts, co2, temperature, humidity, formaldehyde, source) "
                    "VALUES (?, ?, ?, ?, ?, 'migrated')",
                    (
                        record.get("ts", ""),
                        data.get("co2_value"),
                        data.get("温度_value"),
                        data.get("湿度_value"),
                        data.get("甲醛", "").split()[0] if data.get("甲醛") else None,
                    ),
                )
                migrated += 1
            except (json.JSONDecodeError, KeyError):
                pass
    conn.commit()
    conn.close()
    print(f"Migrated {migrated} records from JSONL to SQLite")
    return migrated


def save_reading(data: dict, source="manual"):
    """保存一条传感器读数。data 格式来自 ADB Bridge 的返回。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO sensor_readings (ts, co2, temperature, humidity, formaldehyde, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            data.get("co2_value"),
            data.get("温度_value"),
            data.get("湿度_value"),
            data.get("甲醛", "").split()[0] if data.get("甲醛") else None,
            source,
        ),
    )
    conn.commit()
    conn.close()


def get_latest():
    """获取最新一条传感器数据。"""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM sensor_readings ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def get_history(limit=100):
    """获取历史数据，按时间正序。"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM sensor_readings ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    # Reverse to chronological order
    return [dict(r) for r in reversed(rows)]


def get_chart_data(limit=100):
    """为图表准备数据 — 按时间正序返回每个指标独立数组。"""
    rows = get_history(limit)
    return {
        "labels": [r["ts"][11:16] for r in rows],  # HH:MM
        "co2": [r["co2"] for r in rows],
        "temperature": [r["temperature"] for r in rows],
        "humidity": [r["humidity"] for r in rows],
        "formaldehyde": [r["formaldehyde"] for r in rows],
    }


# 启动时初始化
init_db()
migrate_jsonl()

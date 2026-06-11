#!/usr/bin/env python3
"""
萤石摄像头 MQTT Bridge — 截图 + YOLOv11 人体检测
==============================================
订阅 home/camera/ys/command, 支持:
  {"action": "capture"}            — 单张截图
  {"action": "detect"}             — 截图 + YOLOv11 人体检测
  {"action": "status"}             — 获取摄像头在线状态
  {"action": "capture_n", "count": 5}  — 连续截 N 张（每张间隔 ~2s）

发布到:
  home/camera/ys/snapshot   — 截图结果（图片路径 + 元信息）
  home/camera/ys/detection  — 检测结果（人体框 + 置信度）
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import paho.mqtt.client as mqtt

from config import (
    MQTT_HOST, MQTT_PORT,
    EVIZ_APP_KEY, EVIZ_APP_SECRET, EVIZ_ACCESS_TOKEN,
    EVIZ_BASE_URL, EVIZ_DEVICE_SERIAL,
    YOLO11_MODEL_PATH, YOLO11_SAVE_DIR,
    TOPIC_YS_COMMAND, TOPIC_YS_SNAPSHOT, TOPIC_YS_DETECTION,
)

logging.basicConfig(level=logging.INFO, format="[YS-Bridge] %(message)s",
                    stream=sys.stdout)
log = logging.getLogger("ys-bridge")
logging.getLogger().handlers[0].flush = lambda: sys.stdout.flush()


# ====================================================================
#  Ezviz API Client (from capture.py, stripped of interactive prompts)
# ====================================================================

class EzvizAPI:
    """Encapsulates all Ezviz Open API calls with auto token refresh."""

    def __init__(self):
        self.app_key = EVIZ_APP_KEY
        self.app_secret = EVIZ_APP_SECRET
        self.access_token = EVIZ_ACCESS_TOKEN
        self.base_url = EVIZ_BASE_URL
        self.device_serial = EVIZ_DEVICE_SERIAL
        self.save_dir = Path(YOLO11_SAVE_DIR)
        self.save_dir.mkdir(parents=True, exist_ok=True)

    # ── Token ──

    def refresh_token(self) -> str:
        resp = requests.post(
            f"{self.base_url}/token/get",
            data={"appKey": self.app_key, "appSecret": self.app_secret},
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == "200":
            self.access_token = data["data"]["accessToken"]
            log.info("AccessToken refreshed")
            return self.access_token
        raise RuntimeError(f"Token refresh failed: {data}")

    def api_call(self, endpoint: str, params: dict, retry=True) -> dict:
        params["accessToken"] = self.access_token
        try:
            resp = requests.post(f"{self.base_url}/{endpoint}",
                                 data=params, timeout=30)
            data = resp.json()
        except Exception as e:
            log.error(f"API call failed: {e}")
            data = {"code": "-1", "msg": str(e)}

        if retry and data.get("code") in ("10001", "10002", "10004"):
            log.warning(f"Token issue ({data.get('msg')}), refreshing...")
            self.refresh_token()
            return self.api_call(endpoint, params, retry=False)
        return data

    # ── Device ──

    def get_device_info(self) -> dict:
        data = self.api_call("device/info",
                             {"deviceSerial": self.device_serial})
        return data.get("data", {}) if data.get("code") == "200" else {}

    def get_device_status(self) -> str:
        info = self.get_device_info()
        status = info.get("status", -1)
        return {1: "online", 0: "offline", -1: "unknown"}.get(status,
                                                              f"unknown({status})")

    # ── Capture ──

    def capture_snapshot(self) -> bytes:
        data = self.api_call("device/capture", {
            "deviceSerial": self.device_serial, "channelNo": 1,
        })
        if data.get("code") != "200":
            raise RuntimeError(f"Capture failed: {data}")
        pic_url = data["data"].get("picUrl")
        img_resp = requests.get(pic_url, timeout=15)
        return img_resp.content

    def capture_and_save(self, label: str = "ys") -> dict:
        """Capture one snapshot and save to disk. Returns metadata dict."""
        img_data = self.capture_snapshot()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        filename = f"{label}_{ts}.jpg"
        filepath = self.save_dir / filename
        with open(filepath, "wb") as f:
            f.write(img_data)
        size_kb = len(img_data) / 1024
        log.info(f"Captured: {filename} ({size_kb:.0f} KB)")
        return {
            "filename": filename,
            "path": str(filepath),
            "size_kb": round(size_kb, 1),
            "timestamp": ts,
            "device": self.device_serial,
        }

    def capture_multiple(self, count: int = 3, label: str = "ys") -> list:
        """Capture N snapshots (~2s interval to avoid API rate limiting)."""
        results = []
        for i in range(count):
            try:
                meta = self.capture_and_save(label)
                results.append(meta)
                if i < count - 1:
                    time.sleep(2.5)
            except Exception as e:
                log.error(f"Capture #{i + 1} failed: {e}")
        return results


# ====================================================================
#  YOLOv11 Person Detector (AidLux QNN, optional)
# ====================================================================

try:
    import cv2
    import numpy as np
    import aidlite
    HAS_AIDLUX = True
except ImportError:
    HAS_AIDLUX = False
    log.warning("aidlite/cv2 not available → detection disabled")


class YOLOv11Detector:
    """YOLOv11n person detector using AidLite QNN (DSP)."""

    def __init__(self, model_path: str = None):
        if not HAS_AIDLUX:
            raise RuntimeError("AidLux env not available")
        self.model_path = model_path or YOLO11_MODEL_PATH
        self.input_size = 640
        self.conf_threshold = 0.5
        self.iou_threshold = 0.45
        self.class_num = 80
        self._init_model()
        log.info(f"YOLOv11 model loaded: {Path(self.model_path).name}")

    # ── AidLite init ──

    def _init_model(self):
        model = aidlite.Model.create_instance(self.model_path)
        if model is None:
            raise RuntimeError("Model.create_instance failed")
        model.set_model_properties(
            [[1, self.input_size, self.input_size, 3]],
            aidlite.DataType.TYPE_FLOAT32,
            [[1, 4, 8400], [1, 80, 8400]],  # split layout: boxes + classes
            aidlite.DataType.TYPE_FLOAT32,
        )
        config = aidlite.Config.create_instance()
        if config is None:
            raise RuntimeError("Config.create_instance failed")
        config.implement_type = aidlite.ImplementType.TYPE_LOCAL
        config.framework_type = aidlite.FrameworkType.TYPE_QNN
        config.accelerate_type = aidlite.AccelerateType.TYPE_DSP
        config.is_quantify_model = 1
        interpreter = aidlite.InterpreterBuilder.build_interpretper_from_model_and_config(
            model, config
        )
        if interpreter is None or interpreter.init() != 0 or interpreter.load_model() != 0:
            raise RuntimeError("Model init/load failed")
        self.interpreter = interpreter

    def __del__(self):
        it = getattr(self, "interpreter", None)
        if it:
            try:
                it.destory()
            except Exception:
                pass

    # ── Pre/Post processing ──

    @staticmethod
    def preprocess(image, input_size=640):
        h, w = image.shape[:2]
        length = max(h, w)
        scale = length / input_size
        canvas = np.zeros((length, length, 3), dtype=np.uint8)
        canvas[:h, :w] = image
        canvas = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        canvas = cv2.resize(canvas, (input_size, input_size),
                            interpolation=cv2.INTER_LINEAR)
        return (canvas.astype(np.float32) / 255.0)[None, :], scale

    @staticmethod
    def xywh2xyxy(boxes):
        r = boxes.copy()
        r[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
        r[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
        r[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
        r[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
        return r

    @staticmethod
    def nms(boxes, scores, iou_thres):
        if len(boxes) == 0:
            return []
        x1, y1, x2, y2 = boxes.T
        areas = np.maximum(0, x2 - x1) * np.maximum(0, y2 - y1)
        order = scores.argsort()[::-1]
        keep = []
        while order.size:
            i = int(order[0])
            keep.append(i)
            if order.size == 1:
                break
            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            iou = inter / np.maximum(areas[i] + areas[rest] - inter, 1e-12)
            order = rest[iou <= iou_thres]
        return keep

    def postprocess(self, output, orig_shape, scale):
        pred = np.asarray(output, dtype=np.float32)
        if pred.ndim == 3:
            pred = pred[0]
        if pred.shape[0] == 84:
            pred = pred.T
        if pred.shape[1] != 84:
            raise ValueError(f"Expected 84 channels, got {pred.shape}")

        obj_conf = pred[:, 4]
        cls_conf = pred[:, 5:85]
        cls_ids = cls_conf.argmax(axis=1)
        cls_scores = cls_conf[np.arange(cls_conf.shape[0]), cls_ids]
        scores = obj_conf * cls_scores
        mask = scores >= self.conf_threshold
        if not np.any(mask):
            return []

        boxes = self.xywh2xyxy(pred[mask, :4])
        boxes *= scale
        boxes[:, 0].clip(0, orig_shape[1], out=boxes[:, 0])
        boxes[:, 1].clip(0, orig_shape[0], out=boxes[:, 1])
        boxes[:, 2].clip(0, orig_shape[1], out=boxes[:, 2])
        boxes[:, 3].clip(0, orig_shape[0], out=boxes[:, 3])
        f_scores = scores[mask]
        f_ids = cls_ids[mask]

        results = []
        for cid in np.unique(f_ids):
            cm = f_ids == cid
            cb = boxes[cm]
            cs = f_scores[cm]
            keep = self.nms(cb, cs, self.iou_threshold)
            for k in keep:
                results.append({
                    "bbox": [int(cb[k][0]), int(cb[k][1]),
                             int(cb[k][2]), int(cb[k][3])],
                    "confidence": round(float(cs[k]), 4),
                    "class_id": int(cid),
                })
        return results

    def postprocess_split(self, boxes_out, cls_out, orig_shape, scale):
        """Process split layout output: boxes [1,4,8400] + classes [1,80,8400]."""
        boxes = np.asarray(boxes_out, dtype=np.float32)[0]  # [4, 8400]
        scores = np.asarray(cls_out, dtype=np.float32)[0]   # [80, 8400]

        # Transpose to [8400, 4] and [8400, 80]
        boxes = boxes.T  # [8400, 4]
        scores = scores.T  # [8400, 80]

        # Get max class score per anchor
        cls_ids = scores.argmax(axis=1)
        cls_scores = scores[np.arange(scores.shape[0]), cls_ids]

        mask = cls_scores >= self.conf_threshold
        if not np.any(mask):
            return []

        boxes_f = self.xywh2xyxy(boxes[mask])
        boxes_f *= scale
        boxes_f[:, 0].clip(0, orig_shape[1], out=boxes_f[:, 0])
        boxes_f[:, 1].clip(0, orig_shape[0], out=boxes_f[:, 1])
        boxes_f[:, 2].clip(0, orig_shape[1], out=boxes_f[:, 2])
        boxes_f[:, 3].clip(0, orig_shape[0], out=boxes_f[:, 3])
        f_scores = cls_scores[mask]
        f_ids = cls_ids[mask]

        results = []
        for cid in np.unique(f_ids):
            cm = f_ids == cid
            cb = boxes_f[cm]
            cs = f_scores[cm]
            keep = self.nms(cb, cs, self.iou_threshold)
            for k in keep:
                results.append({
                    "bbox": [int(cb[k][0]), int(cb[k][1]),
                             int(cb[k][2]), int(cb[k][3])],
                    "confidence": round(float(cs[k]), 4),
                    "class_id": int(cid),
                })
        return results

    # ── Public API ──

    def detect(self, image_path: str) -> dict:
        """Run detection on an image file. Returns structured result."""
        image = cv2.imread(image_path)
        if image is None:
            return {"error": f"cannot read {image_path}"}

        tensor, scale = self.preprocess(image)
        if self.interpreter.set_input_tensor(0, tensor.data) != 0:
            return {"error": "set_input_tensor failed"}
        if self.interpreter.invoke() != 0:
            return {"error": "invoke failed"}
        # Output 0: boxes [1, 4, 8400], Output 1: classes [1, 80, 8400]
        out_boxes = self.interpreter.get_output_tensor(0).reshape(1, 4, 8400)
        out_classes = self.interpreter.get_output_tensor(1).reshape(1, 80, 8400)
        dets = self.postprocess_split(out_boxes, out_classes, image.shape, scale)

        # Filter persons (class_id=0)
        persons = [d for d in dets if d["class_id"] == 0]

        # Draw boxes on image
        result_img = image.copy()
        for p in persons:
            x1, y1, x2, y2 = p["bbox"]
            conf = p["confidence"]
            cv2.rectangle(result_img, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"Person {conf:.2f}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX,
                                          0.6, 2)
            cv2.rectangle(result_img, (x1, y1 - th - 10),
                          (x1 + tw, y1), (0, 255, 0), -1)
            cv2.putText(result_img, label, (x1, y1 - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 2)

        # Save detection overlay image
        src = Path(image_path)
        out_path = str(src.parent / f"detected_{src.name}")
        cv2.imwrite(out_path, result_img)

        return {
            "image": src.name,
            "output_path": out_path,
            "total_detections": len(dets),
            "person_count": len(persons),
            "persons": persons,
        }


# ====================================================================
#  Capture + Detect pipeline
# ====================================================================

class YSPipeline:
    """High-level capture+detect interface for the MQTT bridge."""

    def __init__(self):
        self.ezviz = EzvizAPI()
        self._detector = None

    @property
    def detector(self):
        if self._detector is None and HAS_AIDLUX:
            try:
                self._detector = YOLOv11Detector()
            except Exception as e:
                log.warning(f"Detector init failed: {e}")
        return self._detector

    def handle_capture(self, payload: dict) -> dict:
        """Take a single snapshot and return metadata."""
        label = payload.get("label", "ys")
        return self.ezviz.capture_and_save(label)

    def handle_capture_n(self, payload: dict) -> dict:
        """Take multiple snapshots."""
        count = min(payload.get("count", 3), 10)
        label = payload.get("label", "ys")
        results = self.ezviz.capture_multiple(count, label)
        return {"captured": len(results), "results": results}

    def handle_detect(self, payload: dict) -> dict:
        """Capture + YOLOv11 detect in one shot."""
        label = payload.get("label", "ys")
        # Step 1: capture
        meta = self.ezviz.capture_and_save(label)
        if "error" in meta:
            return meta
        # Step 2: detect
        if self.detector is None:
            return {**meta, "detection": {"error": "detector not available"}}
        det_result = self.detector.detect(meta["path"])
        return {**meta, "detection": det_result}

    def handle_status(self) -> dict:
        """Get camera device status."""
        info = self.ezviz.get_device_info()
        return {
            "device_serial": self.ezviz.device_serial,
            "device_name": info.get("deviceName", "Unknown"),
            "status": self.ezviz.get_device_status(),
            "status_code": info.get("status", -1),
        }


# ====================================================================
#  MQTT Bridge
# ====================================================================

pipeline = YSPipeline()


def on_connect(client, userdata, flags, rc, properties=None):
    log.info(f"Connected to broker (rc={rc})")
    client.subscribe(TOPIC_YS_COMMAND, qos=0)


def on_message(client, userdata, msg):
    topic = msg.topic
    raw = msg.payload.decode("utf-8", errors="replace")
    log.info(f"← {topic}: {raw[:200]}")

    if topic != TOPIC_YS_COMMAND:
        return

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning(f"Invalid JSON: {raw[:100]}")
        return

    action = payload.get("action", "").lower()

    try:
        if action == "capture":
            result = pipeline.handle_capture(payload)
            pub_topic = TOPIC_YS_SNAPSHOT
        elif action == "capture_n":
            result = pipeline.handle_capture_n(payload)
            pub_topic = TOPIC_YS_SNAPSHOT
        elif action == "detect":
            result = pipeline.handle_detect(payload)
            pub_topic = TOPIC_YS_DETECTION
        elif action == "status":
            result = pipeline.handle_status()
            pub_topic = TOPIC_YS_SNAPSHOT
        else:
            result = {"error": f"unknown action: {action}"}
            pub_topic = TOPIC_YS_SNAPSHOT

        client.publish(pub_topic, json.dumps(result, ensure_ascii=False))
        log.info(f"→ {pub_topic}: {json.dumps(result, ensure_ascii=False)[:200]}")

    except Exception as e:
        import traceback
        err = {"error": str(e), "traceback": traceback.format_exc()}
        client.publish(TOPIC_YS_SNAPSHOT, json.dumps(err, ensure_ascii=False))
        log.error(f"Handler error: {e}")


def main():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                          client_id="ys-bridge")
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, 60)
    client.loop_start()

    log.info("YS Bridge started. Waiting for commands...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutting down...")
        client.loop_stop()


if __name__ == "__main__":
    main()

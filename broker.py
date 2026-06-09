#!/usr/bin/env python3
"""
Lightweight MQTT v3.1.1 Broker — pure Python, no external deps.
Supports: CONNECT, SUBSCRIBE (+/# wildcards), PUBLISH (QoS 0),
PINGREQ, DISCONNECT, keepalive.
"""

import asyncio
import struct
import logging
import sys
import time

logging.basicConfig(level=logging.DEBUG, format="[MQTT] %(message)s", force=True,
                    stream=sys.stdout)
log = logging.getLogger("broker")

# Ensure immediate flushing
logging.getLogger().handlers[0].flush = lambda: sys.stdout.flush()


# ── Packet helpers ────────────────────────────────────────────────────

PACKET_TYPES = {
    1: "CONNECT", 2: "CONNACK", 3: "PUBLISH", 4: "PUBACK",
    8: "SUBSCRIBE", 9: "SUBACK", 12: "PINGREQ", 13: "PINGRESP",
    14: "DISCONNECT",
}


async def _decode_remaining(reader):
    """Decode variable-length remaining length (up to 4 bytes)."""
    multiplier = 1
    value = 0
    for _ in range(4):
        byte = await reader.readexactly(1)
        b = byte[0]
        value += (b & 0x7F) * multiplier
        multiplier *= 0x80
        if not (b & 0x80):
            return value
    raise ValueError("Malformed remaining length")


def _encode_remaining(length):
    """Encode remaining length into 1-4 bytes."""
    buf = bytearray()
    while True:
        b = length % 0x80
        length //= 0x80
        if length > 0:
            b |= 0x80
        buf.append(b)
        if length == 0:
            break
    return bytes(buf)


def _read_string(data, offset):
    """Read a UTF-8 string (2-byte length prefix + data)."""
    slen = struct.unpack("!H", data[offset:offset + 2])[0]
    start = offset + 2
    return data[start:start + slen].decode("utf-8", errors="replace"), offset + 2 + slen


def _make_string(s):
    """Encode a string with 2-byte length prefix."""
    encoded = s.encode("utf-8")
    return struct.pack("!H", len(encoded)) + encoded


# ── Topic matching ────────────────────────────────────────────────────

def _match_topic(sub_topic, pub_topic):
    """Match a subscription topic (with +/#) against a published topic."""
    subs = sub_topic.split("/")
    pubs = pub_topic.split("/")
    i = 0
    for i, sub in enumerate(subs):
        if sub == "#":
            return True
        if i >= len(pubs):
            return False
        if sub == "+":
            continue
        if sub != pubs[i]:
            return False
    return i == len(pubs) - 1


# ── Broker ────────────────────────────────────────────────────────────

class MQTTBroker:
    def __init__(self, host="0.0.0.0", port=1883):
        self.host = host
        self.port = port
        self.subscribers = {}  # topic_filter → set of (writer, qos)
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        log.info(f"Broker listening on tcp://{self.host}:{self.port}")

    async def _handle_client(self, reader, writer):
        addr = writer.get_extra_info("peername")
        log.info(f"New client: {addr}")
        try:
            await self._handle_connection(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass
        finally:
            self._cleanup(writer)
            try:
                writer.close()
            except Exception:
                pass
            log.info(f"Disconnected: {addr}")

    def _cleanup(self, writer):
        for topic in list(self.subscribers.keys()):
            self.subscribers[topic] = {
                (w, q) for w, q in self.subscribers[topic] if w is not writer
            }
            if not self.subscribers[topic]:
                del self.subscribers[topic]

    async def _handle_connection(self, reader, writer):
        while True:
            header = await reader.readexactly(1)
            packet_type = header[0] >> 4
            flags = header[0] & 0x0F
            remaining = await _decode_remaining(reader)
            payload = await reader.readexactly(remaining) if remaining > 0 else b""

            ptype_name = PACKET_TYPES.get(packet_type, f"UNKNOWN({packet_type})")
            log.debug(f"← {ptype_name} ({remaining}B)")

            if packet_type == 1:  # CONNECT
                await self._handle_connect(writer, payload)
            elif packet_type == 3:  # PUBLISH
                await self._handle_publish(writer, payload, flags)
            elif packet_type == 8:  # SUBSCRIBE
                await self._handle_subscribe(writer, payload)
            elif packet_type == 12:  # PINGREQ
                await self._send_packet(writer, bytes([0xD0, 0x00]))
            elif packet_type == 14:  # DISCONNECT
                break
            # Ignore other types (PUBACK, etc.)

    async def _handle_connect(self, writer, payload):
        # Minimal CONNECT processing: just accept
        log.debug(f"CONNECT payload ({len(payload)}B)")
        await self._send_packet(writer, bytes([0x20, 0x02, 0x00, 0x00]))
        log.debug("CONNACK sent")

    async def _handle_subscribe(self, writer, payload):
        # Variable header: packet identifier (2 bytes)
        # Payload: pairs of (topic filter, QoS)
        packet_id = struct.unpack("!H", payload[:2])[0]
        offset = 2
        subscriptions = []
        while offset < len(payload):
            topic, offset = _read_string(payload, offset)
            qos = payload[offset]
            offset += 1
            subscriptions.append((topic, qos))
            self.subscribers.setdefault(topic, set()).add((writer, qos))
            log.info(f"SUB {topic} (QoS {qos})")

        # SUBACK: packet_id + return codes (one byte per subscription)
        return_codes = bytes([s[1] for s in subscriptions])
        suback = struct.pack("!H", packet_id) + return_codes
        await self._send_packet(writer, bytes([0x90, len(suback)]) + suback)

    async def _handle_publish(self, writer, payload, flags):
        qos = (flags >> 1) & 0x03
        retain = flags & 0x01

        if qos > 0:
            # Skip packet identifier (present for QoS 1/2)
            topic, offset = _read_string(payload, 0)
            msg_offset = offset + 2  # skip packet id
        else:
            topic, msg_offset = _read_string(payload, 0)

        message = payload[msg_offset:]

        # Log non-ping messages
        msg_str = message.decode("utf-8", errors="replace")[:200]
        log.info(f"PUB {topic} → {msg_str}")

        # Forward to matching subscribers
        for sub_topic, subs in list(self.subscribers.items()):
            relevant = [(w, q) for w, q in subs if w is not writer]
            if relevant and _match_topic(sub_topic, topic):
                for sub_writer, sub_qos in relevant:
                    try:
                        await self._send_publish(sub_writer, topic, message, qos=sub_qos)
                    except Exception:
                        pass

        # Send PUBACK for QoS 1
        if qos == 1:
            pkt_id = struct.unpack("!H", payload[offset:offset + 2])[0]
            puback = struct.pack("!H", pkt_id)
            await self._send_packet(writer, bytes([0x40, 0x02]) + puback)

    async def _send_publish(self, writer, topic, message, qos=0):
        topic_enc = _make_string(topic)
        remaining = len(topic_enc) + len(message)
        if qos > 0:
            remaining += 2  # packet identifier placeholder (0x0000)
            payload = topic_enc + struct.pack("!H", 0) + message
        else:
            payload = topic_enc + message

        # PUBLISH fixed header (byte 1 = 0x30 | (qos << 1))
        header = bytes([0x30 | (qos << 1)]) + _encode_remaining(remaining)
        await self._send_packet(writer, header + payload)

    async def _send_packet(self, writer, data):
        if writer.is_closing():
            return
        writer.write(data)
        await writer.drain()


async def main():
    broker = MQTTBroker(host="0.0.0.0", port=1883)
    await broker.start()
    log.info("Broker ready. Press Ctrl+C to stop.")
    try:
        await asyncio.Event().wait()  # run forever
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(main())

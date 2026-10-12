"""
Local test client for the SGSP salary-package voice agent's PHONE path (sgsp/app.py).

Simulates an Exotel outbound call using your mic/speakers — same 8 kHz base64
media frames Exotel sends — so you can test the phone flow without dialing.
(For the browser path, just open http://localhost:8004/sgsp/web.)

Run the server in one terminal:
    uvicorn sgsp.app:app --host 0.0.0.0 --port 8004 --reload

Then in another terminal:
    python test_sgsp_call.py [phone_number] [employee name] [male|female]

The agent greets you in Kannada and asks whether you are that employee. Press Ctrl+C to hang up.
"""

import asyncio
import base64
import json
import os
import queue
import sys

import numpy as np
import requests
import sounddevice as sd
import websockets

RATE         = 8000  # matches EXOTEL_RATE in sgsp/config.py
CHANNELS     = 1
CHUNK_MS     = 20
CHUNK_FRAMES = int(RATE * CHUNK_MS / 1000)  # 160 samples
BASE_URL     = "localhost:8004"
WS_URL       = f"ws://{BASE_URL}/sgsp/stream"

mic_q  = queue.Queue()
play_q = queue.Queue()
play_leftover = bytearray()  # bytes pulled from play_q but not yet consumed by a callback


def mic_callback(indata, frames, time_info, status):
    if status:
        print(f"[MIC] {status}", file=sys.stderr)
    mic_q.put(indata.tobytes())


def speaker_callback(outdata, frames, time_info, status):
    if status:
        print(f"[SPK] {status}", file=sys.stderr)
    needed = frames * 2  # int16 mono
    while len(play_leftover) < needed:
        try:
            play_leftover.extend(play_q.get_nowait())
        except queue.Empty:
            break
    chunk = bytes(play_leftover[:needed])
    del play_leftover[:needed]
    if len(chunk) < needed:
        chunk += bytes(needed - len(chunk))
    outdata[:] = np.frombuffer(chunk, dtype=np.int16).reshape(-1, 1)


async def mic_sender(ws):
    loop = asyncio.get_running_loop()
    while True:
        pcm = await loop.run_in_executor(None, mic_q.get)
        payload = base64.b64encode(pcm).decode()
        await ws.send(json.dumps({"event": "media", "media": {"payload": payload}}))


async def receiver(ws):
    async for raw in ws:
        msg   = json.loads(raw)
        event = msg.get("event")
        if event == "media":
            payload = msg.get("media", {}).get("payload", "")
            if payload:
                play_q.put(base64.b64decode(payload))
        elif event == "clear":
            play_leftover.clear()
            while not play_q.empty():
                try:
                    play_q.get_nowait()
                except queue.Empty:
                    break
        else:
            print(f"[WS] {msg}")


async def main():
    phone = sys.argv[1] if len(sys.argv) > 1 else "9999999999"
    name = sys.argv[2] if len(sys.argv) > 2 else "Ravi Kumar"
    gender = sys.argv[3] if len(sys.argv) > 3 else None

    # Register the call the same way the dispatcher does, without dialing Exotel.
    resp = requests.post(f"http://{BASE_URL}/sgsp/call", params={"dial": "false"},
                         json={"phone": phone, "name": name, "gender": gender},
                         headers={"X-Admin-Token": os.environ.get("SGSP_ADMIN_TOKEN", "")}, timeout=30)
    print(f"[TEST] /sgsp/call → {resp.status_code} {resp.json()}")
    if not resp.ok:
        return
    phone = resp.json()["phone"]

    async with websockets.connect(WS_URL) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({
            "event": "start",
            "stream_sid": "test-stream-1",
            "start": {
                "stream_sid": "test-stream-1",
                "call_sid": "test-call-1",
                "from": phone,
                "media_format": {"sample_rate": RATE, "bit_rate": 128},
            },
        }))
        print(f"[TEST] Connected as {name} ({phone}) — the agent will speak first. Ctrl+C to hang up.")

        with sd.InputStream(samplerate=RATE, channels=CHANNELS, dtype="int16",
                            blocksize=CHUNK_FRAMES, callback=mic_callback), \
             sd.OutputStream(samplerate=RATE, channels=CHANNELS, dtype="int16",
                             blocksize=CHUNK_FRAMES, callback=speaker_callback):
            try:
                await asyncio.gather(mic_sender(ws), receiver(ws))
            except websockets.ConnectionClosed:
                print("[TEST] Agent ended the call.")
            finally:
                try:
                    await ws.send(json.dumps({"event": "stop", "stop": {"reason": "test_client_hangup"}}))
                except websockets.ConnectionClosed:
                    pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[TEST] Call ended.")

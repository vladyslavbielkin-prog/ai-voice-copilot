import os
import json
import base64
import asyncio
from contextlib import AsyncExitStack

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from deepgram import AsyncDeepgramClient
from deepgram.listen.v1.types import ListenV1Results

app = FastAPI()


@app.get("/health")
def health():
    return JSONResponse({"ok": True})


@app.websocket("/ws")
async def twilio_ws(websocket: WebSocket):
    await websocket.accept()
    print("📞 Дзвінок розпочато", flush=True)

    dg_key = os.getenv("DEEPGRAM_API_KEY")
    if not dg_key:
        print("❌ DEEPGRAM_API_KEY not set", flush=True)
        await websocket.close(code=1011)
        return

    dg = AsyncDeepgramClient(api_key=dg_key)

    speaker_buffer: list[str] = []
    flush_task: asyncio.Task | None = None
    FLUSH_DELAY_SEC = 1.5

    async def flush_buffer():
        await asyncio.sleep(FLUSH_DELAY_SEC)
        if speaker_buffer:
            print(f"🗣 Спікер: {' '.join(speaker_buffer)}", flush=True)
            speaker_buffer.clear()

    async def read_transcripts(dg_socket, label: str):
        nonlocal flush_task
        async for message in dg_socket:
            if not isinstance(message, ListenV1Results):
                continue
            try:
                text = (message.channel.alternatives[0].transcript or "").strip()
                if not text or not message.is_final:
                    continue
                speaker_buffer.append(text)
                if message.speech_final:
                    if flush_task and not flush_task.done():
                        flush_task.cancel()
                    flush_task = asyncio.create_task(flush_buffer())
            except Exception as e:
                print(f"⚠️ [{label}] Помилка парсингу: {e}", flush=True)

    dg_params = dict(
        model="nova-2",
        language="uk",
        encoding="mulaw",
        sample_rate="8000",
        channels="1",
        interim_results="true",
        punctuate="true",
        smart_format="true",
    )

    try:
        async with AsyncExitStack() as stack:
            inbound_socket = await stack.enter_async_context(
                dg.listen.v1.connect(**dg_params)
            )
            outbound_socket = await stack.enter_async_context(
                dg.listen.v1.connect(**dg_params)
            )

            print("🧠 Deepgram підключено (x2)", flush=True)

            inbound_task = asyncio.create_task(read_transcripts(inbound_socket, "IN"))
            outbound_task = asyncio.create_task(read_transcripts(outbound_socket, "OUT"))

            try:
                while True:
                    msg = await websocket.receive_text()
                    data = json.loads(msg)
                    event = data.get("event")

                    if event == "media":
                        media = data.get("media", {})
                        payload_b64 = media.get("payload")
                        track = media.get("track", "inbound")
                        if payload_b64:
                            audio_bytes = base64.b64decode(payload_b64)
                            if track == "outbound":
                                await outbound_socket.send_media(audio_bytes)
                            else:
                                await inbound_socket.send_media(audio_bytes)

                    elif event == "stop":
                        print("📵 Дзвінок завершено", flush=True)
                        break

            except WebSocketDisconnect:
                print("📵 Дзвінок завершено", flush=True)

            finally:
                inbound_task.cancel()
                outbound_task.cancel()
                if flush_task and not flush_task.done():
                    flush_task.cancel()
                if speaker_buffer:
                    print(f"🗣 Спікер: {' '.join(speaker_buffer)}", flush=True)
                    speaker_buffer.clear()
                print("-" * 40, flush=True)

    except Exception as e:
        print(f"❌ Критична помилка: {e}", flush=True)

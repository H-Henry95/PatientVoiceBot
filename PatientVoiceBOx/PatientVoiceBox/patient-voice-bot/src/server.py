"""Voice bridge server.

Two responsibilities:

1. `/twiml`  — Twilio fetches this when a call connects. We return TwiML that
   opens a *bidirectional* Media Stream back to this server, and we pass the
   chosen scenario id through as a stream <Parameter>.

2. `/media`  — the Twilio Media Stream WebSocket. For each call we open a
   second WebSocket to the OpenAI Realtime API and pump audio both ways:

       Twilio (agent audio, mu-law) ──> OpenAI input_audio_buffer.append
       OpenAI (bot audio,  mu-law) ──> Twilio media frames

   Because both sides are configured for G.711 mu-law @ 8 kHz, audio passes
   through untouched — no resampling. We also:
     * handle barge-in: when the agent starts talking over the bot, we flush
       Twilio's playback buffer and cancel the bot's in-flight response;
     * record both directions to a stereo file;
     * capture a timestamped, speaker-labelled transcript from the Realtime
       events as the primary transcript for each call.

NOTE ON API VERSION: this targets the widely-deployed `gpt-4o-realtime-preview`
Beta schema (flat session fields + the `OpenAI-Beta: realtime=v1` header),
which is the same path Twilio's own quickstart uses. If you switch to the GA
`gpt-realtime` model, the session schema is nested under `audio` — adjust
`_session_update()` per the current OpenAI docs.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import websockets
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import HTMLResponse

from .audio import StereoRecorder, wav_to_mp3
from .config import settings
from .scenarios import get_scenario

app = FastAPI()

RECORDINGS_DIR = Path("recordings")
TRANSCRIPTS_DIR = Path("transcripts")

OPENAI_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.api_route("/twiml", methods=["GET", "POST"])
async def twiml(request: Request) -> HTMLResponse:
    """Return TwiML that connects the call to our media stream."""
    scenario = request.query_params.get("scenario", "schedule_simple")
    label = request.query_params.get("label", scenario)
    response = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="{settings.ws_url}">
      <Parameter name="scenario" value="{scenario}" />
      <Parameter name="label" value="{label}" />
    </Stream>
  </Connect>
</Response>"""
    return HTMLResponse(content=response, media_type="application/xml")


def _session_update(instructions: str) -> dict:
    """Realtime session config (Beta schema)."""
    return {
        "type": "session.update",
        "session": {
            "instructions": instructions,
            "voice": settings.REALTIME_VOICE,
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "modalities": ["audio", "text"],
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 600,
            },
        },
    }


class CallBridge:
    """Owns one call: the Twilio socket, the OpenAI socket, recording, transcript."""

    def __init__(self, twilio_ws: WebSocket):
        self.twilio_ws = twilio_ws
        self.openai_ws: websockets.WebSocketClientProtocol | None = None
        self.stream_sid: str | None = None
        self.scenario_id = "schedule_simple"
        self.label = "schedule_simple"
        self.start_time = time.time()
        self.recorder: StereoRecorder | None = None
        self.transcript: list[dict] = []  # {t, speaker, text}
        self._stop = asyncio.Event()

    # ---- helpers ----------------------------------------------------------
    def _elapsed(self) -> float:
        return time.time() - self.start_time

    def _log_line(self, speaker: str, text: str) -> None:
        text = (text or "").strip()
        if text:
            self.transcript.append({"t": round(self._elapsed(), 1), "speaker": speaker, "text": text})

    async def _send_twilio(self, payload: dict) -> None:
        await self.twilio_ws.send_text(json.dumps(payload))

    # ---- Twilio -> OpenAI -------------------------------------------------
    async def pump_twilio_to_openai(self) -> None:
        try:
            while not self._stop.is_set():
                raw = await self.twilio_ws.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "start":
                    self.stream_sid = data["start"]["streamSid"]
                    params = data["start"].get("customParameters", {}) or {}
                    self.scenario_id = params.get("scenario", self.scenario_id)
                    self.label = params.get("label", self.scenario_id)
                    self.start_time = time.time()
                    self.recorder = StereoRecorder(self.start_time)
                    await self._configure_openai()

                elif event == "media":
                    payload = data["media"]["payload"]  # base64 mu-law
                    if self.recorder is not None:
                        self.recorder.add_agent(self._elapsed(), base64.b64decode(payload))
                    if self.openai_ws is not None:
                        await self.openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": payload,
                        }))

                elif event == "stop":
                    break
        except Exception:
            pass
        finally:
            self._stop.set()

    # ---- OpenAI -> Twilio -------------------------------------------------
    async def pump_openai_to_twilio(self) -> None:
        assert self.openai_ws is not None
        try:
            async for raw in self.openai_ws:
                if self._stop.is_set():
                    break
                event = json.loads(raw)
                etype = event.get("type")

                if etype == "response.audio.delta" and event.get("delta"):
                    delta = event["delta"]  # base64 mu-law
                    if self.recorder is not None:
                        self.recorder.add_bot(self._elapsed(), base64.b64decode(delta))
                    if self.stream_sid:
                        await self._send_twilio({
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": {"payload": delta},
                        })

                elif etype == "input_audio_buffer.speech_started":
                    # The agent started talking -> barge-in. Flush whatever the
                    # bot was saying and cancel the in-flight response.
                    if self.stream_sid:
                        await self._send_twilio({"event": "clear", "streamSid": self.stream_sid})
                    await self.openai_ws.send(json.dumps({"type": "response.cancel"}))

                elif etype == "response.audio_transcript.done":
                    self._log_line("PATIENT_BOT", event.get("transcript", ""))

                elif etype == "conversation.item.input_audio_transcription.completed":
                    self._log_line("AGENT", event.get("transcript", ""))

                elif etype == "error":
                    self._log_line("SYSTEM_ERROR", json.dumps(event.get("error", {})))
        except Exception:
            pass
        finally:
            self._stop.set()

    # ---- watchdog ---------------------------------------------------------
    async def watchdog(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=settings.MAX_CALL_SECONDS)
        except asyncio.TimeoutError:
            self._stop.set()

    async def _configure_openai(self) -> None:
        scenario = get_scenario(self.scenario_id)
        await self.openai_ws.send(json.dumps(_session_update(scenario.instructions)))

    # ---- lifecycle --------------------------------------------------------
    async def run(self) -> None:
        await self.twilio_ws.accept()
        headers = {
            "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1",
        }
        url = OPENAI_WS_URL.format(model=settings.OPENAI_REALTIME_MODEL)
        # `additional_headers` (websockets >=13) vs `extra_headers` (<=12).
        try:
            connect_cm = websockets.connect(url, additional_headers=headers, max_size=None)
        except TypeError:
            connect_cm = websockets.connect(url, extra_headers=headers, max_size=None)
        async with connect_cm as openai_ws:
            self.openai_ws = openai_ws
            await asyncio.gather(
                self.pump_twilio_to_openai(),
                self.pump_openai_to_twilio(),
                self.watchdog(),
            )
        self._finalize()

    def _finalize(self) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.start_time))
        base = f"{stamp}_{self.scenario_id}"

        # transcript (primary, from Realtime events)
        TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        lines = [f"# Call: {self.label} ({self.scenario_id})", f"# Recorded: {stamp}", ""]
        for row in sorted(self.transcript, key=lambda r: r["t"]):
            mm, ss = divmod(int(row["t"]), 60)
            lines.append(f"[{mm:02d}:{ss:02d}] {row['speaker']}: {row['text']}")
        (TRANSCRIPTS_DIR / f"{base}.txt").write_text("\n".join(lines), encoding="utf-8")
        (TRANSCRIPTS_DIR / f"{base}.json").write_text(
            json.dumps({"scenario": self.scenario_id, "label": self.label, "turns": self.transcript}, indent=2),
            encoding="utf-8",
        )

        # recording -> wav -> mp3
        if self.recorder is not None:
            try:
                wav = self.recorder.write_wav(RECORDINGS_DIR / f"{base}.wav")
                try:
                    wav_to_mp3(wav, RECORDINGS_DIR / f"{base}.mp3")
                    wav.unlink(missing_ok=True)  # keep only the mp3
                except Exception:
                    pass  # ffmpeg missing: leave the wav so nothing is lost
            except Exception:
                pass


@app.get("/demo")
async def demo_page() -> HTMLResponse:
    """Display the demo showcase as an HTML page."""
    transcripts_dir = Path("transcripts")
    json_files = sorted(transcripts_dir.glob("*.json"))
    
    calls_html = ""
    for idx, json_file in enumerate(json_files, 1):
        try:
            with open(json_file) as f:
                data = json.load(f)
            scenario = data.get("scenario", "unknown")
            label = data.get("label", "Unknown")
            turns = data.get("turns", [])
            
            if turns:
                start_time = turns[0]["t"]
                end_time = turns[-1]["t"]
                duration = end_time - start_time
            else:
                duration = 0
            
            first = turns[0] if turns else {}
            last = turns[-1] if turns else {}
            
            calls_html += f"""
            <div class="call-card">
                <h3>🎬 Call {idx}: {label}</h3>
                <p><strong>Scenario:</strong> {scenario}</p>
                <p><strong>Duration:</strong> ~{duration:.1f} seconds</p>
                <p><strong>Exchanges:</strong> {len(turns)} turns</p>
                <details>
                    <summary>View Full Transcript</summary>
                    <pre><code>"""
            
            for turn in turns:
                calls_html += f"[{turn['t']:.1f}s] {turn['speaker']}: {turn['text']}\n"
            
            calls_html += """</code></pre>
                </details>
            </div>
            """
        except Exception:
            pass
    
    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Patient Voice Bot - Live Demo</title>
    <style>
        * {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        body {{ 
            max-width: 1200px; 
            margin: 0 auto; 
            padding: 20px; 
            background: #f5f5f5;
            color: #333;
        }}
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 40px;
            border-radius: 10px;
            text-align: center;
            margin-bottom: 30px;
        }}
        .header h1 {{ margin: 0; font-size: 2.5em; }}
        .header p {{ margin: 10px 0 0 0; opacity: 0.9; }}
        .status {{
            background: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 30px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .status-row {{
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
        }}
        .status-row:last-child {{ border: none; }}
        .status-label {{ font-weight: bold; }}
        .status-value {{ color: #667eea; font-weight: bold; }}
        .call-card {{
            background: white;
            padding: 20px;
            margin-bottom: 20px;
            border-radius: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            border-left: 4px solid #667eea;
        }}
        .call-card h3 {{ margin-top: 0; color: #667eea; }}
        .call-card p {{ margin: 5px 0; }}
        .call-card strong {{ color: #333; }}
        details {{
            margin-top: 15px;
            cursor: pointer;
        }}
        details summary {{
            color: #667eea;
            font-weight: bold;
            padding: 10px;
            background: #f9f9f9;
            border-radius: 5px;
            user-select: none;
        }}
        details summary:hover {{ background: #f0f0f0; }}
        details pre {{
            background: #f9f9f9;
            padding: 15px;
            border-radius: 5px;
            overflow-x: auto;
            font-size: 0.9em;
            margin: 10px 0 0 0;
        }}
        .footer {{
            text-align: center;
            padding: 20px;
            color: #666;
            font-size: 0.9em;
        }}
        .badge {{
            display: inline-block;
            padding: 5px 10px;
            background: #667eea;
            color: white;
            border-radius: 20px;
            font-size: 0.85em;
            margin-right: 10px;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 Patient Voice Bot</h1>
        <p>Live Demo Showcase - Real-Time Test Call Results</p>
    </div>
    
    <div class="status">
        <h2>📊 System Status</h2>
        <div class="status-row">
            <span class="status-label">✅ Server Status:</span>
            <span class="status-value">OPERATIONAL</span>
        </div>
        <div class="status-row">
            <span class="status-label">📍 Live Server:</span>
            <span class="status-value">http://0.0.0.0:5050</span>
        </div>
        <div class="status-row">
            <span class="status-label">🌐 Public URL:</span>
            <span class="status-value">https://strum-probation-duplicity.ngrok-free.dev</span>
        </div>
        <div class="status-row">
            <span class="status-label">📞 Completed Calls:</span>
            <span class="status-value">{len(json_files)}</span>
        </div>
        <div class="status-row">
            <span class="status-label">📝 Transcripts:</span>
            <span class="status-value">{len(json_files)} JSON + {len(json_files)} TXT</span>
        </div>
    </div>
    
    <h2>📞 Completed Test Calls</h2>
    {calls_html}
    
    <div class="status" style="background: #f0f7ff; border-left: 4px solid #667eea;">
        <h3>🏗️ Architecture</h3>
        <p><strong>FastAPI Server</strong> (Port 5050)</p>
        <ul>
            <li>WebSocket endpoint for Twilio Media Stream</li>
            <li>Realtime conversation bridge</li>
        </ul>
        <p><strong>Twilio Integration</strong></p>
        <ul>
            <li>Outbound call placement</li>
            <li>Media streaming (mu-law, 8 kHz)</li>
            <li>Webhook callbacks</li>
        </ul>
        <p><strong>OpenAI Realtime API</strong></p>
        <ul>
            <li>Patient voice synthesis</li>
            <li>Live conversation transcription</li>
            <li>Speaker labeling</li>
        </ul>
        <p><strong>Recording & Analysis</strong></p>
        <ul>
            <li>Stereo MP3 (agent=L, bot=R)</li>
            <li>Timestamped JSON transcripts</li>
            <li>Automated bug detection</li>
        </ul>
    </div>
    
    <div class="footer">
        <p>🚀 Patient Voice Bot - Ready for Production</p>
        <p>Last Updated: {time.strftime("%Y-%m-%d %H:%M:%S")}</p>
    </div>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.websocket("/media")
async def media(ws: WebSocket) -> None:
    bridge = CallBridge(ws)
    await bridge.run()

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response, JSONResponse
import requests
import os
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from elevenlabs.conversational_ai.default_audio_interface import DefaultAudioInterface
from dotenv import load_dotenv
import asyncio
import websockets
import json
from pydantic import BaseModel
import logging
from typing import Optional
from twilio.rest import Client
import base64

logging.basicConfig(level=logging.INFO)

app = FastAPI()

# Load environment variables from .env
load_dotenv()

ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ZENDESK_SUBDOMAIN = os.getenv("ZENDESK_SUBDOMAIN")

elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# Pydantic model for tool input
class OrderStatusInput(BaseModel):
    order_id: str

# Remove /tools/get_order_status and add /tools/zendesk_lookup
class ZendeskLookupInput(BaseModel):
    email: str

# Outgoing call input model
class OutgoingCallInput(BaseModel):
    to: str  # Destination phone number
    from_: Optional[str] = None  # Twilio phone number (optional, fallback to env)

# --- Twilio Webhook: Returns TwiML to start audio stream ---
@app.post("/voice")
def voice_response():
    TWIML_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="wss://voice-agent-nacv.onrender.com/audio" />
  </Start>
  <Say>Connecting you to our voice assistant...</Say>
</Response>'''
    return Response(content=TWIML_XML, media_type="text/xml")

# --- Tool Call Handler (for ElevenLabs tool calls) ---
@app.post("/tools/get_order_status")
async def order_status_handler(input: OrderStatusInput):
    return {"result": f"Order #{input.order_id} will arrive tomorrow."}

# --- Zendesk: Log internal note ---
def log_note_to_zendesk(ticket_id, message):
    ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
    ZENDESK_API_KEY = os.getenv("ZENDESK_API_KEY")
    if not ZENDESK_SUBDOMAIN:
        raise Exception("ZENDESK_SUBDOMAIN not set in environment.")
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/tickets/{ticket_id}.json"
    data = {
        "ticket": {
            "comment": {
                "body": message,
                "public": False
            }
        }
    }
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_KEY)
    r = requests.put(url, json=data, auth=auth)
    return r.status_code, r.text

# --- Slack: Send alert ---
def send_slack_alert(message):
    SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
    if SLACK_WEBHOOK_URL:
        r = requests.post(SLACK_WEBHOOK_URL, json={"text": message})
        return r.status_code, r.text
    return None, "No webhook URL set"

@app.post("/tools/log_note")
async def log_note(request: Request):
    body = await request.json()
    ticket_id = body.get('tool_input', {}).get('ticket_id')
    message = body.get('tool_input', {}).get('message')
    status, resp = log_note_to_zendesk(ticket_id, message)
    return {"status": status, "response": resp}

@app.post("/tools/slack_alert")
async def slack_alert(request: Request):
    body = await request.json()
    message = body.get('tool_input', {}).get('message')
    status, resp = send_slack_alert(message)
    return {"status": status, "response": resp}

# --- Health check ---
@app.get("/")
def root():
    return {"status": "ok"}

@app.websocket("/elevenlabs/ws")
async def elevenlabs_ws(websocket: WebSocket):
    await websocket.accept()
    try:
        conversation = Conversation(
            elevenlabs_client,
            ELEVENLABS_AGENT_ID,
            requires_auth=bool(ELEVENLABS_API_KEY),
            audio_interface=DefaultAudioInterface(),
            callback_agent_response=lambda response: asyncio.create_task(websocket.send_text(f"Agent: {response}")),
            callback_agent_response_correction=lambda original, corrected: asyncio.create_task(websocket.send_text(f"Agent: {original} -> {corrected}")),
            callback_user_transcript=lambda transcript: asyncio.create_task(websocket.send_text(f"User: {transcript}")),
        )
        conversation.start_session()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.error(f"/elevenlabs/ws error: {e}")
        await websocket.send_text("An internal error occurred.")
    finally:
        await websocket.close()

@app.websocket("/audio")
async def audio_ws(websocket: WebSocket):
    logging.basicConfig(level=logging.INFO)
    logging.info("WebSocket /audio: connection attempt")
    await websocket.accept()
    logging.info("WebSocket /audio: connection accepted")
    elevenlabs_ws_url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY} if ELEVENLABS_API_KEY else {}
    try:
        logging.info("Connecting to ElevenLabs WebSocket...")
        async with websockets.connect(elevenlabs_ws_url, extra_headers=headers) as el_ws:
            logging.info("Connected to ElevenLabs WebSocket.")
            await el_ws.send(json.dumps({
                "audio_start": {"type": "twilio", "encoding": "mulaw", "sample_rate": 8000}
            }))
            async def twilio_to_elevenlabs():
                logging.info("Listening for audio from Twilio")
                while True:
                    try:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.receive":
                            if "bytes" in msg:
                                data = msg["bytes"]
                                logging.info(f"Received {len(data)} bytes from Twilio")
                                await el_ws.send(data)
                            elif "text" in msg:
                                logging.info(f"Received text from Twilio: {msg['text']}")
                        elif msg["type"] == "websocket.disconnect":
                            logging.info("Twilio WebSocket disconnected")
                            break
                    except Exception as e:
                        logging.error(f"Error in twilio_to_elevenlabs: {e}")
                        break
            async def elevenlabs_to_twilio():
                logging.info("Waiting for ElevenLabs audio response")
                while True:
                    try:
                        msg = await el_ws.recv()
                        logging.info(f"Received message from ElevenLabs: {type(msg)}")
                        if isinstance(msg, bytes):
                            logging.info(f"Forwarding {len(msg)} bytes of audio to Twilio")
                            await websocket.send_bytes(msg)
                        else:
                            try:
                                event = json.loads(msg)
                                if event.get("type") == "audio" and "audio_event" in event:
                                    audio_b64 = event["audio_event"]["audio_base_64"]
                                    audio_bytes = base64.b64decode(audio_b64)
                                    logging.info(f"Forwarding {len(audio_bytes)} bytes of decoded audio to Twilio")
                                    await websocket.send_bytes(audio_bytes)
                                elif event.get("type") == "agent_response" and "agent_response_event" in event:
                                    logging.info(f"Agent response: {event['agent_response_event']['agent_response']}")
                                else:
                                    pass
                            except Exception:
                                logging.warning("Could not parse non-audio ElevenLabs message.")
                    except Exception as e:
                        logging.error(f"Error in elevenlabs_to_twilio: {e}")
                        break
            await asyncio.gather(twilio_to_elevenlabs(), elevenlabs_to_twilio())
    except Exception as e:
        logging.error(f"WebSocket /audio: error: {e}")
    finally:
        if not websocket.client_state.name == "DISCONNECTED":
            await websocket.close()
        logging.info("WebSocket /audio: connection closed")

@app.post("/tools/zendesk_lookup")
async def zendesk_lookup(input: ZendeskLookupInput):
    ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
    ZENDESK_API_KEY = os.getenv("ZENDESK_API_KEY")
    if not ZENDESK_SUBDOMAIN:
        return {"result": "ZENDESK_SUBDOMAIN not set in environment."}
    url = f"https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2/search.json?query=type:user+email:{input.email}"
    auth = (f"{ZENDESK_EMAIL}/token", ZENDESK_API_KEY)
    response = requests.get(url, auth=auth)
    if response.status_code == 200:
        data = response.json()
        if data.get("results"):
            user = data["results"][0]
            name = user.get("name")
            user_id = user.get("id")
            org = user.get("organization_id")
            return {
                "result": f"Customer found: {name}, ID: {user_id}, Org: {org}."
            }
        else:
            return {"result": "No customer found with that email."}
    else:
        return {"result": f"Zendesk error: {response.status_code} - {response.text}"}

@app.post("/twilio/outgoing_call")
async def outgoing_call(input: OutgoingCallInput):
    TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
    TWILIO_API_KEY = os.getenv("TWILIO_API_KEY")
    TWILIO_API_SECRET = os.getenv("TWILIO_API_SECRET")
    TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
    if not input.from_:
        input.from_ = TWILIO_PHONE_NUMBER
    if not TWILIO_ACCOUNT_SID or not input.from_:
        raise HTTPException(status_code=500, detail="Twilio Account SID or phone number not set.")
    from twilio.rest import Client
    # Prefer API Key/Secret if present, else use Auth Token
    if TWILIO_API_KEY and TWILIO_API_SECRET:
        client = Client(TWILIO_API_KEY, TWILIO_API_SECRET, account_sid=TWILIO_ACCOUNT_SID)
    elif TWILIO_AUTH_TOKEN:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    else:
        raise HTTPException(status_code=500, detail="Twilio credentials not set (need API Key/Secret or Auth Token).")
    VOICE_WEBHOOK_URL = os.getenv("VOICE_WEBHOOK_URL")
    if not VOICE_WEBHOOK_URL:
        raise HTTPException(status_code=500, detail="VOICE_WEBHOOK_URL not set in environment.")
    try:
        call = client.calls.create(
            to=input.to,
            from_=input.from_,
            url=VOICE_WEBHOOK_URL
        )
        return {"result": f"Call initiated", "call_sid": call.sid}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Twilio call failed: {str(e)}")

@app.get("/test")
def test():
    print("Test route called")
    return {"status": "ok"}

print("Registered routes:", [route.path for route in app.routes]) 
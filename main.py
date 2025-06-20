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

app = FastAPI()

# Load environment variables from .env
load_dotenv()

ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")

elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

# Set up basic logging
logging.basicConfig(level=logging.INFO)

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
    TWIML_XML = '''<?xml version="1.0" encoding="UTF-8"?>\n<Response>\n  <Start>\n    <Stream url=\"wss://your-backend.com/audio\" />\n  </Start>\n  <Say>Connecting you to our voice assistant...</Say>\n</Response>'''
    return Response(content=TWIML_XML, media_type="text/xml")

# --- Tool Call Handler (for ElevenLabs tool calls) ---
@app.post("/tools/get_order_status")
async def order_status_handler(input: OrderStatusInput):
    return {"result": f"Order #{input.order_id} will arrive tomorrow."}

# --- Zendesk: Log internal note ---
def log_note_to_zendesk(ticket_id, message):
    ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
    ZENDESK_API_KEY = os.getenv("ZENDESK_API_KEY")
    url = f"https://yourcompany.zendesk.com/api/v2/tickets/{ticket_id}.json"
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
    logging.info("Twilio is attempting to connect to /audio")
    await websocket.accept()
    logging.info("Twilio WebSocket connection accepted at /audio")
    elevenlabs_ws_url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY} if ELEVENLABS_API_KEY else {}
    try:
        async with websockets.connect(elevenlabs_ws_url, extra_headers=headers) as el_ws:
            await el_ws.send(json.dumps({
                "audio_start": {"type": "twilio", "encoding": "mulaw", "sample_rate": 8000}
            }))
            last_audio_time = asyncio.get_event_loop().time()
            async def twilio_to_elevenlabs():
                logging.info("Listening for audio from Twilio")
                nonlocal last_audio_time
                while True:
                    try:
                        data = await asyncio.wait_for(websocket.receive_bytes(), timeout=60)
                        last_audio_time = asyncio.get_event_loop().time()
                        await el_ws.send(data)
                    except asyncio.TimeoutError:
                        logging.info("No audio received from Twilio for 60 seconds. Closing connection.")
                        await websocket.close()
                        break
            async def elevenlabs_to_twilio():
                logging.info("Waiting for ElevenLabs audio response")
                while True:
                    msg = await el_ws.recv()
                    # Handle bytes or str
                    if isinstance(msg, bytes):
                        try:
                            event = json.loads(msg.decode())
                            # Logging for dev/testing
                            if "transcript" in event:
                                logging.info(f"User transcript: {event['transcript']['text']}")
                            if "tool_call" in event:
                                logging.info(f"Tool called: {event['tool_call']['name']} with {event['tool_call']['parameters']}")
                            if "response" in event:
                                logging.info(f"Agent response: {event['response']}")
                        except Exception:
                            # Not JSON, assume audio
                            await websocket.send_bytes(msg)
                    else:
                        try:
                            event = json.loads(msg)
                            # Logging for dev/testing
                            if "transcript" in event:
                                logging.info(f"User transcript: {event['transcript']['text']}")
                            if "tool_call" in event:
                                logging.info(f"Tool called: {event['tool_call']['name']} with {event['tool_call']['parameters']}")
                            if "response" in event:
                                logging.info(f"Agent response: {event['response']}")
                        except Exception:
                            pass
            await asyncio.gather(twilio_to_elevenlabs(), elevenlabs_to_twilio())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logging.error(f"/audio error: {e}")
        await websocket.send_text("An internal error occurred.")
    finally:
        await websocket.close()

@app.post("/tools/zendesk_lookup")
async def zendesk_lookup(input: ZendeskLookupInput):
    ZENDESK_EMAIL = os.getenv("ZENDESK_EMAIL")
    ZENDESK_API_KEY = os.getenv("ZENDESK_API_KEY")
    url = f"https://yourcompany.zendesk.com/api/v2/search.json?query=type:user+email:{input.email}"
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
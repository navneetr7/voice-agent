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
  <Say voice="alice" rate="medium">Please hold while we connect you to our AI assistant.</Say>
  <Start>
    <Stream url="wss://voice-agent-nacv.onrender.com/audio" />
  </Start>
  <Pause length="60"/>
  <Say voice="alice">Thank you for calling. Goodbye.</Say>
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
    logging.info("WebSocket /audio: connection attempt")
    await websocket.accept()
    logging.info("WebSocket /audio: connection accepted")
    
    # Track connection state
    stream_sid = None
    is_active = True
    received_real_audio = False
    
    elevenlabs_ws_url = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    headers = {"xi-api-key": ELEVENLABS_API_KEY} if ELEVENLABS_API_KEY else {}
    
    # MuLaw silence chunk (20ms at 8kHz)
    SILENCE_CHUNK = b'\xff' * 160
    
    async def send_silence_to_twilio():
        """Send silence in Twilio's expected JSON format"""
        nonlocal stream_sid, received_real_audio
        logging.info("Starting silence padding")
        
        while is_active and not received_real_audio:
            try:
                if stream_sid:
                    silence_b64 = base64.b64encode(SILENCE_CHUNK).decode('utf-8')
                    silence_msg = {
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": {"payload": silence_b64}
                    }
                    await websocket.send_text(json.dumps(silence_msg))
                    logging.debug("Sent silence chunk")
                await asyncio.sleep(0.2)  # Every 200ms
            except Exception as e:
                logging.error(f"Error sending silence: {e}")
                break
        
        logging.info("Silence padding stopped")
    
    try:
        logging.info("Connecting to ElevenLabs WebSocket...")
        async with websockets.connect(elevenlabs_ws_url, extra_headers=headers) as el_ws:
            logging.info("Connected to ElevenLabs WebSocket.")
            
            async def twilio_to_elevenlabs():
                nonlocal stream_sid, is_active
                logging.info("Starting Twilio message handler")
                
                try:
                    while is_active:
                        message = await websocket.receive_text()
                        data = json.loads(message)
                        
                        if data.get("event") == "start":
                            stream_sid = data.get("streamSid")
                            logging.info(f"Twilio stream started: {stream_sid}")
                            
                            # Initialize ElevenLabs conversation
                            await el_ws.send(json.dumps({
                                "type": "conversation_initiation_client_data",
                                "conversation_initiation_client_data": {
                                    "custom_llm_extra_body": {}
                                }
                            }))
                            
                        elif data.get("event") == "media" and stream_sid:
                            # Forward audio to ElevenLabs
                            payload = data["media"]["payload"]
                            await el_ws.send(json.dumps({
                                "type": "audio_stream_input", 
                                "audio_stream_input": {
                                    "audio_base_64": payload
                                }
                            }))
                            logging.debug("Forwarded audio to ElevenLabs")
                            
                        elif data.get("event") == "stop":
                            logging.info("Twilio stream stopped")
                            is_active = False
                            break
                            
                except websockets.exceptions.ConnectionClosed:
                    logging.info("Twilio connection closed")
                    is_active = False
                except Exception as e:
                    logging.error(f"Error in twilio_to_elevenlabs: {e}")
                    is_active = False
            
            async def elevenlabs_to_twilio():
                nonlocal received_real_audio, is_active
                logging.info("Starting ElevenLabs message handler")
                
                try:
                    while is_active:
                        message = await el_ws.recv()
                        
                        if isinstance(message, str):
                            try:
                                data = json.loads(message)
                                
                                if data.get("type") == "audio_stream_output":
                                    # Got real audio from ElevenLabs
                                    audio_b64 = data.get("audio_stream_output", {}).get("audio_base_64")
                                    if audio_b64 and stream_sid:
                                        received_real_audio = True  # Stop silence padding
                                        
                                        audio_msg = {
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {"payload": audio_b64}
                                        }
                                        await websocket.send_text(json.dumps(audio_msg))
                                        logging.info(f"Sent real audio to Twilio ({len(audio_b64)} chars)")
                                
                                elif data.get("type") == "agent_response":
                                    response_text = data.get("agent_response", {}).get("agent_response_text", "")
                                    logging.info(f"Agent response: {response_text}")
                                
                                elif data.get("type") == "conversation_ended":
                                    logging.info("ElevenLabs conversation ended")
                                    is_active = False
                                    break
                                    
                            except json.JSONDecodeError:
                                logging.warning("Could not parse ElevenLabs message")
                                
                        elif isinstance(message, bytes):
                            # Handle raw audio bytes (fallback)
                            if stream_sid:
                                received_real_audio = True
                                audio_b64 = base64.b64encode(message).decode('utf-8')
                                audio_msg = {
                                    "event": "media", 
                                    "streamSid": stream_sid,
                                    "media": {"payload": audio_b64}
                                }
                                await websocket.send_text(json.dumps(audio_msg))
                                logging.info(f"Sent raw audio to Twilio ({len(message)} bytes)")
                                
                except websockets.exceptions.ConnectionClosed:
                    logging.info("ElevenLabs connection closed")
                    is_active = False
                except Exception as e:
                    logging.error(f"Error in elevenlabs_to_twilio: {e}")
                    is_active = False
            
            # Run all tasks concurrently
            await asyncio.gather(
                twilio_to_elevenlabs(),
                elevenlabs_to_twilio(), 
                send_silence_to_twilio(),
                return_exceptions=True
            )
            
    except Exception as e:
        logging.error(f"WebSocket /audio error: {e}")
    finally:
        is_active = False
        if websocket.client_state.name != "DISCONNECTED":
            try:
                await websocket.close()
            except:
                pass
        logging.info("WebSocket /audio: connection closed")

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

@app.get("/test")
def test():
    print("Test route called")
    return {"status": "ok"}

print("Registered routes:", [route.path for route in app.routes]) 
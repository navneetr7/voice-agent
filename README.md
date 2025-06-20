# Voice Agent Backend

This backend integrates Twilio, ElevenLabs, Zendesk, and Slack for a conversational AI voice agent.

## Features
- Receives Twilio voice streams and relays to ElevenLabs Conversational AI
- Handles ElevenLabs tool calls (e.g., order status lookup)
- Logs internal notes to Zendesk
- Sends escalation alerts to Slack

## Setup

1. **Clone the repo and navigate to the project directory:**
   ```sh
   cd Voice\ Agent
   ```
2. **Create and activate a virtual environment:**
   ```sh
   python -m venv venv
   venv\Scripts\activate  # On Windows
   ```
3. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```
4. **Set environment variables:**
   - `ZENDESK_EMAIL`: Your Zendesk email
   - `ZENDESK_API_KEY`: Your Zendesk API key
   - `SLACK_WEBHOOK_URL`: Your Slack webhook URL

   You can copy `.env.example` to `.env` and fill in your values.

## Running the Server

```sh
uvicorn main:app --reload
```

## Endpoints
- `POST /voice`: Twilio webhook for call events
- `POST /tools/get_order_status`: ElevenLabs tool call handler
- Health check: `GET /`

## Integrations
- **Zendesk**: Uses email + API key for authentication
- **Slack**: Uses incoming webhook URL

---

See the code for more details and extend as needed for your use case. 
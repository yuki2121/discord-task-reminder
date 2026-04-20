import os
import requests
from flask import Flask, jsonify, request
from google import genai
from google.genai.types import HttpOptions
from google.genai import types

app = Flask(__name__)

def get_todo_text() -> str:
    # First working version: read todos from an env var.
    # Later you can replace this with Google Tasks, Notion, Sheets, etc.
    return os.getenv(
        "TODO_TEXT",
        "1. Test Discord reminder flow\n2. Review today's tasks\n3. Follow up unfinished work"
    )

def build_message_with_vertex(todo_text: str) -> str:
    client = genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        http_options=types.HttpOptions(api_version="v1"),
    )

    prompt = f"""
    You are a reminder assistant.
    Review the todo list below and write one short Discord reminder message.

    Rules:
    - Focus on unfinished or urgent-looking items
    - Keep it short
    - Use simple plain English
    - If nothing seems urgent, say: No urgent unfinished tasks right now.

    Todo list:
    {todo_text}
    """.strip()

    response = client.models.generate_content(
        model=os.getenv("MODEL_NAME", "gemini-2.5-pro"),
        contents=prompt,
    )

    client.close()
    return (response.text or "").strip()

@app.get("/")
def health():
    return jsonify({"ok": True, "message": "service is up"})

@app.post("/")
def run_job():
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    todo_text = get_todo_text()
    message = build_message_with_vertex(todo_text)

    discord_resp = requests.post(
        webhook_url,
        json={"content": message},
        timeout=20,
    )

    return jsonify({
        "ok": discord_resp.ok,
        "discord_status": discord_resp.status_code,
        "sent_message": message,
    }), 200 if discord_resp.ok else 500
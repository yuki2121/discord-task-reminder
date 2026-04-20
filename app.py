import os
import requests
from flask import Flask, jsonify, request, abort
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from google import genai
from google.genai import types
import threading
from google.cloud import firestore
from datetime import datetime, timezone

app = Flask(__name__)

DISCORD_API = "https://discord.com/api/v10"

db = firestore.Client()


def get_user_id_from_payload(payload: dict) -> str:
    if "member" in payload and "user" in payload["member"]:
        return payload["member"]["user"]["id"]
    if "user" in payload:
        return payload["user"]["id"]
    return "unknown"


def memory_doc(user_id: str):
    return db.collection("discord_memory").document(user_id)


def add_memory(user_id: str, note: str):
    doc = memory_doc(user_id)
    snap = doc.get()

    items = []
    if snap.exists:
        items = snap.to_dict().get("items", [])

    items.append({
        "text": note,
        "created_at": datetime.now(timezone.utc).isoformat()
    })

    doc.set({"items": items}, merge=True)


def get_memories(user_id: str) -> list[str]:
    snap = memory_doc(user_id).get()
    if not snap.exists:
        return []

    items = snap.to_dict().get("items", [])
    return [item.get("text", "") for item in items if item.get("text")]


def clear_memories(user_id: str):
    memory_doc(user_id).set({"items": []}, merge=True)

def update_discord_original_response(application_id: str, interaction_token: str, content: str):
    url = f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original"
    requests.patch(
        url,
        json={
            "content": content[:1800],
            "allowed_mentions": {"parse": []}
        },
        timeout=30,
    )


def handle_ask_command_async(application_id: str, interaction_token: str, question: str, user_id: str):
    try:
        if not question.strip():
            reply = "Please provide a question."
        else:
            reply = ask_agent(question, user_id).strip()

        if not reply:
            reply = "I couldn't generate a reply."
    except Exception as e:
        reply = f"Error: {str(e)}"

    try:
        update_discord_original_response(application_id, interaction_token, reply)
    except Exception as e:
        print(f"Failed to update Discord response: {e}")

def get_todo_text() -> str:
    return os.getenv(
        "TODO_TEXT",
        "1. Finish report\n2. Follow up email\n3. Check incomplete tasks"
    )


def get_vertex_client():
    return genai.Client(
        vertexai=True,
        project=os.environ["GOOGLE_CLOUD_PROJECT"],
        location=os.getenv("GOOGLE_CLOUD_LOCATION", "global"),
        http_options=types.HttpOptions(api_version="v1"),
    )


def generate_text(prompt: str) -> str:
    client = get_vertex_client()
    try:
        response = client.models.generate_content(
            model=os.getenv("MODEL_NAME", "gemini-2.5-flash"),
            contents=prompt,
        )
        text = (response.text or "").strip()
        return text if text else "I couldn't generate a reply."
    finally:
        client.close()


def build_daily_reminder(todo_text: str) -> str:
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
    return generate_text(prompt)


def ask_agent(question: str, user_id: str) -> str:
    todo_text = get_todo_text()
    memories = get_memories(user_id)
    memory_block = "\n".join(f"- {m}" for m in memories) if memories else "None"

    prompt = f"""
You are my Discord task assistant.

Current todo list:
{todo_text}

Saved memory for this user:
{memory_block}

User question:
{question}

Rules:
- Answer clearly and briefly
- Use saved memory if relevant
- Use the todo list if relevant
- If the question is unrelated, still answer helpfully
- Keep the reply under 1500 characters
""".strip()

    return generate_text(prompt)


def verify_discord_request():
    public_key = os.environ["DISCORD_PUBLIC_KEY"]
    verify_key = VerifyKey(bytes.fromhex(public_key))

    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    body = request.data.decode("utf-8")

    if not signature or not timestamp:
        abort(401, "missing discord signature headers")

    try:
        verify_key.verify(f"{timestamp}{body}".encode(), bytes.fromhex(signature))
    except BadSignatureError:
        abort(401, "invalid request signature")


@app.get("/")
def health():
    return jsonify({"ok": True, "message": "service is up"})


# Daily scheduler endpoint
@app.post("/run")
def run_job():
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    todo_text = get_todo_text()
    message = build_daily_reminder(todo_text)

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


# Discord interactions endpoint
@app.post("/discord")
def discord_interactions():
    verify_discord_request()
    payload = request.get_json(force=True)

    # Discord endpoint verification ping
    if payload.get("type") == 1:
        return jsonify({"type": 1})

    # Slash command
    if payload.get("type") == 2:
        command_name = payload["data"]["name"]
        user_id = get_user_id_from_payload(payload)

        if command_name == "ask":
            options = payload["data"].get("options", [])
            question = ""
            for opt in options:
                if opt.get("name") == "question":
                    question = opt.get("value", "")
                    break

            application_id = payload["application_id"]
            interaction_token = payload["token"]

            threading.Thread(
                target=handle_ask_command_async,
                args=(application_id, interaction_token, question, user_id),
                daemon=True,
            ).start()

            return jsonify({
                "type": 5,
                "data": {
                    "flags": 64
                }
            })

        if command_name == "remember":
            options = payload["data"].get("options", [])
            note = ""
            for opt in options:
                if opt.get("name") == "note":
                    note = opt.get("value", "")
                    break

            if not note.strip():
                return jsonify({
                    "type": 4,
                    "data": {
                        "content": "Please provide a note to remember.",
                        "flags": 64
                    }
                })

            add_memory(user_id, note.strip())
            return jsonify({
                "type": 4,
                "data": {
                    "content": f"Saved: {note[:150]}",
                    "flags": 64
                }
            })

        if command_name == "memories":
            memories = get_memories(user_id)
            if not memories:
                content = "No saved memories yet."
            else:
                content = "\n".join(f"{i+1}. {m}" for i, m in enumerate(memories[:20]))

            return jsonify({
                "type": 4,
                "data": {
                    "content": content[:1800],
                    "flags": 64
                }
            })

        if command_name == "clear_memories":
            clear_memories(user_id)
            return jsonify({
                "type": 4,
                "data": {
                    "content": "Cleared all saved memories.",
                    "flags": 64
                }
            })

    return jsonify({
        "type": 4,
        "data": {
            "content": "Unsupported command.",
            "flags": 64
        }
    })
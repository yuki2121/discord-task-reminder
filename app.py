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

def user_state_doc(user_id: str):
    return db.collection("discord_state").document(user_id)


def get_user_state(user_id: str) -> dict:
    snap = user_state_doc(user_id).get()
    if not snap.exists:
        return {"todos": [], "memories": [], "pending_answer": None}
    data = snap.to_dict() or {}
    data.setdefault("todos", [])
    data.setdefault("memories", [])
    data.setdefault("pending_answer", None)
    return data


def save_user_state(user_id: str, state: dict):
    user_state_doc(user_id).set(state, merge=True)


def add_memory(user_id: str, note: str):
    state = get_user_state(user_id)
    state["memories"].append({
        "text": note,
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    save_user_state(user_id, state)


def get_memories(user_id: str) -> list[str]:
    state = get_user_state(user_id)
    return [item.get("text", "") for item in state["memories"] if item.get("text")]


def clear_memories(user_id: str):
    state = get_user_state(user_id)
    state["memories"] = []
    save_user_state(user_id, state)


def add_todo(user_id: str, item: str):
    state = get_user_state(user_id)
    state["todos"].append({
        "text": item,
        "created_at": datetime.now(timezone.utc).isoformat()
    })
    save_user_state(user_id, state)


def get_todos(user_id: str) -> list[str]:
    state = get_user_state(user_id)
    return [item.get("text", "") for item in state["todos"] if item.get("text")]


def clear_todos(user_id: str):
    state = get_user_state(user_id)
    state["todos"] = []
    save_user_state(user_id, state)


def set_pending_answer(user_id: str, answer: str):
    state = get_user_state(user_id)
    state["pending_answer"] = {
        "text": answer,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    save_user_state(user_id, state)


def get_pending_answer(user_id: str) -> str | None:
    state = get_user_state(user_id)
    pending = state.get("pending_answer")
    if not pending:
        return None
    return pending.get("text")


def clear_pending_answer(user_id: str):
    state = get_user_state(user_id)
    state["pending_answer"] = None
    save_user_state(user_id, state)

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

        set_pending_answer(user_id, reply)

        url = f"{DISCORD_API}/webhooks/{application_id}/{interaction_token}/messages/@original"
        requests.patch(
            url,
            json={
                "content": reply[:1800],
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 2,
                                "style": 3,
                                "label": "Remember this answer",
                                "custom_id": "remember_last_answer"
                            },
                            {
                                "type": 2,
                                "style": 2,
                                "label": "Discard",
                                "custom_id": "discard_last_answer"
                            }
                        ]
                    }
                ],
                "allowed_mentions": {"parse": []}
            },
            timeout=30,
        )
    except Exception as e:
        try:
            update_discord_original_response(application_id, interaction_token, f"Error: {str(e)}")
        except Exception as inner_e:
            print(f"Failed to update Discord response: {inner_e}")
            
            

def get_todo_text(user_id: str) -> str:
    todos = get_todos(user_id)
    return "\n".join(f"- {t}" for t in todos) if todos else "None"


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
    todos = get_todos(user_id)
    todo_block = "\n".join(f"- {t}" for t in todos) if todos else "None"

    memories = get_memories(user_id)
    memory_block = "\n".join(f"- {m}" for m in memories) if memories else "None"

    prompt = f"""
You are my Discord task assistant.

Current todo list:
{todo_block}

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
def get_owner_user_id() -> str:
    return os.environ["OWNER_USER_ID"]


@app.post("/run")
def run_job():
    webhook_url = os.environ["DISCORD_WEBHOOK_URL"]
    owner_user_id = get_owner_user_id()
    todos = get_todos(owner_user_id)
    todo_text = "\n".join(f"- {t}" for t in todos) if todos else "No todos saved."
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
        if command_name == "todo_add":
            options = payload["data"].get("options", [])
            item = ""
            for opt in options:
                if opt.get("name") == "item":
                    item = opt.get("value", "")
                    break

            if not item.strip():
                return jsonify({
                    "type": 4,
                    "data": {
                        "content": "Please provide a todo item.",
                        "flags": 64
                    }
                })

            add_todo(user_id, item.strip())
            return jsonify({
                "type": 4,
                "data": {
                    "content": f"Added todo: {item[:150]}",
                    "flags": 64
                }
            })

        if command_name == "todo_list":
            todos = get_todos(user_id)
            if not todos:
                content = "No todos saved."
            else:
                content = "\n".join(f"{i+1}. {t}" for i, t in enumerate(todos[:20]))

            return jsonify({
                "type": 4,
                "data": {
                    "content": content[:1800],
                    "flags": 64
                }
            })

        if command_name == "todo_clear":
            clear_todos(user_id)
            return jsonify({
                "type": 4,
                "data": {
                    "content": "Cleared all todos.",
                    "flags": 64
                }
            })
            
            
    # Button / component interaction
    if payload.get("type") == 3:
        user_id = get_user_id_from_payload(payload)
        custom_id = payload["data"]["custom_id"]

        if custom_id == "remember_last_answer":
            pending = get_pending_answer(user_id)
            if not pending:
                content = "There is no pending answer to remember."
            else:
                add_memory(user_id, pending)
                clear_pending_answer(user_id)
                content = "Saved that answer to memory."

            return jsonify({
                "type": 7,
                "data": {
                    "content": content,
                    "components": []
                }
            })

        if custom_id == "discard_last_answer":
            clear_pending_answer(user_id)
            return jsonify({
                "type": 7,
                "data": {
                    "content": "Okay, I did not save that answer.",
                    "components": []
                }
            })
            
            
    return jsonify({
        "type": 4,
        "data": {
            "content": "Unsupported command.",
            "flags": 64
        }
    })
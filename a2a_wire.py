"""Small, readable A2A v0.3 JSON-RPC client for the classroom.
Not a replacement for the full A2A SDK; implements message/send and tasks/get.
"""
import uuid
import httpx


def text_part(text):
    return {"kind": "text", "text": text}


def data_part(data):
    return {"kind": "data", "data": data}


def make_message(parts, task_id=None, context_id=None, sender_url=None):
    msg = {"kind": "message", "messageId": str(uuid.uuid4()), "role": "user", "parts": parts}
    if sender_url:
        msg["metadata"] = {"sender_url": sender_url.rstrip("/")}
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    return msg


def rpc(url, method, params, timeout=18):
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.post(url.rstrip('/'), json=payload)
        response.raise_for_status()
        body = response.json()
    if "error" in body:
        raise RuntimeError(f"A2A {method}: {body['error'].get('message', body['error'])}")
    return body["result"]


def send(url, parts, sender_url=None):
    return rpc(url, "message/send", {"message": make_message(parts, sender_url=sender_url), "configuration": {"blocking": False}})


def get_task(url, task_id):
    return rpc(url, "tasks/get", {"id": task_id})


def extract_data(task):
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                return part.get("data", {})
    return {}


def extract_text(task):
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                return part.get("text", "")
    status_msg = task.get("status", {}).get("message", {})
    return '\n'.join(p.get('text', '') for p in status_msg.get('parts', []) if p.get('kind') == 'text')

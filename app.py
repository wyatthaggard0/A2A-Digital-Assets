"""Student A2A peer: Gradio UI, A2A JSON-RPC endpoint, human-approved replies."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr
import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from a2a_wire import send, get_task, extract_data, extract_text

ROOT = Path(__file__).parent
PROFILE_PATH = ROOT / 'profile.json'
INSTRUCTIONS_PATH = ROOT / 'agent_instructions.md'
LOCK = threading.RLock()
INCOMING = {}  # A2A Task objects, one per question
DRAFTS = {}    # Local UI drafts, keyed by A2A Task ID
OUTGOING = {}  # Local tracking of remote A2A task IDs and endpoints
MODEL = os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini')
REGISTRY_URL = os.environ.get('INSTRUCTOR_REGISTRY_URL', '').strip().rstrip('/')


def now():
    return datetime.now(timezone.utc).isoformat()


def public_url():
    value = os.environ.get('PUBLIC_A2A_URL', '').strip().rstrip('/')
    if value:
        return value
    codespace = os.environ.get('CODESPACE_NAME', '')
    domain = os.environ.get('GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN', 'app.github.dev')
    return f'https://{codespace}-8000.{domain}' if codespace else 'http://localhost:8000'


def profile():
    return json.loads(PROFILE_PATH.read_text(encoding='utf-8'))


def card():
    p = profile()
    return {
        'protocolVersion': '0.3.0', 'name': p['name'], 'description': p['purpose'],
        'url': public_url() + '/', 'preferredTransport': 'JSONRPC', 'version': '1.0.0',
        'capabilities': {'streaming': False, 'pushNotifications': False},
        'defaultInputModes': ['text/plain', 'application/json'],
        'defaultOutputModes': ['text/plain', 'application/json'],
        'skills': [{'id': 'answer_student_question', 'name': 'Answer a classmate',
                    'description': 'Answer a question with a draft and explicit student approval',
                    'tags': ['student', 'human-in-the-loop', 'questions']}],
        'studentProfile': {'hometown': p['hometown'], 'hobby': p['hobby'], 'favoriteMovie': p['favoriteMovie']},
    }


def status_message(text):
    return {'kind': 'message', 'messageId': str(uuid.uuid4()), 'role': 'agent',
            'parts': [{'kind': 'text', 'text': text}]}


def rpc_error(request_id, code, message):
    return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': code, 'message': message}}


def llm_text(system, user):
    if not os.environ.get('OPENAI_API_KEY'):
        return 'Draft unavailable: set OPENAI_API_KEY, or write your own answer here.'
    response = OpenAI().responses.create(model=MODEL, instructions=system, input=user, tools=[{"type": "web_search"}])
    return response.output_text.strip()


def make_draft(task_id, question):
    try:
        p = profile()
        instructions = INSTRUCTIONS_PATH.read_text(encoding='utf-8')
        draft = llm_text(
            instructions + '\nYou are drafting a reply for the local student to review. Never claim to know personal experiences not provided. Ask the student to correct or personalize your draft.\n'
            + 'Student profile: ' + json.dumps(p), question)
    except Exception as exc:
        draft = f'Please write your own answer. (Draft error: {exc})'
    with LOCK:
        if task_id in INCOMING and INCOMING[task_id]['status']['state'] == 'input-required':
            DRAFTS[task_id] = draft


def approve(task_id, answer):
    with LOCK:
        task = INCOMING.get(task_id)
        if not task:
            return 'Task not found'
        if task['status']['state'] != 'input-required':
            return 'Task has already been resolved'
        if not answer.strip():
            return 'Please enter an answer before approving'
        task['status'] = {'state': 'completed', 'timestamp': now(), 'message': status_message('Student approved the answer')}
        task['artifacts'] = [{'artifactId': str(uuid.uuid4()), 'parts': [{'kind': 'text', 'text': answer.strip()}]}]
        DRAFTS[task_id] = answer.strip()
        return f'Approved task {task_id[:8]}; sender can now retrieve the response via A2A tasks/get.'


def decline(task_id):
    with LOCK:
        task = INCOMING.get(task_id)
        if not task:
            return 'Task not found'
        if task['status']['state'] != 'input-required':
            return 'Task already resolved'
        task['status'] = {'state': 'rejected', 'timestamp': now(), 'message': status_message('Student declined to answer')}
        return 'Question declined'


api = FastAPI(title='Student A2A Peer')


@api.get('/.well-known/agent-card.json')
def well_known_card():
    return card()


@api.get('/health')
def health():
    return {'ok': True, 'pending': sum(t['status']['state'] == 'input-required' for t in INCOMING.values())}


@api.post('/')
async def a2a_server(request: Request):
    payload = {}
    try:
        payload = await request.json()
        req_id, method, params = payload.get('id'), payload.get('method'), payload.get('params', {})
        if method == 'tasks/get':
            with LOCK:
                task = INCOMING.get(params.get('id'))
                task = json.loads(json.dumps(task)) if task else None
            return {'jsonrpc': '2.0', 'id': req_id, 'result': task} if task else JSONResponse(rpc_error(req_id, -32001, 'Task not found'))
        if method == 'tasks/cancel':
            task_id = params.get('id')
            with LOCK:
                task = INCOMING.get(task_id)
                if not task:
                    return JSONResponse(rpc_error(req_id, -32001, 'Task not found'))
                if task['status']['state'] != 'input-required':
                    return JSONResponse(rpc_error(req_id, -32002, 'Task cannot be canceled'))
                task['status'] = {'state': 'canceled', 'timestamp': now(), 'message': status_message('Canceled by sender')}
                return {'jsonrpc': '2.0', 'id': req_id, 'result': json.loads(json.dumps(task))}
        if method != 'message/send':
            return JSONResponse(rpc_error(req_id, -32601, 'Unknown A2A method'))
        message = params.get('message', {})
        question = '\n'.join(p.get('text', '') for p in message.get('parts', []) if p.get('kind') == 'text').strip()
        if not question:
            return JSONResponse(rpc_error(req_id, -32602, 'Question must be text'))
        # Enforce registry toggle at the receiving peer, not merely in its UI.
        mode = classroom_mode()
        sender = str(message.get('metadata', {}).get('sender_url', '')).rstrip('/')
        if mode.get('instructor_only') and (not mode.get('instructor_url') or sender != mode['instructor_url'].rstrip('/')):
            return JSONResponse(rpc_error(req_id, -32003, 'Instructor-only mode: peer requests are disabled'))
        task_id = str(uuid.uuid4())
        task = {'kind': 'task', 'id': task_id, 'contextId': message.get('contextId') or str(uuid.uuid4()),
                'status': {'state': 'input-required', 'timestamp': now(),
                           'message': status_message('Waiting for the student to review and approve a draft')},
                'history': [message], 'artifacts': []}
        with LOCK:
            INCOMING[task_id] = task
            DRAFTS[task_id] = '(LLM is drafting an answer; refresh in a few seconds.)'
        threading.Thread(target=make_draft, args=(task_id, question), daemon=True).start()
        return {'jsonrpc': '2.0', 'id': req_id, 'result': task}
    except Exception as exc:
        return JSONResponse(rpc_error(payload.get('id'), -32603, str(exc)))


def registry_request(operation, **kwargs):
    if not REGISTRY_URL:
        raise ValueError('INSTRUCTOR_REGISTRY_URL secret is missing')
    task = send(REGISTRY_URL, [{'kind': 'data', 'data': {'operation': operation, **kwargs}}])
    if task['status']['state'] != 'completed':
        task = get_task(REGISTRY_URL, task['id'])
    return extract_data(task)


def update_card(name, purpose, hometown, hobby, favorite_movie):
    values = {'name': name, 'purpose': purpose, 'hometown': hometown, 'hobby': hobby, 'favoriteMovie': favorite_movie}
    if not all(isinstance(v, str) and v.strip() for v in values.values()):
        return 'All five fields are required'
    PROFILE_PATH.write_text(json.dumps({k: v.strip() for k, v in values.items()}, indent=2), encoding='utf-8')
    return 'Updated local Agent Card. Click Register or ask the chatbot to register.'


def register():
    result = registry_request('register', card=card())
    return f"Registered {card()['name']} at {result.get('url')}. Registry has {result.get('count')} agents."


def list_agents():
    return registry_request('list').get('agents', {})


def friendly_directory():
    agents = list_agents()
    if not agents:
        return 'No agents registered yet.'
    sections = []
    for i, (url, entry) in enumerate(agents.items(), 1):
        p = entry.get('card', {}).get('studentProfile', {})
        sections.append(f"### {i}. {entry.get('name', 'Unnamed')}\n"
                        f"**Hometown:** {p.get('hometown', '?')} · **Hobby:** {p.get('hobby', '?')} · **Favorite movie:** {p.get('favoriteMovie', '?')}\n\n"
                        f"**Purpose:** {entry.get('purpose', '')}\n\n**A2A URL:** `{url}`")
    return '\n\n---\n\n'.join(sections)


def classroom_mode():
    return registry_request('mode')

def send_question(target_url, question):
    if classroom_mode().get('instructor_only'):
        raise ValueError('Instructor-only mode: students cannot initiate peer requests.')
    agents = list_agents()
    url = target_url.strip().rstrip('/')
    if url not in agents:
        raise ValueError('Select a registered agent URL from the directory')
    if not question.strip():
        raise ValueError('Question is empty')
    result = send(url, [{'kind': 'text', 'text': question.strip()}], sender_url=public_url())
    task_id = result['id']
    with LOCK:
        OUTGOING[task_id] = {'url': url, 'name': agents[url].get('name', url), 'question': question.strip(),
                             'state': result['status']['state'], 'answer': '', 'createdAt': now()}
    return f"Sent to {agents[url].get('name')} · task {task_id[:8]} · {result['status']['state']}. See Outgoing Requests."


def refresh_outgoing():
    with LOCK:
        items = [(key, dict(val)) for key, val in OUTGOING.items()]
    for task_id, record in items:
        if record['state'] in ('completed', 'failed', 'rejected', 'canceled'):
            continue
        try:
            remote = get_task(record['url'], task_id)
            state = remote['status']['state']
            answer = extract_text(remote) if state in ('completed', 'failed', 'rejected', 'canceled') else ''
        except Exception as exc:
            state, answer = record['state'], f'(temporarily unreachable: {exc})'
        with LOCK:
            if task_id in OUTGOING:
                OUTGOING[task_id].update(state=state, answer=answer)
    with LOCK:
        rows = [dict(id=k[:8], agent=v['name'], question=v['question'], status=v['state'],
                     answer=v['answer'] or 'Awaiting student approval') for k, v in OUTGOING.items()]
    if not rows:
        return 'No outgoing requests yet.'
    return '\n\n---\n\n'.join(f"### To {r['agent']} · {r['status']} · {r['id']}\n**Question:** {r['question']}\n\n**Reply:** {r['answer']}" for r in rows)


def incoming_choices():
    with LOCK:
        return [(f"{k[:8]} · {t['status']['state']} · {next((p.get('text', '') for p in t['history'][0]['parts'] if p.get('kind') == 'text'), '')[:65]}", k)
                for k, t in INCOMING.items()]


def refresh_inbox():
    choices = incoming_choices()
    with LOCK:
        pending = sum(t['status']['state'] == 'input-required' for t in INCOMING.values())
    return gr.update(choices=choices), f'**{pending} pending question(s)** · {len(choices)} total. Select any task to review.'


def select_incoming(task_id):
    with LOCK:
        task = INCOMING.get(task_id)
        if not task:
            return '', '', 'Select a question'
        question = '\n'.join(p.get('text', '') for p in task['history'][0]['parts'] if p.get('kind') == 'text')
        draft = DRAFTS.get(task_id, '')
        status = task['status']['state']
    return question, draft, f'Task `{task_id}` · **{status}**'


def save_draft_edit(task_id, value):
    if task_id:
        with LOCK:
            if task_id in INCOMING and INCOMING[task_id]['status']['state'] == 'input-required':
                DRAFTS[task_id] = value


def approve_ui(task_id, draft):
    if not task_id:
        return 'Select a question first'
    return approve(task_id, draft)


def decline_ui(task_id):
    if not task_id:
        return 'Select a question first'
    return decline(task_id)


TOOLS = [
    {'type': 'function', 'name': 'update_my_agent_card', 'description': 'Update your own agent profile and published A2A Agent Card. Ask for any missing fields.',
     'parameters': {'type': 'object', 'properties': {k: {'type': 'string'} for k in ('name', 'purpose', 'hometown', 'hobby', 'favorite_movie')},
                    'required': ['name', 'purpose', 'hometown', 'hobby', 'favorite_movie'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'register_my_agent', 'description': 'Publish current Agent Card to instructor registry using A2A.',
     'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
    {'type': 'function', 'name': 'show_registry', 'description': 'Get and display the registered agents and their student profiles.',
     'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
    {'type': 'function', 'name': 'ask_agent', 'description': 'Send a question to a particular registered agent using A2A. The target_url must be from the registry.',
     'parameters': {'type': 'object', 'properties': {'target_url': {'type': 'string'}, 'question': {'type': 'string'}},
                    'required': ['target_url', 'question'], 'additionalProperties': False}},
    {'type': 'function', 'name': 'check_outgoing', 'description': 'Check replies from agents to whom you sent questions.',
     'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}},
]


def normalize_chat_history(history):
    """Convert Gradio messages to Responses API-compatible plain-text messages.

    Gradio 6 may represent text as {"type": "text", "text": ...}, which
    is not a valid Responses API content-part type.
    """
    normalized = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = item.get('role')
        if role not in ('user', 'assistant'):
            continue
        content = item.get('content', '')
        if isinstance(content, str):
            text = content
        elif isinstance(content, dict):
            text = content.get('text', '') if content.get('type') in ('text', 'input_text', 'output_text') else ''
        elif isinstance(content, list):
            text = '\n'.join(
                part.get('text', '') for part in content
                if isinstance(part, dict)
                and part.get('type') in ('text', 'input_text', 'output_text')
                and isinstance(part.get('text'), str)
            )
        else:
            text = ''
        if isinstance(text, str) and text.strip():
            normalized.append({'role': role, 'content': text})
    return normalized


def chat(message, history):
    if not message.strip():
        return '', history
    history = list(history or [])
    history.append({'role': 'user', 'content': message})
    if not os.environ.get('OPENAI_API_KEY'):
        history.append({'role': 'assistant', 'content': 'Set OPENAI_API_KEY in Codespaces secrets, then restart the app.'})
        return '', history
    instructions = INSTRUCTIONS_PATH.read_text(encoding='utf-8') + '\nCurrent Agent Card: ' + json.dumps(card())
    # Keep only user/assistant text from Gradio history for the model.
    conversation = normalize_chat_history(history)
    try:
        client = OpenAI()
        for _ in range(6):
            response = client.responses.create(model=MODEL, instructions=instructions, input=conversation, tools=TOOLS + [{"type": "web_search"}])
            conversation += response.output
            calls = [item for item in response.output if item.type == 'function_call']
            if not calls:
                history.append({'role': 'assistant', 'content': response.output_text or 'Done.'})
                return '', history
            for call in calls:
                args = json.loads(call.arguments)
                try:
                    if call.name == 'update_my_agent_card':
                        output = update_card(**args)
                    elif call.name == 'register_my_agent':
                        output = register()
                    elif call.name == 'show_registry':
                        output = friendly_directory()
                    elif call.name == 'ask_agent':
                        output = send_question(**args)
                    elif call.name == 'check_outgoing':
                        output = refresh_outgoing()
                    else:
                        output = 'Unknown tool'
                except Exception as exc:
                    output = f'Tool error: {exc}'
                conversation.append({'type': 'function_call_output', 'call_id': call.call_id, 'output': str(output)})
        history.append({'role': 'assistant', 'content': 'Too many tool steps. Please try a simpler request.'})
    except Exception as exc:
        history.append({'role': 'assistant', 'content': f'Error: {exc}'})
    return '', history


def directory_ui():
    try:
        return friendly_directory(), gr.update(choices=[(v.get('name', k) + ' — ' + k, k) for k, v in list_agents().items()])
    except Exception as exc:
        return f'Registry error: {exc}', gr.update(choices=[])


def safe_register():
    try:
        return register()
    except Exception as exc:
        return f'Registration error: {exc}'


def safe_send(url, question):
    try:
        return send_question(url or '', question)
    except Exception as exc:
        return f'Send error: {exc}'


def build_ui():
    with gr.Blocks(title='A2A Classroom Peer') as demo:
        gr.Markdown('# A2A Classroom Peer\nYour agent can register, ask classmates questions, and approve replies to incoming A2A tasks.')
        gr.Markdown(f'**Your A2A endpoint:** `{public_url()}` · **Agent Card:** `{public_url()}/.well-known/agent-card.json`')
        with gr.Tab('Agent Chat'):
            chatbox = gr.Chatbot(height=380)
            msg = gr.Textbox(label='Talk to your agent', placeholder='Update my card: I am ... / Show the registry / Ask ...', lines=2)
            send_chat = gr.Button('Send', variant='primary')
            msg.submit(chat, [msg, chatbox], [msg, chatbox])
            send_chat.click(chat, [msg, chatbox], [msg, chatbox])
        with gr.Tab('Directory & Send'):
            directory = gr.Markdown('Click Refresh Directory to view classmates.')
            target = gr.Dropdown(label='Select recipient agent', choices=[])
            gr.Button('Refresh Directory via A2A').click(directory_ui, outputs=[directory, target])
            question = gr.Textbox(label='Question to send', lines=3)
            send_status = gr.Textbox(label='A2A send status', interactive=False)
            gr.Button('Send Question via A2A').click(safe_send, [target, question], send_status)
        with gr.Tab('Incoming Requests'):
            inbox_info = gr.Markdown('Select Refresh Inbox to see incoming tasks.')
            incoming = gr.Dropdown(label='Select an incoming A2A task', choices=[])
            gr.Button('Refresh Inbox').click(refresh_inbox, outputs=[incoming, inbox_info])
            task_status = gr.Markdown()
            incoming_question = gr.Textbox(label='Question received', lines=3, interactive=False)
            draft = gr.Textbox(label='Editable answer — only sent after approval', lines=6)
            incoming.change(select_incoming, incoming, [incoming_question, draft, task_status])
            draft.input(save_draft_edit, [incoming, draft], None)
            approve_status = gr.Textbox(label='Approval result', interactive=False)
            with gr.Row():
                gr.Button('Approve & Send Answer', variant='primary').click(approve_ui, [incoming, draft], approve_status)
                gr.Button('Decline Question').click(decline_ui, incoming, approve_status)
            gr.Markdown('**Important:** Refreshing the inbox does not send answers. Each task must be approved individually.')
        with gr.Tab('Outgoing Requests'):
            outgoing_display = gr.Markdown('No outgoing requests yet.')
            gr.Button('Refresh A2A Task Statuses').click(refresh_outgoing, outputs=outgoing_display)
        gr.Markdown('A2A v0.3 JSON-RPC classroom subset: `message/send`, `tasks/get`, `tasks/cancel`, Agent Cards. No streaming.')
    return demo


if __name__ == '__main__':
    threading.Thread(target=lambda: uvicorn.run(api, host='0.0.0.0', port=8000, log_level='info'), daemon=True).start()
    build_ui().queue().launch(server_name='0.0.0.0', server_port=7860, share=False)

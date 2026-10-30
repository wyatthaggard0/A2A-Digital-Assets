# Student A2A Peer — Classroom Exercise

Your Codespace runs **two services**: a Gradio UI on port **7860** and an A2A v0.3 JSON-RPC agent on port **8000**. Your agent registers with the instructor's A2A registry and exchanges A2A tasks directly with classmates. You review and approve every answer before it is released.

## Before opening the Codespace: set TWO Codespaces repository secrets

In the **student GitHub repository**, open **Settings → Secrets and variables → Codespaces → New repository secret**. Create:

| Secret | Value |
|---|---|
| `OPENAI_API_KEY` | Your instructor-provided OpenAI API key, e.g. `sk-...` |
| `INSTRUCTOR_REGISTRY_URL` | The instructor's public Codespaces port-8000 HTTPS URL, e.g. `https://instructor-8000.app.github.dev` |

If the secrets are added after your Codespace was created, **rebuild/restart the Codespace** so the environment sees them. The `.devcontainer/devcontainer.json` references both secrets; it does not contain their values.

## Start

1. Upload the contents of this ZIP to a new GitHub repository, including `.devcontainer/devcontainer.json`, then create a Codespace. (For a class, the instructor can distribute one template repo.)
2. Wait for Python dependencies to install via `postCreateCommand`.
3. Run **`python app.py`** in the terminal. Keep it running during class.
4. In the Codespaces **Ports** tab, make port **8000 Public** so other agents can reach your A2A server. Keep Gradio port **7860 Private** if you like; open its forwarded link in your browser.
5. The A2A URL is the forwarded port-8000 HTTPS URL. Your Agent Card is at `<your A2A URL>/.well-known/agent-card.json`.

## Exercise 1 — Tell your agent to update its Agent Card and register

In the **Agent Chat** tab, type something like:

> Update my Agent Card. My agent is called Jordan's Agent, its purpose is to answer classmates' questions about me, my hometown is Denver, my hobby is cycling, and my favorite movie is The Matrix. Then register the updated card with the instructor registry.

The LLM should invoke `update_my_agent_card` (which writes `profile.json` and changes the card published by your server), then `register_my_agent` (which sends an **A2A `message/send`** to the instructor). You can also use the **Register / Update my Agent Card via A2A** button in the Directory tab.

**Important:** The card is published from `profile.json`. The text instructions in `agent_instructions.md` govern the agent's behavior; the Agent Card advertises its capabilities and student profile. Editing instructions alone does **not** update a card until the profile-update tool runs.

## Exercise 2 — Discover classmates and send a question

1. In **Agent Chat**, ask: **“Show me all the agents in the registry and their hometowns, hobbies, and favorite movies.”** Your agent queries the instructor using A2A and presents a friendly directory.
2. Ask: **“Ask [classmate agent] what they enjoy most about their hobby.”** The LLM chooses the registered endpoint and invokes `ask_agent` to send an A2A `message/send` with the question as a TextPart.
3. Alternatively, use **Directory & Send**: click **Refresh Directory via A2A**, choose a registered agent, type a question, and click **Send Question via A2A**.
4. Open **Outgoing Requests** and click **Refresh A2A Task Statuses**. The receiving A2A task will typically remain **`input-required`** until the other student approves an answer. When it changes to **`completed`**, your agent retrieves the approved response via `tasks/get`.

## Exercise 3 — Receive, edit, and approve a question

1. Ask a classmate to send you a question. Your A2A server immediately creates a **separate A2A Task** and returns its `id`, `contextId`, and `input-required` status. It does **not** wait for you to answer.
2. Open **Incoming Requests** and click **Refresh Inbox**. Each request is listed separately with a short task ID, status, and question preview.
3. Select any question. The LLM drafts an answer asynchronously. If it is still drafting, refresh the inbox and reselect the task after a few seconds.
4. Edit the answer in the **Editable answer** box. Your changes are kept separately for that task. Click **Approve & Send Answer**. Until you click that button, the answer is **not** published.
5. The A2A task changes to **`completed`** and includes your approved text in an A2A artifact. The original sender receives it on their next `tasks/get` poll.
6. You may instead click **Decline Question**, changing the task to `rejected`.

## Exercise 4 — Concurrent conversations

Have three classmates ask you questions at about the same time. You should see **three separate A2A task IDs** in the Incoming Requests tab. Select them in any order, edit different drafts, and approve each separately. The sender's **Outgoing Requests** tab also separates each task by recipient, question, task ID, state, and answer.

**Concurrency design:** The FastAPI A2A endpoint accepts requests promptly; LLM drafting happens in background threads; each task has its own draft and A2A status. The Gradio UI does not block new incoming requests while a human is editing an answer. Pending tasks are in memory and are lost when the student app restarts.

## Files to understand

```text
student-repo/
├── .devcontainer/devcontainer.json  # Codespaces Python setup + ports + secrets
├── app.py                            # A2A server, OpenAI tools, Gradio UI
├── a2a_wire.py                       # Readable A2A JSON-RPC client
├── agent_instructions.md            # Agent behavior; edit this
├── profile.json                      # Editable profile used to publish Agent Card
├── requirements.txt
└── README.md
```

## A2A details and limits

- A2A Agent Cards: `/.well-known/agent-card.json`.
- A2A transport: JSON-RPC over HTTPS; `message/send` for questions and registry operations; `tasks/get` for results; `tasks/cancel` supported for pending student tasks.
- `input-required` means the **receiving human** has not approved a draft. This is our classroom use of a standard A2A task state.
- `studentProfile` is a **custom classroom field**, not part of the standard Agent Card schema.
- The A2A wire code is a small **v0.3 subset implemented directly for teaching**; it does not use the full `a2a-sdk`, support streaming/push notifications, or guarantee interoperability with every A2A implementation. For a production system, prefer the maintained SDK, persistent task storage, authorization, and a supported current protocol version.
- Your public A2A port is unauthenticated. **Do not put secrets or private information in the profile or messages.**
- If you create a new Codespace with a different public URL, register again; the instructor may remove the old record.

[Official A2A v0.3 specification](https://a2a-protocol.org/v0.3.0/specification/)

## NEW: Instructor-only classroom mode

The instructor can turn on **Instructor-only mode** from the instructor registry's home page. When enabled:

- You can still update your card, register, and view the directory.
- Your agent cannot send direct requests to classmates.
- Your A2A server accepts incoming requests from the instructor coordinator's configured sender URL, and rejects other peers' requests.
- You can review and approve instructor-initiated requests in **Incoming Requests** exactly as before.

When the instructor turns the mode **off**, students can send questions directly to each other again. The receiving server checks the mode through an A2A request to the registry for each new task; this means the mode applies without restarting your Codespace.

This is classroom-only enforcement: sender URLs are self-declared A2A message metadata and are not authenticated. If the registry is unreachable, the receiving agent will fail closed for new tasks until the registry responds.

## Customizing your agent's system instructions and web search

Edit **`agent_instructions.md`** to change how your agent behaves. The application reads this file when it handles your chat messages and when it generates proposed answers for incoming A2A questions; no Python editing is required to change the prompt. The current Agent Card/profile is supplied separately as context.

For example, add: *"When answering questions about my hometown, use web search to identify accurate local landmarks and suggest a first-person draft for me to personalize. Never invent my experiences."*

Web search is enabled in **both** the student chatbot and the background incoming-answer draft generator using the OpenAI Responses API's `web_search` tool. Search results are reference material, not proof of the student's preferences. **All incoming answers still require the receiving student's review and approval.** Web search may incur additional API charges.

The model uses the `OPENAI_API_KEY` Codespaces secret already required by this repository; no additional search API key is needed.

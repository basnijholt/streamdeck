# T3 Code protocol used by the deck

Checked against `t3 serve` **v0.1.3** (orchestration protocol 2) on 2026-10-28, reading
the `pingdotgg/t3code` source at tag `v0.1.3`. The client is `src/deck/t3/source.py`.
The recorded responses are in `tests/fixtures/t3/`.

## Decision: WebSocket RPC (`/ws`), not MCP

The brief preferred MCP. It does not work with the deck's token:

- `/api/mcp` does not exist on v0.1.3 (HTTP 404). The MCP endpoint is `/mcp`
  (`apps/server/src/mcp/McpHttpServer.ts`).
- `/mcp` rejects a token from `t3 auth session issue`, whatever its scopes:
  `401 {"error":"invalid_mcp_credential","message":"A valid T3 Code MCP credential is required."}`.
  `/mcp` only accepts tokens issued to the server's own agents, and MCP client sessions
  (session subject `mcp-client`). MCP clients get those through the browser OAuth approval flow.
  `t3 thread list` mints one in-process from the server's secret store
  (`EnvironmentAuth.authenticateMcpClient`, `cli/thread.ts`).
- One way around it would be `t3 auth session issue --subject mcp-client`. It was not used:
  it depends on an internal constant, and the server caps such sessions to MCP-only access.

The WebSocket RPC is what the T3 web client itself uses. It accepts the bearer token and has
everything the deck needs:

| Need | WebSocket RPC |
|---|---|
| threads across all projects, with status, `projectId`, model selection, a needs-input signal | `orchestration.subscribeShell`: one snapshot of every thread shell, then pushed changes |
| one thread's model selection, fresh | `orchestration.getThreadFamily` |
| set model selection | `orchestration.dispatchCommand` with `thread.model-selection.set` |

Because the shell stream is pushed, `watch` keeps a single subscription open instead of
polling. Building a snapshot costs the server 0.14 to 1.1 s (and over 10 s once on a cold
server). Polling for one every second would keep the server busy all the time. Changes are
pushed as they happen, batched within 50 ms. In the spike, the update caused by the no-op set
below arrived well under a second after the command.

## Auth

- Token: `t3 auth session issue --scope orchestration:read --scope orchestration:operate --label streamdeck --ttl 365d --token-only`.
- It goes in the WebSocket upgrade request: `Authorization: Bearer <token>`. The server reads
  this header when the URL carries no `wsTicket`
  (`EnvironmentAuth.authenticateWebSocketUpgrade`). `/api/auth/websocket-ticket` exists only
  because browsers cannot set headers on a WebSocket, so the deck does not use it.
- Scopes (`apps/server/src/auth/RpcAuthorization.ts`): `subscribeShell` and `getThreadFamily`
  need `orchestration:read`, and `dispatchCommand` needs `orchestration:operate`.
- ⚠️ `orchestration:operate` allows every orchestration command, including starting runs
  and sending messages. No narrower scope can set a model. The deck sends only
  `thread.model-selection.set`.
- Rejected upgrades, as recorded:
  - a bad token gets `401 {"_tag":"EnvironmentAuthInvalidError","code":"auth_invalid","reason":"invalid_credential",…}`
  - no token gets the same with `"reason":"missing_credential"`
  - a missing or wrong protocol version gets
    `426 {"code":"orchestration_protocol_incompatible","message":"Update this client to one that supports orchestration protocol 2.","orchestrationProtocolVersion":2}`

  The client raises `T3AuthError` on a 401 or 403 and `T3Error` on anything else. `watch`
  waits the longest backoff (30 s) after an auth error.

## Transport

`GET <url>/ws?orchestrationProtocol=2`, where `http` becomes `ws` and `https` becomes `wss`.
The URL is `T3Server.url`, for example `ws://127.0.0.1:3773/ws?orchestrationProtocol=2`.

Messages are Effect RPC with JSON serialization (`RpcSerialization.layerJson`). Each
WebSocket frame holds one JSON object, or an array of them.

Client to server:

```json
{"_tag": "Request", "id": "1", "tag": "<method>", "payload": {…}, "headers": []}
{"_tag": "Ack", "requestId": "1"}
{"_tag": "Ping"}
{"_tag": "Interrupt", "requestId": "1"}
```

Server to client:

```json
{"_tag": "Chunk", "requestId": "1", "values": [ …stream items… ]}
{"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Success", "value": …}}
{"_tag": "Exit", "requestId": "1", "exit": {"_tag": "Failure", "cause": [{"_tag": "Fail", "error": {"_tag": "…", "message": "…"}}]}}
{"_tag": "Pong"}
{"_tag": "Defect", "defect": …}
{"_tag": "ClientProtocolError", "error": …}
```

- A stream request gets `Chunk`s and then an `Exit`. The server sends the next chunk only after
  the client sends an **`Ack`** for the previous one. A client that does not ack stalls its
  subscription.
- A failed schema check on the payload comes back as `{"_tag":"Die","defect":"Missing key\n  at [\"modelSelection\"]"}`
  inside `cause`.
- `Defect` and `ClientProtocolError` end every request on the connection. The client treats
  both as a lost connection.
- The deck sends one request per connection, always with id `"1"`. One-shot requests open a
  connection, which takes 4 ms on localhost, and close it after the `Exit`.

## `orchestration.subscribeShell`

Request (`tests/fixtures/t3/subscribe_shell.json`):

```json
{"_tag": "Request", "id": "1", "tag": "orchestration.subscribeShell",
 "payload": {"omitIdleSubagents": true, "omitOlderSettled": true}, "headers": []}
```

These two options leave out subagents that are idle and threads settled long ago
(`isOlderSettledThreadShell`). Neither kind ever gets a key. On this machine they shrink the
snapshot from 2.1 MB (810 threads) to about 120 kB (29 threads). A thread that comes back,
such as one that gets unsettled or a subagent that starts running, arrives as
`thread.updated`.

Stream items (`OrchestrationV2ShellStreamItem`):

| `kind` | Meaning | Client |
|---|---|---|
| `snapshot` **without** `resolvedRepositoryIdentityRoots` | the authoritative snapshot: `snapshot.threads`, `snapshot.archivedThreads` (always empty here), `snapshot.projects`, `snapshotSequence` | replaces the thread list |
| `snapshot` **with** `resolvedRepositoryIdentityRoots` | a project-metadata refresh with `threads: []` | ignored (it must not clear the list) |
| `synchronized` | sent only when the request sets `requestCompletionMarker` | ignored |
| `project.updated` / `project.removed` | project changes | ignored |
| `thread.updated` with `location: "active"` | full new thread shell | upsert |
| `thread.updated` with `location: "archive"` | the thread moved to the archive | removed |
| `thread.removed` (`threadId`) | thread deleted | removed |

The first chunk holds the authoritative snapshot, followed in the same chunk by the
project-only refresh (`tests/fixtures/t3/shell_snapshot.json`). A recorded update is in
`tests/fixtures/t3/shell_thread_updated.json`. Trimmed to the fields the deck reads, it
looks like this:

```json
{"_tag": "Chunk", "requestId": "1", "values": [{
  "kind": "thread.updated", "sequence": 906703, "location": "active",
  "thread": {
    "id": "1cca8a80-c3a4-48a5-bfe0-87c810cb2f91",
    "projectId": "6046f0c3-9e68-46fc-bcd2-974c52d24cd3",
    "title": "Thread 27",
    "modelSelection": {"instanceId": "claudeAgent", "model": "claude-opus-5-5",
                       "options": [{"id": "effort", "value": "xhigh"}, {"id": "fastMode", "value": false}]},
    "lineage": {"parentThreadId": null, "relationshipToParent": null, "rootThreadId": "1cca8a80-…"},
    "status": "completed", "activityRunStatus": null,
    "pendingRuntimeRequest": null,
    "settledOverride": "settled", "settledAt": "2026-10-28T05:13:40.204Z",
    "archivedAt": null, "updatedAt": "2026-10-28T19:23:47.992Z"
  }}]}
```

### Thread shell to `Thread`

| `Thread` field | From the shell |
|---|---|
| `thread_id`, `project_id`, `title` | `id`, `projectId`, `title` |
| `status` | `activityRunStatus ?? status`, the same as `t3_thread_list` and `t3 thread list` (`OrchestratorMcpService.ts`) |
| `pending_requests` | 1 if `pendingRuntimeRequest` waits on the user, otherwise 0 (see below) |
| `instance_id`, `model`, `options` | `modelSelection.instanceId`, `.model`, `.options` as `(id, value)` pairs (`()` when absent) |
| `settled` | `settledOverride == "settled"` (the MCP's `threadSettlement`) |
| `archived` | `archivedAt != null` |
| `parent_thread_id` | `lineage.parentThreadId` |
| `updated_at` | `updatedAt` (ISO 8601 with `Z`) |

`pendingRuntimeRequest` is `{id, kind, createdAt, blocking?}` or `null`. The deck counts it the
way T3's own sidebar does (`packages/shared/src/agentAwareness.ts`):

- `kind: "user_input"` counts unless `blocking: false` and the run is still active
  (`preparing`, `starting` or `running`).
- An `auth_refresh` does not count.
- Any other kind is an approval (`command`, `file-read`, `file-change`, `mcp-elicitation`,
  `permission`, `dynamic_tool_call`) and counts.

A shell that fails to parse is skipped with a warning, and the rest of the list is kept.

## `orchestration.getThreadFamily`

```json
{"_tag": "Request", "id": "1", "tag": "orchestration.getThreadFamily",
 "payload": {"threadId": "1cca8a80-c3a4-48a5-bfe0-87c810cb2f91"}, "headers": []}
```

The answer is `Exit` with `{"snapshotSequence": n, "threads": [<thread shell>, …]}`. It holds
every active thread sharing the thread's `lineage.rootThreadId`: the thread itself, its forks
and its subagents (`tests/fixtures/t3/thread_family.json`). An unknown thread gets
`"threads": []` (`thread_family_unknown.json`). `get_model_selection` takes `modelSelection`
from the shell whose `id` matches. It took 20 to 70 ms and returned 25 to 70 kB.
`getThreadProjection` would also have worked, but it carries the whole transcript.

## `orchestration.dispatchCommand` with `thread.model-selection.set`

Request (`tests/fixtures/t3/set_model_selection.json`, where `<any>` is generated):

```json
{"_tag": "Request", "id": "1", "tag": "orchestration.dispatchCommand",
 "payload": {"type": "thread.model-selection.set", "commandId": "deck:<uuid4>",
             "threadId": "1cca8a80-c3a4-48a5-bfe0-87c810cb2f91",
             "modelSelection": {"instanceId": "claudeAgent", "model": "claude-opus-5-5",
                                "options": [{"id": "effort", "value": "high"}, {"id": "fastMode", "value": false}]}},
 "headers": []}
```

- Success: `{"_tag":"Exit","requestId":"1","exit":{"_tag":"Success","value":{"sequence":906703}}}`
  (`dispatch_command_success.json`, 19 ms).
- Rejected: `Exit` with
  `cause: [{"_tag":"Fail","error":{"_tag":"OrchestrationV2DispatchCommandError","message":"No orchestration projection exists for thread …", …}}]`
  (`dispatch_command_failure.json`). The client raises `T3Error(<message>)` so the strip can
  show it.
- `commandId` makes the command idempotent: the server replays an id it has already seen. The
  deck sends a fresh `deck:<uuid4>` every time.
- `modelSelection` replaces the thread's whole selection. The deck always sends every option
  (`ModelSelection.to_wire`), which keeps the ones it does not change.
- The same `instanceId` means `thread.model-selection.set`. A different one would need
  `provider.switch`, which the deck never sends: model lists are per instance.

### Live verification

Under the live-server safety rule, the set path was checked with a single no-op only: it
re-sent the selection a thread already had. The thread was `1cca8a80…`: completed, settled,
not the controller's session. Reading the thread back afterwards showed the same
`instanceId`, `model` and `options`, and its status was still `completed` and settled. No
real thread's model or effort was changed, and no thread or message was created.

## Limitations

- **A set touches `updatedAt`, even a no-op one.** The server writes `updatedAt: now` on every
  `thread.model-selection.set`. This was seen in the no-op above. The thread moves to the
  front of the most-recently-updated slot order.
- **`pending_requests` is 0 or 1.** The shell names only the oldest pending request. The exact
  count (`pendingRequestCount` of `t3 thread show`) comes from the per-thread projection,
  which the deck does not fetch. Key state only needs to know whether there are any.
- **The thread list is not exhaustive.** Older settled threads and idle subagents are left
  out (see `SHELL_OPTIONS`), and archived threads are dropped. None of them gets a key.
- **The protocol is pinned to version 2.** A newer server answers 426. `watch` then logs it,
  stays offline and keeps retrying with backoff. The RPC is T3's internal client protocol, not
  a public API, so the fixtures pin v0.1.3's shapes.
- **Liveness.** `watch` sends `Ping` after `interval` (1 s) without a message. It drops a
  connection that is silent for 15 s, or that sends no snapshot within 60 s of subscribing.
  Then it reconnects with 1 s doubling to 30 s backoff, and resets the backoff on every
  snapshot.

## Fixtures (`tests/fixtures/t3/`)

The fixtures are real frames from the live server. Thread titles became `Thread N` and
project titles `Project N`. Branch names other than `main` became `t3/branch-N`, because T3
derives them from thread titles. Workspace paths and worktree paths were replaced. Goals, error
texts, pull-request titles and links, repository identities and project scripts were
removed. No token appears in them. Thread and project ids are real.
`set_model_selection.json` is the recorded no-op request with `effort` set to `high` (what
the test sends) and the generated `id`/`commandId` replaced by `<any>`.

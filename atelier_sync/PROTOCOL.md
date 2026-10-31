# Atelier Sync protocol 1

All API calls are `POST` with `Content-Type: application/json`. Text is UTF-8. Responses use `Cache-Control: no-store`. Errors use HTTP 4xx/5xx with `{protocol:1,code,error}`; `error` is a Romanian message. A transient 500 creates no committed receipt, and the client may retry the same operation IDs. Body limit: 32 MiB. Sync request limit: 50 operations. Canonical UTF-8 agenda state is limited to 30 MiB; bootstrap or state growth beyond this limit is rejected before commit, so accepted data remains pullable.

## Status and registration

`POST /v1/status {}` is public and returns only `{protocol:1,capabilities:["suppliers-v1","client-deletion-v1"],workspaceId,initialized}`. `GET /` is a minimal public health page with no agenda data.

`POST /v1/register` accepts `{pairingKey,deviceId,role,name}`. `role` is `master` or `secondary`. IDs are nonempty strings up to 100 characters, and names are nonempty strings up to 150 characters. A valid configured pairing key is required. Re-registering an existing ID returns the same token; an authorized registration can update its name and role. Secondary devices can register before initialization.

The result is `{protocol:1,capabilities:["suppliers-v1","client-deletion-v1"],workspaceId,token,initialized}`. Authentication for sync is `Authorization: Bearer <token>`, bound to the registered `deviceId`. Changing the pairing key does not revoke prior tokens; deleting a device association does.

Atelier Sync 0.3.0 advertises `suppliers-v1` and `client-deletion-v1` on status, registration, and synchronization responses. New Android clients include `capabilities:["suppliers-v1","client-deletion-v1"]` on their requests and require this response capability before registration, bootstrap, joining, uploading operations, or accepting a reply. If it is absent, they must keep the local agenda and outbox intact and ask the user to update the HA application. This prevents older servers from accepting and stripping supplier information or deleted-client markers. Update every device before using client deletion: older clients do not understand deleted identities even though ordinary legacy operations remain supported. Older clients remain usable with ordinary legacy records; they are not globally rejected by the new server.

## Synchronization

```json
{
  "deviceId": "device-uuid",
  "workspaceId": "workspace-id-from-status",
  "operations": [
    {
      "id": "operation-uuid",
      "createdAt": "2026-10-31T10:00:00.000Z",
      "changes": [
        {"key": "expenses/expense-uuid", "before": null, "after": {"id":"expense-uuid","date":"2026-10-31","amount":12.34,"description":"Consumabile","createdAt":"2026-10-31T10:00:00.000Z","updatedAt":"2026-10-31T10:00:00.000Z"}}
      ]
    }
  ]
}
```

An empty `operations` array pulls the latest agenda. `workspaceId` must match; clients must never silently adopt a different workspace.

Entity keys are exactly `settings`, `meta`, `clients/<id>`, `services/<id>`, `appointments/<id>`, `expenses/<id>`, and `suppliers/<id>`. `meta` is `{demo:boolean}`. `settings` and `meta` cannot be deleted. A collection entity's record `id` must equal the suffix of its key. `null` means absence or deletion. Changes within one operation must use distinct keys.

The optional schema-1 `suppliers` catalog contains at most 10,000 `{id,name}` records. IDs are unique nonempty strings of at most 100 UTF-16 code units. Names are nonempty trimmed strings of at most 150 UTF-16 code units; duplicate names remain valid for concurrent offline additions. An expense may contain the optional pair `supplierId` and `supplierName`; both must be present or both absent. The ID must reference a catalog entry. `supplierName` is a nonempty trimmed historical name of at most 150 UTF-16 code units and may differ from the current catalog name. Renaming a supplier never changes purchase snapshots. Legacy expenses keep both fields absent. A referenced supplier cannot be deleted unless the same atomic operation removes or updates all its references.

A client may include the optional strictly boolean `deleted` field; absent and explicit `false` remain distinct CAS values and legacy absence is preserved. If `deleted:true`, `archived` must normalize to `true`, while the phone and contact notes must be empty. The original client ID and name remain available to every current appointment and correction snapshot. Physical removal is valid only when no current or audit snapshot references the ID; there is no automatic cascade deletion.

After initialization, an existing deleted identity cannot be revived by removing or clearing `deleted`. A candidate appointment whose current `clientId` is deleted is accepted only if that appointment ID already existed with the same `clientId` immediately before the atomic operation. New appointments and reassignment to a deleted identity are rejected for every status, including cancellation. Existing same-client appointments may change status, schedule, price or other fields under the existing overlap and completed-record audit rules. A valid first bootstrap may contain deleted identities and their historical appointments; the transition guard is intentionally not applied to initial recovery. A stale client whose `before` omits a stored marker receives a normal CAS conflict; an exact `before` followed by attempted revival is rejected atomically.

Each `before` must equal the live entity by semantic JSON comparison: object key order is irrelevant, array order is retained, and booleans are distinct from numbers. All changes in an operation are validated together against the aggregate agenda. Client references, schema 1, money, audit history, and overlapping noncancelled bookings are checked. Dates and local `HH:mm` values are stored as entered; no server-clock restriction is imposed on offline history.

Operation IDs are global and stable. A canonical payload hash sorts object keys and retains array order. An exact retry from the same device returns its persisted result without applying the operation again. Reusing an ID with another payload or device is rejected. Conflict and rejection receipts are durable too; a consciously resolved/rebased change needs a new operation ID.

All changes and their receipt commit atomically. Independent later operations in the same request may apply after an earlier conflict. Clients must hold back operations that depend on conflicted local state.

```json
{
  "protocol": 1,
  "capabilities": ["suppliers-v1","client-deletion-v1"],
  "workspaceId": "workspace-id",
  "initialized": true,
  "serverRevision": 2,
  "results": [{"id":"operation-uuid","status":"applied","reason":"Modificările au fost salvate pe server.","keys":["expenses/expense-uuid"]}],
  "state": {"version":1,"demo":false,"settings":{},"clients":[],"services":[],"appointments":[],"expenses":[],"suppliers":[]}
}
```

The abbreviated `state.settings` above is illustrative; real responses contain the complete validated settings. Result status is `applied`, `conflict`, or `rejected`. Results include all changed keys. `serverRevision` is a nonnegative JavaScript-safe integer: initialization starts at 1, each newly applied operation increments it once, and retries/conflicts/rejections do not increment it. A lower revision on the same workspace means server recovery/rollback and must be surfaced explicitly by the client.

The canonical server agenda always contains `expenses:[]` and `suppliers:[]`, including when the initial legacy agenda omitted these collections. Adding an empty suppliers collection does not change existing entity CAS values or require rewriting the database. An old client submitting a supplier-bearing expense with its supplier fields stripped from `before` gets a normal conflict against the complete server entity; no purchase snapshot is silently lost. Optional legacy service durations, appointment colors/corrections, and agenda-range revisions remain supported. The server uses its own SQLite database, not a shared filesystem copy of the Android database.

## First initialization and lost responses

A master-role device may add top-level `bootstrap:<schema-1 agenda>` and `bootstrapId:<stable unique ID>` to a sync request. The client must persist the ID and frozen snapshot before sending it. A secondary device cannot initialize the database. A pull before initialization returns `409 needs_initialization`.

The server atomically records the initializer device, bootstrap ID, snapshot hash, state, and first revision. If the response is lost, the same device can resend the same ID and snapshot, receiving ordinary current state. The retry does not reset later changes. The same ID with altered data returns `409 bootstrap_id_reused`; a different initialization on an existing store returns `409 already_initialized`. A client connecting to an existing store must explicitly adopt its snapshot after preserving local data, rather than attempting a silent bootstrap overwrite.

## Operational errors

`401` covers bad pairing keys or missing/invalid bearer tokens. `403` covers a token/device mismatch or a secondary initialization attempt. `409` covers workspace mismatch, missing initialization, and initialization reuse errors. `400` covers malformed JSON/request shape. `413` covers the body limit. Unknown endpoints return `404`.

Registration, synchronization, and backup operations never need Home Assistant administrator tokens or its API.

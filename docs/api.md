# API reference

Every route, who may call it, and what it answers. Interactive docs are
served at <http://localhost:8000/docs> while the stack is up.

[← back to the README](../README.md)

---

| Route | Who | Returns |
| --- | --- | --- |
| `POST /media` | any member | `202` `{"id","status":"pending"}`; `422` `{"reasons"}` when a generation prompt is refused |
| `GET /media/{id}` | members of the owning workspace | `200`; `404` for anyone else |
| `GET /review` | admins | `200` with the held media of their workspace; `503` when the queue cannot be read |
| `POST /review/{id}/approve` | admins | `200`; `403` for members; `409` if not held, never screened, or an absolute rule forbids it |
| `POST /review/{id}/reject` | admins | `200`; `403` for members |
| `POST /provider/callback` | the provider | `200`; `401` without a valid signature |
| `GET /_metrics` | ops | counts, unchecked publishes, oldest waiting job |
| `POST /_admin/reset` | ops | clears media, jobs and queue; resets the provider's counters |
| `GET /healthz` | ops | liveness and active screening mode |

Provider control: `POST localhost:8001/_control` with
`{"mode":"ok"|"slow"|"timeout"|"error","delay_ms":0}` (also `rate_limit` and
`malformed`, used by the tests), and `GET localhost:8001/_stats` for per-item
call counts. Interactive docs at <http://localhost:8000/docs>.


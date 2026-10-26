import { useCallback, useEffect, useState } from "react";
import {
  API_BASE,
  ApiError,
  type HeldItem,
  IDENTITIES,
  type Identity,
  SAMPLES,
  fetchReviewQueue,
  review,
  upload,
} from "./api";

/** The three states the brief requires this page to tell apart, as a type.
 *
 *  Modelling them as a discriminated union rather than as `items` plus a
 *  `loading` flag plus an `error` string is deliberate: there is no combination
 *  of flags that can render a failure as an empty list, because a failure
 *  carries no items at all. "Empty" is `ready` with nothing in it, which is a
 *  different thing from `failed`, and the compiler enforces the distinction. */
type QueueState =
  | { kind: "loading" }
  | { kind: "failed"; status: number | null; message: string }
  | { kind: "ready"; items: HeldItem[] };

export default function App() {
  const [who, setWho] = useState<Identity>(IDENTITIES[0]);
  const [state, setState] = useState<QueueState>({ kind: "loading" });
  const [notice, setNotice] = useState<string>("");
  const [busy, setBusy] = useState<string>("");

  const load = useCallback(async (identity: Identity) => {
    setState({ kind: "loading" });
    try {
      const items = await fetchReviewQueue(identity);
      setState({ kind: "ready", items });
    } catch (error) {
      const apiError = error instanceof ApiError ? error : null;
      setState({
        kind: "failed",
        status: apiError?.status ?? null,
        message: apiError?.message ?? String(error),
      });
    }
  }, []);

  useEffect(() => {
    void load(who);
  }, [who, load]);

  async function act(mediaId: string, action: "approve" | "reject") {
    setBusy(mediaId);
    setNotice("");
    try {
      await review(who, mediaId, action);
      setNotice(`${action}d ${mediaId.slice(0, 8)}`);
    } catch (error) {
      const apiError = error instanceof ApiError ? error : null;
      // A 409 here is the system refusing to let an admin override an absolute
      // rule, or to approve something that was never screened. Worth showing
      // verbatim rather than as a generic failure.
      setNotice(apiError ? `refused (${apiError.status}): ${apiError.message}` : String(error));
    } finally {
      setBusy("");
      void load(who);
    }
  }

  async function send(sample: keyof typeof SAMPLES) {
    setNotice("");
    try {
      setNotice(await upload(who, sample));
    } catch (error) {
      setNotice(String(error));
    }
    window.setTimeout(() => void load(who), 1200);
  }

  return (
    <main>
      <header>
        <h1>Review queue</h1>
        <p className="sub">
          Held media awaiting an admin decision · <code>{API_BASE}</code>
        </p>
      </header>

      <section className="bar">
        <label>
          Acting as{" "}
          <select
            value={who.userId}
            onChange={(event) =>
              setWho(IDENTITIES.find((i) => i.userId === event.target.value) ?? IDENTITIES[0])
            }
          >
            {IDENTITIES.map((identity) => (
              <option key={identity.userId} value={identity.userId}>
                {identity.label}
              </option>
            ))}
          </select>
        </label>
        <button onClick={() => void load(who)}>Reload</button>
      </section>

      <section className="bar samples">
        <span className="muted">Create sample media:</span>
        {Object.entries(SAMPLES).map(([key, sample]) => (
          <button key={key} className="ghost" onClick={() => void send(key)}>
            {sample.label}
          </button>
        ))}
      </section>

      {notice && <p className="notice">{notice}</p>}

      <Queue state={state} busy={busy} onAct={act} canAct={who.role === "admin"} />

      <footer>
        <p className="muted">
          This page distinguishes three states on purpose: <strong>loading</strong>,{" "}
          <strong>failed</strong>, and <strong>genuinely empty</strong>. During the incident a
          failed request was rendered as an empty list, so moderators saw &ldquo;Nothing to
          review&rdquo; for a morning while 40 items waited. Set <code>REVIEW_FAIL=1</code> on the
          API to see the failed state.
        </p>
      </footer>
    </main>
  );
}

function Queue({
  state,
  busy,
  canAct,
  onAct,
}: {
  state: QueueState;
  busy: string;
  canAct: boolean;
  onAct: (mediaId: string, action: "approve" | "reject") => void;
}) {
  if (state.kind === "loading") {
    return (
      <div className="panel loading" data-testid="state-loading">
        <h2>Loading…</h2>
        <p>Asking the API for this workspace&rsquo;s held media.</p>
      </div>
    );
  }

  if (state.kind === "failed") {
    return (
      <div className="panel failed" data-testid="state-failed">
        <h2>Could not load the queue</h2>
        <p>{state.message}</p>
        <p className="muted">
          {state.status === null ? "No response" : `HTTP ${state.status}`} — this is
          <strong> not</strong> an empty queue. There may be items waiting that cannot be shown.
        </p>
      </div>
    );
  }

  if (state.items.length === 0) {
    return (
      <div className="panel empty" data-testid="state-empty">
        <h2>Nothing to review</h2>
        <p>The queue loaded successfully and no media is currently held.</p>
      </div>
    );
  }

  return (
    <div data-testid="state-ready">
      <h2>
        {state.items.length} item{state.items.length === 1 ? "" : "s"} held
      </h2>
      <table>
        <thead>
          <tr>
            <th>id</th>
            <th>kind</th>
            <th>reasons</th>
            <th>frames</th>
            <th>screened</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {state.items.map((item) => (
            <tr key={item.id}>
              <td>
                <code>{item.id.slice(0, 10)}</code>
              </td>
              <td>
                {item.type} / {item.source}
                {item.prompt && <div className="muted prompt">{item.prompt}</div>}
              </td>
              <td>
                {item.reasons.map((reason) => (
                  <span
                    key={reason}
                    className={`tag ${reason.startsWith("screening_unavailable") ? "warn" : ""}`}
                  >
                    {reason}
                  </span>
                ))}
              </td>
              <td>
                {item.frames_scored}/{item.frames_total}
              </td>
              <td>
                {item.screened ? (
                  <span className="tag ok">yes</span>
                ) : (
                  <span className="tag warn">no</span>
                )}
              </td>
              <td className="actions">
                <button
                  disabled={!canAct || busy === item.id}
                  onClick={() => onAct(item.id, "approve")}
                  title={canAct ? "Publish this item" : "Members cannot approve"}
                >
                  Approve
                </button>
                <button
                  className="danger"
                  disabled={!canAct || busy === item.id}
                  onClick={() => onAct(item.id, "reject")}
                >
                  Reject
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted">
        An item with <code>screened: no</code> is held because screening was unavailable, not
        because a human needs to judge it. It has no provider decision behind it, so approving it
        is refused with <code>409</code>; the re-screening sweep will take it when the provider
        recovers.
      </p>
    </div>
  );
}

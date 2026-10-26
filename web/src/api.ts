const BASE: string = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export type Role = "admin" | "member";

export interface Identity {
  userId: string;
  workspaceId: string;
  role: Role;
  label: string;
}

/** The seeded identities. There is no login; these two headers are the whole
 *  auth model, so the page just lets you pick who you are. */
export const IDENTITIES: Identity[] = [
  { userId: "admin_v", workspaceId: "ws_verified", role: "admin", label: "admin_v · verified" },
  { userId: "member_v", workspaceId: "ws_verified", role: "member", label: "member_v · verified" },
  { userId: "admin_u", workspaceId: "ws_unverified", role: "admin", label: "admin_u · unverified" },
  { userId: "member_u", workspaceId: "ws_unverified", role: "member", label: "member_u · unverified" },
];

export interface HeldItem {
  id: string;
  status: string;
  reasons: string[];
  type: string;
  source: string;
  prompt: string | null;
  explicit: boolean;
  frames_total: number;
  frames_scored: number;
  screened: boolean;
  created_at: string;
}

/** An API failure that carries its status, so the UI can say *how* it failed
 *  rather than collapsing everything into "something went wrong". */
export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function authHeaders(who: Identity): HeadersInit {
  return { "X-User-Id": who.userId, "X-Workspace-Id": who.workspaceId };
}

async function describeFailure(response: Response): Promise<string> {
  let detail = "";
  try {
    const body = (await response.json()) as Record<string, unknown>;
    detail = String(body.error ?? JSON.stringify(body.detail ?? body));
  } catch {
    detail = response.statusText;
  }
  switch (response.status) {
    case 401:
      return "Not identified. The request carried no usable X-User-Id / X-Workspace-Id.";
    case 403:
      return "Only workspace admins can read the review queue.";
    case 503:
      return `The review queue could not be read (${detail}). This is a failure, not an empty queue.`;
    default:
      return `HTTP ${response.status}: ${detail}`;
  }
}

/** Fetch the held media of the caller's workspace.
 *
 *  Throws on any non-2xx. That matters: the bug this page exists to prevent was
 *  a failed request being rendered as an empty list, so a failure must never be
 *  able to reach the UI looking like data. */
export async function fetchReviewQueue(who: Identity): Promise<HeldItem[]> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/review`, { headers: authHeaders(who) });
  } catch (cause) {
    throw new ApiError(
      `Could not reach the API at ${BASE}. Is it running? (${String(cause)})`,
      null,
    );
  }
  if (!response.ok) {
    throw new ApiError(await describeFailure(response), response.status);
  }
  const body: unknown = await response.json();
  if (!Array.isArray(body)) {
    throw new ApiError("The API returned something that is not a list of items.", response.status);
  }
  return body as HeldItem[];
}

export async function review(
  who: Identity,
  mediaId: string,
  action: "approve" | "reject",
): Promise<void> {
  const response = await fetch(`${BASE}/review/${mediaId}/${action}`, {
    method: "POST",
    headers: authHeaders(who),
  });
  if (!response.ok) {
    throw new ApiError(await describeFailure(response), response.status);
  }
}

/** Sample uploads, so the queue can be demonstrated without reaching for curl. */
export const SAMPLES: Record<string, { label: string; body: unknown }> = {
  held: {
    label: "held for review (minor 0.6)",
    body: { type: "image", source: "upload", frames: [{ minor: 0.6 }] },
  },
  blocked: {
    label: "blocked (minor 0.9)",
    body: { type: "image", source: "upload", frames: [{ minor: 0.9 }] },
  },
  clean: {
    label: "clean (publishes)",
    body: { type: "image", source: "upload", frames: [{ explicit: 0.1 }] },
  },
  longVideo: {
    label: "60s video, explicit at the end",
    body: {
      type: "video",
      source: "upload",
      frames: [...Array(59).fill({ explicit: 0.01 }), { explicit: 0.95 }],
    },
  },
  refusedPrompt: {
    label: "generation prompt that must be refused",
    body: {
      type: "image",
      source: "generation",
      prompt: "a 16-year-old girl",
      frames: [{ explicit: 0.1 }],
    },
  },
};

export async function upload(who: Identity, sample: keyof typeof SAMPLES): Promise<string> {
  const response = await fetch(`${BASE}/media`, {
    method: "POST",
    headers: { ...authHeaders(who), "Content-Type": "application/json" },
    body: JSON.stringify(SAMPLES[sample].body),
  });
  const body = (await response.json()) as Record<string, unknown>;
  if (response.status === 422) {
    // A refused generation prompt. Not an error -- the expected answer.
    return `refused: ${JSON.stringify(body.reasons)}`;
  }
  if (!response.ok) {
    throw new ApiError(`HTTP ${response.status}: ${JSON.stringify(body)}`, response.status);
  }
  return `accepted: ${String(body.id)} (${String(body.status)})`;
}

export const API_BASE = BASE;

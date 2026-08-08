export type Me = {
  open_id: string;
  name: string;
  avatar_url: string;
  role: "USER" | "ADMIN";
  expires_at: string;
};

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

function cookie(name: string): string {
  const value = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${name}=`));
  return value ? decodeURIComponent(value.slice(name.length + 1)) : "";
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", cookie("lark_ledger_csrf"));
  }
  const response = await fetch(`/api/web/v1${path}`, {
    ...init,
    headers,
    credentials: "same-origin",
  });
  if (!response.ok) {
    const payload = (await response.json().catch(() => null)) as {
      detail?: string;
    } | null;
    throw new ApiError(response.status, payload?.detail ?? "请求失败，请稍后重试");
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

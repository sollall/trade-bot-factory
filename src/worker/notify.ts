import type { Env } from "./env";

/** Discord / Slack 互換の Webhook に送る。失敗しても本処理は止めない */
export async function notify(env: Env, message: string): Promise<void> {
  if (!env.WEBHOOK_URL) return;
  const text = `[${env.MODE}] ${message}`;
  try {
    await fetch(env.WEBHOOK_URL, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ content: text, text }),
    });
  } catch (e) {
    console.error("notify failed", e);
  }
}

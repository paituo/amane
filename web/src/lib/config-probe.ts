/**
 * 配置自检的流式读取.
 *
 * 端点返回 NDJSON (一行一个事件, 最后一行 `done: true`). 不引 SSE: 这里只有单向输出、没有
 * 重连语义, 一个 reader 循环就够; 而"正在下载模型"与"卡住了"在界面上必须能区分,
 * 所以必须边收边显示, 不能等整段结束.
 */

import { apiFetch } from "@/lib/api-token";
import { extractErrorMessage } from "@/lib/api-error";

export type ProbeLevel = "info" | "ok" | "warn" | "error";

export interface ProbeEvent {
  seq: number;
  level: ProbeLevel;
  message: string;
  detail?: string | null;
  elapsed_ms: number;
  step_ms: number;
}

export interface ProbeOutcome {
  ok: boolean;
  elapsed_ms: number;
}

interface ProbeRequest {
  section: string;
  /** 该 section 的表单值; 传了就按"未保存的改动"测, 服务端只校验与合并, 不落盘. */
  values?: Record<string, unknown> | null;
  signal?: AbortSignal;
  onEvent: (event: ProbeEvent) => void;
}

const API_BASE = import.meta.env.VITE_API_URL || "";

async function failureDetail(resp: Response): Promise<string> {
  const fallback = `HTTP ${resp.status}`;
  try {
    return extractErrorMessage(await resp.json(), fallback);
  } catch {
    return fallback;
  }
}

export async function runConfigProbe({
  section,
  values,
  signal,
  onEvent,
}: ProbeRequest): Promise<ProbeOutcome> {
  const resp = await apiFetch(`${API_BASE}/api/config/probe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ section, values: values ?? null }),
    signal,
  });
  if (!resp.ok) {
    throw new Error(await failureDetail(resp));
  }
  if (!resp.body) {
    throw new Error("响应没有可读的流");
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let outcome: ProbeOutcome | null = null;

  const consume = (line: string): void => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const payload = JSON.parse(trimmed) as Record<string, unknown>;
    if (payload.done === true) {
      outcome = { ok: payload.ok === true, elapsed_ms: Number(payload.elapsed_ms ?? 0) };
      return;
    }
    onEvent(payload as unknown as ProbeEvent);
  };

  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let newline = buffer.indexOf("\n");
    while (newline >= 0) {
      consume(buffer.slice(0, newline));
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf("\n");
    }
  }
  // 末行可能没有换行符, 不能因此丢掉 done.
  buffer += decoder.decode();
  consume(buffer);

  const finished: ProbeOutcome | null = outcome;
  if (finished === null) {
    throw new Error("测试流在给出结论前中断");
  }
  return finished;
}

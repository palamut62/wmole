import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { writable } from "svelte/store";
import type { Request, SidecarEvent } from "./types";

type Handler = (e: SidecarEvent) => void;
const handlers = new Map<string, Handler>();
let counter = 0;

export const connected = writable(false);

/** Uygulama geneli canlı durum: aktif istek sayısı + son ilerleme bilgisi. */
export const activity = writable<{
  busy: boolean;
  op: string;
  label: string;
  done: number;
  total: number;
}>({ busy: false, op: "", label: "", done: 0, total: 0 });

let inflight = 0;

function startActivity(op: string) {
  inflight += 1;
  activity.update((a) => ({ ...a, busy: true, op }));
}
function endActivity() {
  inflight = Math.max(0, inflight - 1);
  if (inflight === 0) activity.set({ busy: false, op: "", label: "", done: 0, total: 0 });
}

listen<string>("sidecar-event", (msg) => {
  let e: SidecarEvent;
  try {
    e = JSON.parse(msg.payload);
  } catch {
    return;
  }
  if (e.ev === "ready") {
    connected.set(true);
    return;
  }
  if (e.ev === "progress") {
    activity.update((a) => ({
      ...a,
      busy: true,
      label: String((e as any).label ?? ""),
      done: Number((e as any).done ?? 0),
      total: Number((e as any).total ?? 0),
    }));
  }
  if (e.id && handlers.has(e.id)) {
    handlers.get(e.id)!(e);
    if (e.ev === "done") handlers.delete(e.id);
  }
});

export function nextId(prefix = "req"): string {
  counter += 1;
  return `${prefix}-${counter}`;
}

/** İstek gönder; her olay için onEvent çağrılır. done'da Promise çözülür ve done olayını döner. */
export function request(
  req: Omit<Request, "id">,
  onEvent?: Handler,
): Promise<SidecarEvent> {
  const id = nextId(req.op);
  const full: Request = { ...req, id };
  startActivity(req.op);
  return new Promise((resolve) => {
    handlers.set(id, (e) => {
      onEvent?.(e);
      if (e.ev === "done") {
        endActivity();
        resolve(e);
      }
    });
    invoke("send_request", { line: JSON.stringify(full) });
  });
}

/** Çalışan bir isteği iptal et. */
export function cancel(id: string) {
  invoke("send_request", { line: JSON.stringify({ id, op: "cancel" }) });
}

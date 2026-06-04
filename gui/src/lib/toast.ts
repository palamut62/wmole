import { writable } from "svelte/store";

export interface Toast {
  id: number;
  text: string;
  kind: "ok" | "err" | "info";
}
export const toasts = writable<Toast[]>([]);
let n = 0;

export function toast(text: string, kind: Toast["kind"] = "info") {
  const id = ++n;
  toasts.update((t) => [...t, { id, text, kind }]);
  setTimeout(
    () => toasts.update((t) => t.filter((x) => x.id !== id)),
    4000,
  );
}

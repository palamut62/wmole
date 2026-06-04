export type Op =
  | "ping"
  | "status"
  | "scan"
  | "delete"
  | "cancel"
  | "uninstall_list"
  | "leftovers"
  | "uninstall_run"
  | "optimize_list"
  | "optimize_run"
  | "ports_list"
  | "ports_kill"
  | "update"
  | "remove";

export interface Request {
  id: string;
  op: Op;
  mode?: "analyze" | "clean" | "purge" | "installers";
  paths?: string[];
  targets?: string[];
  permanent?: boolean;
  dry_run?: boolean;
  query?: string;
  limit?: number;
  app?: unknown;
  uninstall?: string;
  key?: string;
  keys?: string[];
  pid?: number;
  pids?: number[];
  all_binds?: boolean;
  yes?: boolean;
}

export interface SidecarEvent {
  id: string | null;
  ev: "ready" | "started" | "progress" | "item" | "item_result" | "done" | "error";
  [key: string]: unknown;
}

export interface ScanItem {
  path: string;
  name: string;
  size: number;
  kind: string;
  category?: string;
  selected: boolean;
}

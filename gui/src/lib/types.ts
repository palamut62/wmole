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
  | "remove"
  | "cleanup_history"
  | "drives"
  | "open_path"
  | "settings_get"
  | "settings_set"
  | "is_admin"
  | "completion_install"
  | "schedule_get"
  | "schedule_set"
  | "schedule_clear"
  | "startup_list"
  | "startup_disable"
  | "processes_list"
  | "duplicates";

export interface Request {
  id: string;
  op: Op;
  mode?: "analyze" | "clean" | "purge" | "installers" | "categories" | "large";
  paths?: string[];
  path?: string;
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
  whitelist?: string[];
  denylist?: string[];
  purge_paths?: string[];
  config?: Record<string, unknown>;
  day?: string;
  time?: string;
  name?: string;
  location?: string;
  min_size?: number;
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

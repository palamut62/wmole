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
  | "duplicates"
  | "security_overview"
  | "quarantine_restore"
  | "secret_scan"
  | "supply_audit"
  | "explain"
  | "disk_facts"
  | "journal"
  | "quarantine_verify";

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
  uninstall_key?: string;
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
  entry_id?: string;
  items?: { path: string; kind?: string }[];
  kind?: string;
  risk?: boolean;
  check_git?: boolean;
  budget_seconds?: number;
  recover?: boolean;
}

/** The six questions every cleanup candidate answers. */
export interface Explanation {
  /** What this is. */
  what: string;
  /** What disappears when it is deleted. */
  lost: string;
  /** What survives. */
  kept: string;
  /** Command that recreates it — displayed only, never executed. */
  rebuild: string;
  /** Whether `rebuild` genuinely restores it. */
  regenerable: boolean;
  /** When the space actually comes back. */
  when_freed: "immediate" | "quarantine-expiry" | "empty-recycle-bin" | "reboot";
  /** Whether the delete can be undone, and how. */
  reversible: string;
}

/** One named contribution to a risk score, so the number is explainable. */
export interface RiskModifier {
  code: string;
  points: number;
  label: string;
}

export interface Risk {
  score: number; // 0-100
  level: "low" | "moderate" | "high" | "critical";
  modifiers: RiskModifier[];
}

/** Logical vs. actually-allocated bytes for a path. */
export interface DiskFacts {
  /** Sum of st_size — not what a delete frees. */
  logical: number;
  /** Allocated bytes: what the drive really reclaims. */
  on_disk: number;
  /** Logical bytes that live only in the cloud and free nothing locally. */
  placeholder_bytes: number;
  placeholder_files: number;
  files: number;
  /** A measurement budget was hit; the figures are lower bounds. */
  truncated: boolean;
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
  /** Sizing hit its per-directory budget: `size` is a lower bound. */
  truncated?: boolean;
}

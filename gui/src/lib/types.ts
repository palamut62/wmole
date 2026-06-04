export type Op = "ping" | "status" | "scan" | "delete" | "cancel";

export interface Request {
  id: string;
  op: Op;
  mode?: "analyze" | "clean" | "purge" | "installers";
  paths?: string[];
  targets?: string[];
  permanent?: boolean;
  dry_run?: boolean;
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

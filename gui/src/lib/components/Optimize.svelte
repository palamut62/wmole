<script lang="ts">
  import { onMount } from "svelte";
  import { request } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import GenericConfirm from "./GenericConfirm.svelte";
  import StatusBar from "./StatusBar.svelte";
  import { toast } from "$lib/toast";
  import { t, tr } from "$lib/i18n";

  interface Action {
    path: string; // key
    name: string;
    description: string;
    risk: string;
    requires_admin: boolean;
    selected: boolean;
  }

  let actions = $state<Action[]>([]);
  let dryRun = $state(true);
  let confirmOpen = $state(false);
  let progress = $state({ done: 0, total: 0, label: "" });

  let selected = $derived(actions.filter((a) => a.selected));
  let hasHighRisk = $derived(selected.some((a) => a.risk === "high"));

  async function load() {
    const buf: Action[] = [];
    await request({ op: "optimize_list" }, (e: SidecarEvent) => {
      if (e.ev === "item") buf.push(e as unknown as Action);
    });
    actions = buf;
  }
  onMount(load);

  function toggle(a: Action) {
    a.selected = !a.selected;
    actions = [...actions];
  }

  function run() {
    if (!selected.length) return;
    if (!dryRun && hasHighRisk) {
      confirmOpen = true;
      return;
    }
    execute();
  }

  async function execute() {
    confirmOpen = false;
    const keys = selected.map((a) => a.path);
    let ok = 0,
      err = 0;
    await request({ op: "optimize_run", keys, dry_run: dryRun }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    progress = { done: 0, total: 0, label: "" };
    toast(
      `${dryRun ? tr("Dry-run:") + " " : ""}${ok} ${tr("işlem tamam")}${err ? `, ${err} ${tr("hata")}` : ""}`,
      err ? "err" : "ok",
    );
  }
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{$t("Optimize")}</h2>
    <label class="dry"><input type="checkbox" bind:checked={dryRun} /> {$t("Dry-run (önizleme)")}</label>
    <button onclick={run} disabled={!selected.length}>
      {dryRun ? $t("Önizle") : $t("Çalıştır")} ({selected.length})
    </button>
  </div>
  <div class="list">
    {#each actions as a (a.path)}
      <label class="entry" class:high={a.risk === "high"}>
        <input type="checkbox" checked={a.selected} onchange={() => toggle(a)} />
        <span class="title">{a.name}</span>
        <span class="desc">{a.description}</span>
        {#if a.risk === "high"}<span class="badge">{$t("YÜKSEK RİSK")}</span>{/if}
        {#if a.requires_admin}<span class="badge admin">{$t("ADMIN")}</span>{/if}
      </label>
    {/each}
  </div>
</div>

<GenericConfirm
  open={confirmOpen}
  danger
  title="Yüksek Riskli İşlemler"
  message={`${tr("Seçili işlemlerden bazıları YÜKSEK RİSKLİ ve sistemi etkileyebilir:")}\n\n${selected
    .filter((a) => a.risk === "high")
    .map((a) => "• " + a.name)
    .join("\n")}\n\n${tr("Devam edilsin mi?")}`}
  confirmLabel="Evet, Çalıştır"
  onConfirm={execute}
  onCancel={() => (confirmOpen = false)}
/>
<StatusBar label={progress.label} done={progress.done} total={progress.total} onCancel={null} />

<style>
  .scan { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; }
  .toolbar h2 { margin: 0; color: var(--fg); }
  .dry { color: var(--muted); display: flex; gap: 6px; align-items: center; }
  button {
    background: var(--btn); color: var(--fg); border: none; padding: 6px 14px;
    border-radius: 4px; cursor: pointer; font-family: monospace;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .list {
    flex: 1; min-height: 0; overflow: auto; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px;
  }
  .entry {
    display: flex; gap: 10px; align-items: center; padding: 8px 10px;
    border-radius: 4px; cursor: pointer;
  }
  .entry:hover { background: var(--border); }
  .title { color: var(--fg); min-width: 180px; }
  .desc { color: var(--faint); font-size: 12px; flex: 1; }
  .badge {
    background: #e5534b; color: white; font-size: 10px; padding: 2px 6px;
    border-radius: 3px;
  }
  .badge.admin { background: #d29922; }
</style>

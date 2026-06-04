<script lang="ts">
  import { onMount } from "svelte";
  import { request } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import GenericConfirm from "./GenericConfirm.svelte";
  import StatusBar from "./StatusBar.svelte";
  import { toast } from "$lib/toast";

  interface Port {
    path: string;
    name: string;
    port: number;
    pid: number | null;
    proto: string;
    process: string;
    ip: string;
    hint: string;
    selected: boolean;
  }

  let ports = $state<Port[]>([]);
  let allBinds = $state(false);
  let loading = $state(false);
  let confirmOpen = $state(false);
  let progress = $state({ done: 0, total: 0, label: "" });

  let selected = $derived(ports.filter((p) => p.selected && p.pid));

  async function load() {
    ports = [];
    loading = true;
    const buf: Port[] = [];
    await request({ op: "ports_list", all_binds: allBinds }, (e: SidecarEvent) => {
      if (e.ev === "item") buf.push(e as unknown as Port);
    });
    ports = buf;
    loading = false;
  }
  onMount(load);

  function toggle(p: Port) {
    p.selected = !p.selected;
    ports = [...ports];
  }

  async function doKill() {
    confirmOpen = false;
    const pids = [...new Set(selected.map((p) => p.pid!))];
    let ok = 0,
      err = 0;
    await request({ op: "ports_kill", pids, dry_run: false }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    progress = { done: 0, total: 0, label: "" };
    toast(`${ok} süreç kapatıldı${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
    load();
  }
</script>

<div class="scan">
  <div class="toolbar">
    <h2>Ports</h2>
    <label class="dry"><input type="checkbox" bind:checked={allBinds} onchange={load} /> Tüm bind'ler</label>
    <button onclick={load} disabled={loading}>{loading ? "…" : "Yenile"}</button>
    <button class="danger" onclick={() => (confirmOpen = true)} disabled={!selected.length}>
      Süreci Kapat ({selected.length})
    </button>
  </div>
  <div class="list">
    {#each ports as p (p.path)}
      <label class="entry">
        <input type="checkbox" checked={p.selected} onchange={() => toggle(p)} disabled={!p.pid} />
        <span class="port">{p.proto}/{p.port}</span>
        <span class="pid">pid {p.pid ?? "-"}</span>
        <span class="proc">{p.process || "-"}</span>
        <span class="hint">{p.hint}</span>
      </label>
    {/each}
    {#if !ports.length && !loading}
      <p class="empty">Dinleyen localhost portu yok (tümü için admin gerekebilir).</p>
    {/if}
  </div>
</div>

<GenericConfirm
  open={confirmOpen}
  danger
  title="Süreç Kapatma"
  message={`${selected.length} süreç sonlandırılacak (kill). Kaydedilmemiş veriler kaybolabilir. Devam?`}
  confirmLabel="Kapat"
  onConfirm={doKill}
  onCancel={() => (confirmOpen = false)}
/>
<StatusBar label={progress.label} done={progress.done} total={progress.total} onCancel={null} />

<style>
  .scan { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 14px; margin-bottom: 10px; }
  .toolbar h2 { margin: 0; color: #e6edf3; }
  .dry { color: #9aa7b4; display: flex; gap: 6px; align-items: center; }
  button {
    background: #243140; color: #e6edf3; border: none; padding: 6px 14px;
    border-radius: 4px; cursor: pointer; font-family: monospace;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  .list {
    flex: 1; min-height: 0; overflow: auto; background: #11161c;
    border: 1px solid #1b2530; border-radius: 8px; padding: 6px;
  }
  .entry { display: flex; gap: 12px; align-items: center; padding: 6px 10px; border-radius: 4px; }
  .entry:hover { background: #1b2530; }
  .port { color: #58d6a0; min-width: 90px; }
  .pid { color: #9aa7b4; min-width: 90px; }
  .proc { color: #e6edf3; min-width: 160px; }
  .hint { color: #6e7681; font-size: 12px; }
  .empty { color: #6e7681; padding: 12px; }
</style>

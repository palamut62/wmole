<script lang="ts">
  import { onMount } from "svelte";
  import { request } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import GenericConfirm from "./GenericConfirm.svelte";
  import { toast } from "$lib/toast";
  import { t } from "$lib/i18n";

  interface Proc { path: string; name: string; size: number; pid: number; cpu: number; selected: boolean; }
  let procs = $state<Proc[]>([]);
  let filter = $state("");
  let loading = $state(false);
  let confirmOpen = $state(false);

  let selected = $derived(procs.filter((p) => p.selected));
  let shown = $derived(filter ? procs.filter((p) => p.name.toLowerCase().includes(filter.toLowerCase())) : procs);

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + u[i];
  }

  async function load() {
    procs = []; loading = true;
    const buf: Proc[] = [];
    await request({ op: "processes_list" }, (e: SidecarEvent) => {
      if (e.ev === "item") buf.push(e as unknown as Proc);
    });
    procs = buf; loading = false;
  }
  onMount(load);

  function toggle(p: Proc) { p.selected = !p.selected; procs = [...procs]; }

  async function kill() {
    confirmOpen = false;
    const pids = selected.map((p) => p.pid);
    let ok = 0, err = 0;
    await request({ op: "ports_kill", pids, dry_run: false }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
    });
    toast(`${ok} süreç kapatıldı${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
    load();
  }
</script>

<div class="view">
  <div class="toolbar">
    <h2>{$t("İşlemler")}</h2>
    <input placeholder={$t("filtrele…")} bind:value={filter} />
    <button onclick={load} disabled={loading}>{loading ? "…" : $t("Yenile")}</button>
    <button class="danger" onclick={() => selected.length && (confirmOpen = true)} disabled={!selected.length}>{$t("Kapat")} ({selected.length})</button>
    <span class="count">{shown.length}/{procs.length} {$t("işlem")}</span>
  </div>
  <div class="list">
    <VirtualList items={shown} rowHeight={26}>
      {#snippet row(item)}
        <label class="entry">
          <input type="checkbox" checked={item.selected} onchange={() => toggle(item)} />
          <span class="mem">{fmt(item.size)}</span>
          <span class="cpu">{item.cpu?.toFixed(0) ?? 0}%</span>
          <span class="pid">{item.pid}</span>
          <span class="name">{item.name}</span>
        </label>
      {/snippet}
    </VirtualList>
  </div>
</div>

<GenericConfirm open={confirmOpen} danger title="Süreç Kapatma"
  message={`${selected.length} süreç sonlandırılacak. Kaydedilmemiş veriler kaybolabilir. Devam?`}
  confirmLabel="Kapat" onConfirm={kill} onCancel={() => (confirmOpen = false)} />

<style>
  .view { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  h2 { margin: 0; color: var(--fg); }
  input { background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 5px 10px; border-radius: 4px; font-family: monospace; }
  button { background: var(--btn); color: var(--fg); border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; }
  button.danger { background: #e5534b; color: white; }
  .count { color: var(--muted); font-size: 12px; }
  .list { flex: 1; min-height: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .entry { display: flex; gap: 12px; align-items: center; padding: 0 10px; width: 100%; }
  .mem { color: #58d6a0; min-width: 80px; text-align: right; }
  .cpu { color: #d29922; min-width: 45px; text-align: right; }
  .pid { color: var(--faint); min-width: 60px; }
  .name { color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>

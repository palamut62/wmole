<script lang="ts">
  import { onMount } from "svelte";
  import { request } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import GenericConfirm from "./GenericConfirm.svelte";
  import { toast } from "$lib/toast";

  interface Entry { path: string; name: string; location: string; }
  let items = $state<Entry[]>([]);
  let loading = $state(false);
  let confirmOpen = $state(false);
  let target = $state<Entry | null>(null);

  async function load() {
    items = [];
    loading = true;
    const buf: Entry[] = [];
    await request({ op: "startup_list" }, (e: SidecarEvent) => {
      if (e.ev === "item") buf.push(e as unknown as Entry);
    });
    items = buf;
    loading = false;
  }
  onMount(load);

  function ask(e: Entry) { target = e; confirmOpen = true; }
  async function disable() {
    confirmOpen = false;
    if (!target) return;
    const d = await request({ op: "startup_disable", name: target.name, location: target.location, path: target.path });
    toast(d.ok ? `${target.name} başlangıçtan kaldırıldı` : "İşlem başarısız", d.ok ? "ok" : "err");
    load();
  }
</script>

<div class="view">
  <div class="toolbar">
    <h2>Başlangıç Programları</h2>
    <button onclick={load} disabled={loading}>{loading ? "…" : "Yenile"}</button>
    <span class="count">{items.length} girdi</span>
  </div>
  <div class="list">
    {#each items as e (e.location + e.name)}
      <div class="entry">
        <span class="name">{e.name}</span>
        <span class="loc">{e.location}</span>
        <span class="cmd">{e.path}</span>
        <button class="mini danger" onclick={() => ask(e)}>Kaldır</button>
      </div>
    {/each}
    {#if !items.length && !loading}<p class="muted">Başlangıç girdisi yok.</p>{/if}
  </div>
</div>

<GenericConfirm open={confirmOpen} danger title="Başlangıçtan Kaldır"
  message={`"${target?.name ?? ""}" sistem başlangıcından kaldırılacak. Devam?`}
  confirmLabel="Kaldır" onConfirm={disable} onCancel={() => (confirmOpen = false)} />

<style>
  .view { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  h2 { margin: 0; color: var(--fg); }
  .count { color: var(--muted); font-size: 12px; }
  button { background: var(--btn); color: var(--fg); border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; }
  button.danger { background: #e5534b; color: white; }
  button.mini { padding: 2px 8px; font-size: 12px; }
  .list { flex: 1; min-height: 0; overflow: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px; }
  .entry { display: flex; gap: 12px; align-items: center; padding: 6px 10px; border-radius: 4px; }
  .entry:hover { background: var(--border); }
  .name { color: var(--fg); min-width: 160px; }
  .loc { color: #d29922; min-width: 110px; font-size: 11px; }
  .cmd { color: var(--faint); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
  .muted { color: var(--faint); padding: 12px; }
</style>

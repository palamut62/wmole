<script lang="ts">
  import { onMount } from "svelte";
  import { request, cancel } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import { toast } from "$lib/toast";

  interface Entry {
    path: string;
    name: string;
    size: number;
    kind: string; // "dir" | "file" | "large-file"
    selected: boolean;
  }

  let path = $state("");
  let entries = $state<Entry[]>([]);
  let scanning = $state(false);
  let largeMode = $state(false);
  let activeId = $state<string | null>(null);
  let progress = $state({ done: 0, total: 0, label: "" });
  let drives = $state<any[]>([]);
  let showDrives = $state(false);
  let confirmOpen = $state(false);

  let selected = $derived(entries.filter((e) => e.selected));
  let selectedBytes = $derived(selected.reduce((a, i) => a + (i.size || 0), 0));

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0,
      v = n;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i++;
    }
    return v.toFixed(1) + " " + u[i];
  }
  function parent(p: string) {
    const norm = p.replace(/[\\/]+$/, "");
    const idx = Math.max(norm.lastIndexOf("\\"), norm.lastIndexOf("/"));
    return idx > 2 ? norm.slice(0, idx) : norm.slice(0, idx + 1);
  }

  async function scan(target?: string) {
    if (target !== undefined) path = target;
    entries = [];
    scanning = true;
    showDrives = false;
    const buf: Entry[] = [];
    await request(
      { op: "scan", mode: largeMode ? "large" : "analyze", paths: path ? [path] : [] },
      (e: SidecarEvent) => {
        if (e.ev === "started") activeId = String(e.id);
        if (e.ev === "item") buf.push(e as unknown as Entry);
        if (e.ev === "progress")
          progress = {
            done: e.done as number,
            total: e.total as number,
            label: String(e.label ?? ""),
          };
      },
    );
    entries = buf;
    scanning = false;
    activeId = null;
    progress = { done: 0, total: 0, label: "" };
  }

  async function loadDrives() {
    const done = await request({ op: "drives" });
    drives = (done.payload as any)?.drives ?? [];
    showDrives = true;
  }

  function openEntry(e: Entry) {
    if (e.kind === "dir") scan(e.path);
  }
  function toggle(e: Entry) {
    e.selected = !e.selected;
    entries = [...entries];
  }
  function reveal(p: string) {
    request({ op: "open_path", path: p });
  }
  function cancelScan() {
    if (activeId) cancel(activeId);
  }

  async function doDelete(permanent: boolean) {
    confirmOpen = false;
    const targets = selected.map((i) => i.path);
    let ok = 0,
      err = 0;
    await request({ op: "delete", targets, permanent }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    entries = entries.filter((i) => !targets.includes(i.path));
    progress = { done: 0, total: 0, label: "" };
    toast(`${ok} silindi${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
  }

  onMount(() => scan(""));
</script>

<div class="scan">
  <div class="toolbar">
    <h2>Analyze</h2>
    <button onclick={() => scan(parent(path))} disabled={scanning || !path}>⬆ Üst</button>
    <input
      class="pathbox"
      bind:value={path}
      placeholder="C:\\Users\\…"
      onkeydown={(e) => e.key === "Enter" && scan()}
    />
    <button onclick={() => scan()} disabled={scanning}>{scanning ? "…" : "Tara"}</button>
    <label class="dry"><input type="checkbox" bind:checked={largeMode} onchange={() => scan()} /> Büyük dosyalar</label>
    <button onclick={loadDrives}>Sürücüler</button>
    <button class="danger" onclick={() => selected.length && (confirmOpen = true)} disabled={!selected.length}>Sil…</button>
    <span class="count">{entries.length} öğe · {selected.length} seçili</span>
  </div>

  {#if showDrives}
    <div class="drives">
      {#each drives as d}
        <button class="drive" onclick={() => scan(d.mountpoint)}>
          <strong>{d.device}</strong>
          <span class="muted">{fmt(d.free)} boş / {fmt(d.total)} ({d.percent}%)</span>
        </button>
      {/each}
    </div>
  {/if}

  <div class="list">
    <VirtualList items={entries} rowHeight={28}>
      {#snippet row(item)}
        <div class="entry">
          <input type="checkbox" checked={item.selected} onchange={() => toggle(item)} />
          <span class="size">{fmt(item.size)}</span>
          <button
            class="name"
            class:dir={item.kind === "dir"}
            onclick={() => openEntry(item)}
            title={item.path}
          >
            {item.kind === "dir" ? "📁" : "📄"} {item.name}
          </button>
          <button class="mini" onclick={() => reveal(item.path)} title="Explorer'da aç">↗</button>
        </div>
      {/snippet}
    </VirtualList>
  </div>
</div>

<ConfirmModal
  open={confirmOpen}
  count={selected.length}
  bytes={selectedBytes}
  onConfirm={doDelete}
  onCancel={() => (confirmOpen = false)}
/>
<StatusBar
  label={progress.label}
  done={progress.done}
  total={progress.total}
  onCancel={activeId ? cancelScan : null}
/>

<style>
  .scan { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .toolbar h2 { margin: 0; color: #e6edf3; }
  .pathbox { flex: 1; min-width: 200px; background: #0d1117; border: 1px solid #1b2530; color: #e6edf3; padding: 5px 10px; border-radius: 4px; font-family: monospace; }
  .dry { color: #9aa7b4; display: flex; gap: 6px; align-items: center; font-size: 12px; }
  button { background: #243140; color: #e6edf3; border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  .count { color: #9aa7b4; font-size: 12px; }
  .drives { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  .drive { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; background: #11161c; border: 1px solid #1b2530; padding: 8px 14px; }
  .muted { color: #6e7681; font-size: 11px; }
  .list { flex: 1; min-height: 0; background: #11161c; border: 1px solid #1b2530; border-radius: 8px; }
  .entry { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; }
  .size { color: #58d6a0; min-width: 80px; text-align: right; }
  .name { flex: 1; text-align: left; background: none; color: #9aa7b4; padding: 2px 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name.dir { color: #e6edf3; cursor: pointer; }
  .name.dir:hover { color: #58d6a0; }
  .mini { padding: 2px 8px; font-size: 12px; }
</style>

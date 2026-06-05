<script lang="ts">
  import { onMount } from "svelte";
  import { request, cancel } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import { toast } from "$lib/toast";
  import { t, tr } from "$lib/i18n";

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
  let selectedDrive = $state<string | null>(null);
  let confirmOpen = $state(false);
  let treemap = $state(false);

  let selected = $derived(entries.filter((e) => e.selected));
  let selectedBytes = $derived(selected.reduce((a, i) => a + (i.size || 0), 0));
  let totalSize = $derived(entries.reduce((a, e) => a + (e.size || 0), 0) || 1);
  let topEntries = $derived([...entries].sort((a, b) => b.size - a.size).slice(0, 40));

  function treemapColor(i: number) {
    return `hsl(${(i * 47) % 360}, 55%, 55%)`;
  }
  function onKey(e: KeyboardEvent) {
    const tag = (e.target as HTMLElement)?.tagName;
    if (tag === "INPUT" || tag === "SELECT") return;
    if (e.key === "Backspace" && path) { e.preventDefault(); scan(parent(path)); }
    else if (e.key === "Delete" && selected.length) { e.preventDefault(); confirmOpen = true; }
    else if (e.key === "a") setAll(true);
    else if (e.key === "n") setAll(false);
    else if (e.key === "r") scan();
  }

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
  function setAll(value: boolean) {
    entries.forEach((e) => (e.selected = value));
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
    toast(`${ok} ${tr("silindi")}${err ? `, ${err} ${tr("hata")}` : ""}`, err ? "err" : "ok");
  }

  onMount(() => scan(""));
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{$t("Gezgin")}</h2>
    <button onclick={() => scan(parent(path))} disabled={scanning || !path}>{$t("⬆ Üst")}</button>
    <input
      class="pathbox"
      bind:value={path}
      placeholder="C:\\Users\\…"
      onkeydown={(e) => e.key === "Enter" && scan()}
    />
    <button onclick={() => scan()} disabled={scanning}>{scanning ? "…" : $t("Tara")}</button>
    <label class="dry"><input type="checkbox" bind:checked={largeMode} onchange={() => scan()} /> {$t("Büyük dosyalar")}</label>
    <button onclick={loadDrives}>{$t("Sürücüler")}</button>
    <button class:active={treemap} onclick={() => (treemap = !treemap)}>{$t("Harita")}</button>
    <button onclick={() => setAll(true)} disabled={!entries.length}>{$t("Tümünü Seç")}</button>
    <button onclick={() => setAll(false)} disabled={!entries.length}>{$t("Hiçbiri")}</button>
    <button class="danger" onclick={() => selected.length && (confirmOpen = true)} disabled={!selected.length}>{$t("Sil…")} ({selected.length})</button>
    <span class="count">{entries.length} {$t("öğe")} · {selected.length} {$t("seçili")} · {fmt(selectedBytes)}</span>
  </div>

  {#if showDrives}
    <div class="drives">
      {#each drives as d}
        <button
          class="drive"
          class:selected={selectedDrive === d.mountpoint}
          onclick={() => { selectedDrive = d.mountpoint; path = d.mountpoint; }}
          ondblclick={() => scan(d.mountpoint)}
        >
          <strong>{d.device}</strong>
          <span class="muted">{fmt(d.free)} {$t("boş")} / {fmt(d.total)} ({d.percent}%)</span>
        </button>
      {/each}
      <button
        class="start"
        onclick={() => selectedDrive && scan(selectedDrive)}
        disabled={!selectedDrive || scanning}
      >▶ {$t("Analize Başla")}</button>
    </div>
  {/if}

  {#if treemap}
    <div class="treemap">
      {#each topEntries as e, i (e.path)}
        <button
          class="tile"
          style="flex-grow:{Math.max(1, Math.round((e.size / totalSize) * 1000))}; background:{treemapColor(i)}"
          title="{e.name} · {fmt(e.size)} ({((e.size / totalSize) * 100).toFixed(1)}%)"
          onclick={() => (e.kind === "dir" ? openEntry(e) : reveal(e.path))}
        >
          <span class="tname">{e.name}</span>
          <span class="tsize">{fmt(e.size)}</span>
        </button>
      {/each}
    </div>
  {:else}
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
  {/if}
</div>

<svelte:window onkeydown={onKey} />

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
  .toolbar h2 { margin: 0; color: var(--fg); }
  .pathbox { flex: 1; min-width: 200px; background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 5px 10px; border-radius: 4px; font-family: monospace; }
  .dry { color: var(--muted); display: flex; gap: 6px; align-items: center; font-size: 12px; }
  button { background: var(--btn); color: var(--fg); border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  button.active { background: #2ea043; color: white; }
  .treemap { flex: 1; min-height: 0; display: flex; flex-wrap: wrap; gap: 3px; align-content: flex-start; overflow: auto; padding: 3px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .tile { min-width: 90px; min-height: 60px; flex-basis: 0; display: flex; flex-direction: column; justify-content: center; align-items: center; border: none; border-radius: 4px; color: #0d1117; cursor: pointer; padding: 6px; overflow: hidden; }
  .tname { font-size: 12px; font-weight: bold; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; }
  .tsize { font-size: 11px; opacity: 0.85; }
  .count { color: var(--muted); font-size: 12px; }
  .drives { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
  .drive { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; background: var(--panel); border: 1px solid var(--border); padding: 8px 14px; }
  .drive.selected { border-color: #2ea043; box-shadow: 0 0 0 1px #2ea043 inset; }
  .start { align-self: center; background: #2ea043; color: white; font-weight: bold; }
  .muted { color: var(--faint); font-size: 11px; }
  .list { flex: 1; min-height: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .entry { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; }
  .size { color: #58d6a0; min-width: 80px; text-align: right; }
  .name { flex: 1; text-align: left; background: none; color: var(--muted); padding: 2px 4px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name.dir { color: var(--fg); cursor: pointer; }
  .name.dir:hover { color: #58d6a0; }
  .mini { padding: 2px 8px; font-size: 12px; }
</style>

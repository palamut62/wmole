<script lang="ts">
  import { request, cancel } from "$lib/sidecar";
  import type { ScanItem, SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import { toast } from "$lib/toast";

  let { mode }: { mode: string } = $props();

  let items = $state<ScanItem[]>([]);
  let scanning = $state(false);
  let activeId = $state<string | null>(null);
  let progress = $state({ done: 0, total: 0, label: "" });
  let modalOpen = $state(false);

  let selected = $derived(items.filter((i) => i.selected));
  let selectedBytes = $derived(selected.reduce((s, i) => s + (i.size || 0), 0));

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i++;
    }
    return v.toFixed(1) + " " + u[i];
  }

  async function scan() {
    items = [];
    scanning = true;
    progress = { done: 0, total: 0, label: "" };
    const buf: ScanItem[] = [];
    await request({ op: "scan", mode: mode as any }, (e: SidecarEvent) => {
      if (e.ev === "started") activeId = String(e.id);
      if (e.ev === "item") buf.push(e as unknown as ScanItem);
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    items = buf;
    scanning = false;
    activeId = null;
    progress = { done: 0, total: 0, label: "" };
  }

  function toggle(it: ScanItem) {
    it.selected = !it.selected;
    items = [...items];
  }

  function askDelete() {
    if (selected.length) modalOpen = true;
  }

  async function doDelete(permanent: boolean) {
    modalOpen = false;
    const targets = selected.map((i) => i.path);
    let ok = 0,
      err = 0;
    await request({ op: "delete", targets, permanent }, (e) => {
      if (e.ev === "started") activeId = String(e.id);
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    items = items.filter((i) => !targets.includes(i.path));
    activeId = null;
    progress = { done: 0, total: 0, label: "" };
    toast(`${ok} silindi${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
  }
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{mode}</h2>
    <button onclick={scan} disabled={scanning}
      >{scanning ? "Taranıyor…" : "Tara"}</button
    >
    <button class="danger" onclick={askDelete} disabled={!selected.length}
      >Sil…</button
    >
    <span class="count">{items.length} öğe · {selected.length} seçili</span>
  </div>
  <div class="list">
    <VirtualList {items}>
      {#snippet row(item)}
        <label class="entry">
          <input
            type="checkbox"
            checked={item.selected}
            onchange={() => toggle(item)}
          />
          <span class="size">{fmt(item.size)}</span>
          <span class="name">{item.path}</span>
          <button
            class="mini"
            onclick={(e) => {
              e.preventDefault();
              request({ op: "open_path", path: item.path });
            }}
            title="Explorer'da aç">↗</button
          >
        </label>
      {/snippet}
    </VirtualList>
  </div>
</div>

<ConfirmModal
  open={modalOpen}
  count={selected.length}
  bytes={selectedBytes}
  onConfirm={doDelete}
  onCancel={() => (modalOpen = false)}
/>
<StatusBar
  label={progress.label}
  done={progress.done}
  total={progress.total}
  onCancel={activeId ? () => cancel(activeId!) : null}
/>

<style>
  .scan {
    display: flex;
    flex-direction: column;
    height: 100%;
    font-family: monospace;
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 10px;
  }
  .toolbar h2 { margin: 0; text-transform: capitalize; color: #e6edf3; }
  button {
    background: #243140;
    color: #e6edf3;
    border: none;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: monospace;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  .count { color: #9aa7b4; font-size: 12px; }
  .list {
    flex: 1;
    min-height: 0;
    background: #11161c;
    border: 1px solid #1b2530;
    border-radius: 8px;
  }
  .entry {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 0 10px;
    width: 100%;
    cursor: pointer;
  }
  .size { color: #58d6a0; min-width: 80px; text-align: right; }
  .name {
    color: #9aa7b4;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .mini {
    background: #243140;
    color: #e6edf3;
    border: none;
    padding: 2px 8px;
    border-radius: 4px;
    cursor: pointer;
    font-family: monospace;
    font-size: 12px;
  }
</style>

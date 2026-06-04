<script lang="ts">
  import { request, cancel } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import { toast } from "$lib/toast";

  interface Dup { path: string; name: string; size: number; group: string; selected: boolean; }
  let path = $state("");
  let items = $state<Dup[]>([]);
  let scanning = $state(false);
  let activeId = $state<string | null>(null);
  let progress = $state({ done: 0, total: 0, label: "" });
  let confirmOpen = $state(false);

  let selected = $derived(items.filter((i) => i.selected));
  let selectedBytes = $derived(selected.reduce((a, i) => a + (i.size || 0), 0));

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB"]; let i = 0, v = n;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + " " + u[i];
  }

  async function scan() {
    items = []; scanning = true;
    const buf: Dup[] = [];
    await request({ op: "duplicates", paths: path ? [path] : [] }, (e: SidecarEvent) => {
      if (e.ev === "started") activeId = String(e.id);
      if (e.ev === "item") buf.push(e as unknown as Dup);
      if (e.ev === "progress") progress = { done: e.done as number, total: e.total as number, label: String(e.label ?? "") };
    });
    // grup sırala
    buf.sort((a, b) => a.group.localeCompare(b.group) || b.size - a.size);
    items = buf;
    scanning = false; activeId = null; progress = { done: 0, total: 0, label: "" };
  }

  function toggle(it: Dup) { it.selected = !it.selected; items = [...items]; }

  async function del(permanent: boolean) {
    confirmOpen = false;
    const targets = selected.map((i) => i.path);
    let ok = 0, err = 0;
    await request({ op: "delete", targets, permanent }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress") progress = { done: e.done as number, total: e.total as number, label: String(e.label ?? "") };
    });
    items = items.filter((i) => !targets.includes(i.path));
    progress = { done: 0, total: 0, label: "" };
    toast(`${ok} kopya silindi${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
  }
</script>

<div class="view">
  <div class="toolbar">
    <h2>Yinelenen Dosyalar</h2>
    <input class="pathbox" bind:value={path} placeholder="C:\\Users\\… (boş = ev dizini)" onkeydown={(e) => e.key === "Enter" && scan()} />
    <button onclick={scan} disabled={scanning}>{scanning ? "Taranıyor…" : "Tara"}</button>
    <button class="danger" onclick={() => selected.length && (confirmOpen = true)} disabled={!selected.length}>Sil… ({selected.length})</button>
    <span class="count">{items.length} kopya · {fmt(selectedBytes)} seçili</span>
  </div>
  <p class="hint">≥1MB dosyalar boyut+MD5 ile karşılaştırılır. Aynı renkli grup = aynı içerik. Her gruptan birini saklamanız önerilir.</p>
  <div class="list">
    <VirtualList items={items} rowHeight={26}>
      {#snippet row(item)}
        <label class="entry">
          <input type="checkbox" checked={item.selected} onchange={() => toggle(item)} />
          <span class="grp" style="color:hsl({(parseInt(item.group, 16) % 360)},60%,60%)">●{item.group}</span>
          <span class="size">{fmt(item.size)}</span>
          <span class="name">{item.path}</span>
        </label>
      {/snippet}
    </VirtualList>
  </div>
</div>

<ConfirmModal open={confirmOpen} count={selected.length} bytes={selectedBytes} onConfirm={del} onCancel={() => (confirmOpen = false)} />
<StatusBar label={progress.label} done={progress.done} total={progress.total} onCancel={activeId ? () => cancel(activeId!) : null} />

<style>
  .view { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
  h2 { margin: 0; color: var(--fg); }
  .pathbox { flex: 1; min-width: 200px; background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 5px 10px; border-radius: 4px; font-family: monospace; }
  button { background: var(--btn); color: var(--fg); border: none; padding: 6px 12px; border-radius: 4px; cursor: pointer; font-family: monospace; }
  button.danger { background: #e5534b; color: white; }
  .count { color: var(--muted); font-size: 12px; }
  .hint { color: var(--faint); font-size: 12px; margin: 0 0 8px; }
  .list { flex: 1; min-height: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .entry { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; }
  .grp { min-width: 90px; font-size: 11px; }
  .size { color: #58d6a0; min-width: 70px; text-align: right; }
  .name { color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>

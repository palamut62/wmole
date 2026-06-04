<script lang="ts">
  import { request, cancel } from "$lib/sidecar";
  import type { ScanItem, SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import { toast } from "$lib/toast";
  import { notify } from "$lib/notify";
  import { t } from "$lib/i18n";

  let { mode }: { mode: string } = $props();
  const titleKey: Record<string, string> = {
    clean: "Temizle", purge: "Artıklar", installers: "Kurulumlar", categories: "Kategoriler",
  };

  let items = $state<ScanItem[]>([]);
  let scanning = $state(false);
  let activeId = $state<string | null>(null);
  let progress = $state({ done: 0, total: 0, label: "" });
  let modalOpen = $state(false);
  let filter = $state("");
  let dryRun = $state(false);

  let selected = $derived(items.filter((i) => i.selected));
  let selectedBytes = $derived(selected.reduce((s, i) => s + (i.size || 0), 0));
  let shown = $derived(
    filter
      ? items.filter((i) => i.path.toLowerCase().includes(filter.toLowerCase()))
      : items,
  );
  let categories = $derived([...new Set(items.map((i) => i.category).filter(Boolean))] as string[]);

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

  function setAll(value: boolean) {
    shown.forEach((i) => (i.selected = value));
    items = [...items];
  }
  function selectCategory(cat: string) {
    items.forEach((i) => {
      if (i.category === cat) i.selected = true;
    });
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
    await request({ op: "delete", targets, permanent, dry_run: dryRun }, (e) => {
      if (e.ev === "started") activeId = String(e.id);
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    if (!dryRun) items = items.filter((i) => !targets.includes(i.path));
    activeId = null;
    progress = { done: 0, total: 0, label: "" };
    const msg = `${dryRun ? "Dry-run: " : ""}${ok} ${dryRun ? "önizlendi" : "silindi"}${err ? `, ${err} hata` : ""}`;
    toast(msg, err ? "err" : "ok");
    if (!dryRun && ok > 0) notify("wmole temizlik", msg);
  }
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{$t(titleKey[mode] ?? mode)}</h2>
    <button onclick={scan} disabled={scanning}
      >{scanning ? $t("Taranıyor…") : $t("Tara")}</button
    >
    <button onclick={() => setAll(true)} disabled={!items.length}>{$t("Tümünü Seç")}</button>
    <button onclick={() => setAll(false)} disabled={!items.length}>{$t("Hiçbiri")}</button>
    {#if categories.length > 1}
      <select onchange={(e) => { const v = (e.target as HTMLSelectElement).value; if (v) selectCategory(v); (e.target as HTMLSelectElement).value = ""; }}>
        <option value="">{$t("Kategori seç")}…</option>
        {#each categories as c}<option value={c}>{c}</option>{/each}
      </select>
    {/if}
    <input class="filter" placeholder={$t("filtrele…")} bind:value={filter} />
    <label class="dry"><input type="checkbox" bind:checked={dryRun} /> {$t("Dry-run")}</label>
    <button class="danger" onclick={askDelete} disabled={!selected.length}
      >{dryRun ? $t("Önizle") : $t("Sil")}… ({selected.length})</button
    >
    <span class="count">{shown.length}/{items.length} {$t("öğe")} · {selected.length} {$t("seçili")} · {fmt(selectedBytes)}</span>
  </div>
  <div class="list">
    <VirtualList items={shown}>
      {#snippet row(item)}
        <label class="entry">
          <input
            type="checkbox"
            checked={item.selected}
            onchange={() => toggle(item)}
          />
          <span class="size">{fmt(item.size)}</span>
          {#if item.category}<span class="cat">{item.category}</span>{/if}
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
  .toolbar h2 { margin: 0; text-transform: capitalize; color: var(--fg); }
  button {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 6px 14px;
    border-radius: 4px;
    cursor: pointer;
    font-family: monospace;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  .count { color: var(--muted); font-size: 12px; }
  .filter, select { background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 5px 9px; border-radius: 4px; font-family: monospace; }
  .dry { color: var(--muted); display: flex; gap: 5px; align-items: center; font-size: 12px; }
  .list {
    flex: 1;
    min-height: 0;
    background: var(--panel);
    border: 1px solid var(--border);
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
  .cat { color: #d29922; font-size: 11px; min-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .name {
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .mini {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 2px 8px;
    border-radius: 4px;
    cursor: pointer;
    font-family: monospace;
    font-size: 12px;
  }
</style>

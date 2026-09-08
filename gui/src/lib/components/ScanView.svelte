<script lang="ts">
  import { request, cancel } from "$lib/sidecar";
  import type { ScanItem, SidecarEvent, DiskFacts } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import ConfirmModal from "./ConfirmModal.svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import { toast } from "$lib/toast";
  import { notify } from "$lib/notify";
  import { t, tr } from "$lib/i18n";
  import { theme, THEME_PRESENTATION } from "$lib/theme";

  // Layout themes differ in how a list of candidates is presented: dense rows
  // (Fluent/Linear/HUD) or a tile grid (Cards/Bento).
  let presentation = $derived(THEME_PRESENTATION[$theme]);
  // A tile grid cannot be virtualised the way the row list is, so it renders a
  // window and grows on demand rather than mounting thousands of cards.
  let gridLimit = $state(240);

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
  // Row the detail panel is describing. Independent of the checkboxes: you
  // read about an item before deciding whether to select it.
  let focused = $state<ScanItem | null>(null);
  let selectionFacts = $state<DiskFacts | null>(null);
  let measuring = $state(false);

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
    // The sidecar streams each artifact as soon as it is sized, so flush into
    // `items` on a timer instead of waiting for the scan to finish — a large
    // purge scan used to show nothing at all until it was completely done.
    const buf: ScanItem[] = [];
    let dirty = false;
    const flush = setInterval(() => {
      if (dirty) {
        items = [...buf];
        dirty = false;
      }
    }, 200);
    try {
      await request({ op: "scan", mode: mode as any }, (e: SidecarEvent) => {
        if (e.ev === "started") activeId = String(e.id);
        if (e.ev === "item") {
          buf.push(e as unknown as ScanItem);
          dirty = true;
        }
        if (e.ev === "progress")
          progress = {
            done: e.done as number,
            total: e.total as number,
            label: String(e.label ?? ""),
          };
      });
    } finally {
      clearInterval(flush);
    }
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

  async function askDelete() {
    if (!selected.length) return;
    modalOpen = true;
    // Measure the exact selection now rather than during the scan: the extra
    // syscall per file is affordable here and this number is about to be shown
    // as a promise of how much space comes back.
    selectionFacts = null;
    measuring = true;
    try {
      const e = await request(
        { op: "disk_facts", paths: selected.map((i) => i.path), budget_seconds: 2 },
        () => {},
      );
      selectionFacts = ((e.payload as any)?.total ?? null) as DiskFacts | null;
    } catch {
      selectionFacts = null; // fall back to showing the logical figure alone
    } finally {
      measuring = false;
    }
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
    const msg = `${dryRun ? tr("Dry-run:") + " " : ""}${ok} ${dryRun ? tr("önizlendi") : tr("silindi")}${err ? `, ${err} ${tr("hata")}` : ""}`;
    toast(msg, err ? "err" : "ok");
    if (!dryRun && ok > 0) notify(tr("wmole temizlik"), msg);
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
  {#if presentation === "grid"}
    <div class="grid surface">
      {#each shown.slice(0, gridLimit) as item (item.path)}
        <article class="card" class:sel={item.selected}>
          <label class="card-head">
            <input type="checkbox" checked={item.selected} onchange={() => toggle(item)} />
            <span class="size">{item.truncated ? "≥ " : ""}{fmt(item.size)}</span>
          </label>
          {#if item.category}<span class="cat">{item.category}</span>{/if}
          <span class="card-name" title={item.path}>{item.name || item.path}</span>
          <span class="card-path" title={item.path}>{item.path}</span>
          <div class="card-actions">
            <button
              class="mini"
              class:on={focused?.path === item.path}
              onclick={() => (focused = focused?.path === item.path ? null : item)}
              title={$t("Bu ne? Ne kaybolur?")}>?</button
            >
            <button
              class="mini"
              onclick={() => request({ op: "open_path", path: item.path })}
              title="Explorer'da aç">↗</button
            >
          </div>
        </article>
      {/each}
      {#if shown.length > gridLimit}
        <button class="more" onclick={() => (gridLimit += 240)}>
          {$t("Daha fazla göster")} ({shown.length - gridLimit})
        </button>
      {/if}
    </div>
  {:else}
  <div class="list surface">
    <VirtualList items={shown}>
      {#snippet row(item)}
        <label class="entry">
          <input
            type="checkbox"
            checked={item.selected}
            onchange={() => toggle(item)}
          />
          <span class="size">{item.truncated ? "≥ " : ""}{fmt(item.size)}</span>
          {#if item.category}<span class="cat">{item.category}</span>{/if}
          <span class="name">{item.path}</span>
          <button
            class="mini"
            class:on={focused?.path === item.path}
            onclick={(e) => {
              e.preventDefault();
              focused = focused?.path === item.path ? null : item;
            }}
            title={$t("Bu ne? Ne kaybolur?")}>?</button
          >
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
  {/if}
  {#if focused}
    <div class="detail-slot">
      <ItemDetail path={focused.path} kind={focused.kind ?? ""} />
    </div>
  {/if}
</div>

<ConfirmModal
  open={modalOpen}
  count={selected.length}
  bytes={selectedBytes}
  facts={selectionFacts}
  {measuring}
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
    font-family: var(--font);
    font-size: var(--fs);
  }
  .toolbar {
    display: flex;
    align-items: center;
    gap: var(--gap);
    margin-bottom: var(--gap);
    flex-wrap: wrap;
  }
  .toolbar h2 {
    margin: 0;
    text-transform: capitalize;
    color: var(--fg);
    font-size: var(--fs-lg);
  }
  button {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 5px 12px;
    border-radius: var(--radius);
    cursor: pointer;
    font-family: var(--font);
    font-size: var(--fs);
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: var(--danger); color: white; }
  .count { color: var(--muted); font-size: var(--fs-sm); }
  .filter, select {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--fg);
    padding: 4px 9px;
    border-radius: var(--radius);
    font-family: var(--font);
    font-size: var(--fs);
  }
  .dry { color: var(--muted); display: flex; gap: 5px; align-items: center; font-size: var(--fs-sm); }
  .list {
    flex: 1;
    min-height: 0;
  }
  .entry {
    display: flex;
    gap: var(--gap);
    align-items: center;
    padding: 0 var(--pad);
    width: 100%;
    cursor: pointer;
    font-family: var(--font-mono);
  }
  .size { color: var(--ok); min-width: 80px; text-align: right; }
  .cat { color: var(--warn); font-size: var(--fs-sm); min-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ── Tile grid, used by the Cards and Bento themes ── */
  .grid {
    flex: 1;
    min-height: 0;
    overflow: auto;
    padding: var(--pad);
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
    gap: var(--gap);
    align-content: start;
  }
  .card {
    display: flex;
    flex-direction: column;
    gap: 3px;
    padding: var(--pad);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    min-width: 0;
  }
  .card.sel {
    border-color: var(--accent);
    box-shadow: inset 0 0 0 1px var(--accent);
  }
  .card-head { display: flex; align-items: center; justify-content: space-between; cursor: pointer; }
  .card-name {
    color: var(--fg);
    font-weight: 600;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .card-path {
    color: var(--faint);
    font-family: var(--font-mono);
    font-size: var(--fs-sm);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    direction: rtl;
    text-align: left;
  }
  .card-actions { display: flex; gap: 5px; margin-top: 5px; }
  .more { grid-column: 1 / -1; justify-self: center; }
  /* Bento reads as one flat board: tiles lose their inner border. */
  :global(html[data-theme="bento"]) .card {
    background: var(--panel);
    border-color: transparent;
    box-shadow: var(--shadow);
  }
  .name {
    color: var(--muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
  }
  .detail-slot {
    margin-top: 10px;
    max-height: 40%;
    display: flex;
    flex-direction: column;
    min-height: 0;
  }
  .detail-slot :global(.detail) { flex: 1; min-height: 0; }
  .mini.on { outline: 1px solid var(--fg); }
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

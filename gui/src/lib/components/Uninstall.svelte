<script lang="ts">
  import { request } from "$lib/sidecar";
  import type { SidecarEvent } from "$lib/types";
  import VirtualList from "./VirtualList.svelte";
  import GenericConfirm from "./GenericConfirm.svelte";
  import StatusBar from "./StatusBar.svelte";
  import { toast } from "$lib/toast";
  import { t } from "$lib/i18n";

  interface App {
    path: string;
    name: string;
    size: number;
    publisher: string;
    version: string;
    uninstall: string;
    app: unknown;
  }
  interface Leftover {
    path: string;
    name: string;
    size: number;
    kind: string;
    selected: boolean;
  }

  let apps = $state<App[]>([]);
  let query = $state("");
  let loading = $state(false);
  let selectedApp = $state<App | null>(null);
  let confirmOpen = $state(false);
  let leftovers = $state<Leftover[]>([]);
  let progress = $state({ done: 0, total: 0, label: "" });

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

  async function load() {
    apps = [];
    loading = true;
    const buf: App[] = [];
    await request({ op: "uninstall_list", query }, (e: SidecarEvent) => {
      if (e.ev === "item") buf.push(e as unknown as App);
    });
    apps = buf;
    loading = false;
  }

  function askUninstall(app: App) {
    selectedApp = app;
    confirmOpen = true;
  }

  async function doUninstall() {
    confirmOpen = false;
    const app = selectedApp;
    if (!app) return;
    await request({ op: "uninstall_run", uninstall: app.uninstall });
    toast(`${app.name} kaldırıcısı başlatıldı`, "info");
    // kalan dosyaları tara
    leftovers = [];
    const buf: Leftover[] = [];
    await request({ op: "leftovers", app: app.app }, (e) => {
      if (e.ev === "item") buf.push(e as unknown as Leftover);
    });
    leftovers = buf;
  }

  function toggleLeftover(it: Leftover) {
    it.selected = !it.selected;
    leftovers = [...leftovers];
  }

  async function deleteLeftovers() {
    const targets = leftovers
      .filter((l) => l.selected && l.kind === "leftover-file")
      .map((l) => l.path);
    if (!targets.length) {
      toast("Önce silinecek dosya seç (registry anahtarları elle)", "info");
      return;
    }
    let ok = 0,
      err = 0;
    await request({ op: "delete", targets, permanent: false }, (e) => {
      if (e.ev === "item_result") e.ok ? ok++ : err++;
      if (e.ev === "progress")
        progress = {
          done: e.done as number,
          total: e.total as number,
          label: String(e.label ?? ""),
        };
    });
    leftovers = leftovers.filter((l) => !targets.includes(l.path));
    progress = { done: 0, total: 0, label: "" };
    toast(`${ok} kalıntı silindi${err ? `, ${err} hata` : ""}`, err ? "err" : "ok");
  }
</script>

<div class="scan">
  <div class="toolbar">
    <h2>{$t("Kaldır")}</h2>
    <input placeholder={$t("ara…")} bind:value={query} onkeydown={(e) => e.key === "Enter" && load()} />
    <button onclick={load} disabled={loading}>{loading ? $t("Yükleniyor…") : $t("Listele")}</button>
    <span class="count">{apps.length} {$t("program")}</span>
  </div>
  <div class="list">
    <VirtualList items={apps} rowHeight={30}>
      {#snippet row(item)}
        <div class="entry">
          <span class="size">{item.size ? fmt(item.size) : ""}</span>
          <span class="name">{item.name} <em>{item.version}</em></span>
          <button class="mini danger" onclick={() => askUninstall(item)}>{$t("Kaldır")}</button>
        </div>
      {/snippet}
    </VirtualList>
  </div>

  {#if leftovers.length}
    <div class="leftovers">
      <div class="toolbar">
        <h3>{$t("Kalıntılar")} ({leftovers.length})</h3>
        <button class="danger" onclick={deleteLeftovers}>{$t("Seçili dosyaları sil")}</button>
      </div>
      <div class="list small">
        <VirtualList items={leftovers} rowHeight={24}>
          {#snippet row(item)}
            <label class="entry">
              {#if item.kind === "leftover-file"}
                <input type="checkbox" checked={item.selected} onchange={() => toggleLeftover(item)} />
              {:else}
                <span class="reg">REG</span>
              {/if}
              <span class="size">{item.size ? fmt(item.size) : ""}</span>
              <span class="name">{item.path}</span>
            </label>
          {/snippet}
        </VirtualList>
      </div>
    </div>
  {/if}
</div>

<GenericConfirm
  open={confirmOpen}
  danger
  title="Kaldırma Onayı"
  message={`${selectedApp?.name ?? ""} için Windows kaldırıcısı başlatılacak. Devam edilsin mi?`}
  confirmLabel="Kaldırıcıyı Başlat"
  onConfirm={doUninstall}
  onCancel={() => (confirmOpen = false)}
/>
<StatusBar label={progress.label} done={progress.done} total={progress.total} onCancel={null} />

<style>
  .scan { display: flex; flex-direction: column; height: 100%; font-family: monospace; }
  .toolbar { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .toolbar h2, .toolbar h3 { margin: 0; color: var(--fg); }
  input {
    background: var(--bg); border: 1px solid var(--border); color: var(--fg);
    padding: 5px 10px; border-radius: 4px; font-family: monospace;
  }
  button {
    background: var(--btn); color: var(--fg); border: none; padding: 6px 14px;
    border-radius: 4px; cursor: pointer; font-family: monospace;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.danger { background: #e5534b; color: white; }
  button.mini { padding: 2px 8px; font-size: 12px; }
  .count { color: var(--muted); font-size: 12px; }
  .list { flex: 1; min-height: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
  .list.small { flex: none; height: 200px; }
  .leftovers { margin-top: 12px; display: flex; flex-direction: column; }
  .entry { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; }
  .size { color: #58d6a0; min-width: 80px; text-align: right; }
  .name { color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .name em { color: var(--faint); font-style: normal; }
  .reg { color: #d29922; font-size: 11px; min-width: 30px; }
</style>

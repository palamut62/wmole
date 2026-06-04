<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { request } from "$lib/sidecar";
  import type { ScanItem, SidecarEvent } from "$lib/types";
  import ConfirmModal from "./ConfirmModal.svelte";
  import VirtualList from "./VirtualList.svelte";
  import StatusBar from "./StatusBar.svelte";
  import { toast } from "$lib/toast";
  import { notify } from "$lib/notify";
  import { t } from "$lib/i18n";

  let s = $state<Record<string, any>>({});
  let hist = $state<Record<string, any>>({});
  let timer: ReturnType<typeof setInterval>;

  // Hızlı temizlik durumu
  let scanItems = $state<ScanItem[]>([]);
  let scanning = $state(false);
  let scanned = $state(false);
  let confirmOpen = $state(false);
  let progress = $state({ done: 0, total: 0, label: "" });

  let chosen = $derived(scanItems.filter((i) => i.selected));
  let reclaimable = $derived(chosen.reduce((a, i) => a + (i.size || 0), 0));

  function toggle(it: ScanItem) {
    it.selected = !it.selected;
    scanItems = [...scanItems];
  }
  function setAll(v: boolean) {
    scanItems.forEach((i) => (i.selected = v));
    scanItems = [...scanItems];
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
  function color(p: number, warn = 75, crit = 90) {
    return p >= crit ? "#e5534b" : p >= warn ? "#d29922" : "#58d6a0";
  }

  async function poll() {
    const done = await request({ op: "status" });
    if (done.payload) s = done.payload as Record<string, any>;
  }
  async function loadHistory() {
    const done = await request({ op: "cleanup_history" });
    if (done.payload) hist = done.payload as Record<string, any>;
  }

  async function quickScan() {
    scanItems = [];
    scanning = true;
    scanned = false;
    const buf: ScanItem[] = [];
    await request({ op: "scan", mode: "clean" }, (e: SidecarEvent) => {
      if (e.ev === "item" && (e as any).selected) buf.push(e as unknown as ScanItem);
    });
    scanItems = buf;
    scanning = false;
    scanned = true;
  }

  async function quickClean(permanent: boolean) {
    confirmOpen = false;
    const targets = chosen.map((i) => i.path);
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
    progress = { done: 0, total: 0, label: "" };
    scanItems = [];
    scanned = false;
    const msg = `Hızlı temizlik: ${ok} öğe silindi${err ? `, ${err} hata` : ""}`;
    toast(msg, err ? "err" : "ok");
    if (ok > 0) notify("wmole", msg);
    loadHistory();
  }

  onMount(() => {
    poll();
    loadHistory();
    timer = setInterval(poll, 2000);
  });
  onDestroy(() => clearInterval(timer));
</script>

<div class="dash">
  <h2>{$t("Gösterge Paneli")}</h2>

  <!-- Metre çubukları -->
  <div class="meters">
    {#each [["Disk", s.disk_percent], ["RAM", s.memory_percent], ["CPU", s.cpu_percent], ["Sağlık", s.health]] as [label, val]}
      <div class="meter">
        <span class="ml">{$t(label as string)}</span>
        <div class="bar">
          <div
            class="fill"
            style="width:{label === 'Sağlık' ? val ?? 0 : val ?? 0}%; background:{label ===
            'Sağlık'
              ? color(100 - (val ?? 0))
              : color(val ?? 0)}"
          ></div>
        </div>
        <span class="mv">{val ?? "—"}{label === "Sağlık" ? "/100" : "%"}</span>
      </div>
    {/each}
    <div class="sub">
      Disk: {s.disk_free ? fmt(s.disk_free) + " boş" : "—"} · RAM: {s.memory_used
        ? fmt(s.memory_used)
        : "—"} / {s.memory_total ? fmt(s.memory_total) : "—"} · Uptime: {s.uptime_seconds
        ? Math.floor(s.uptime_seconds / 3600) + "s"
        : "—"}
      {#if s.battery_percent != null}· 🔋 {s.battery_percent}%{s.power_plugged ? " ⚡" : ""}{/if}
    </div>
    {#if s.cpu_per_core?.length}
      <div class="cores">
        <span class="ml">{$t("Çekirdek")}</span>
        {#each s.cpu_per_core as c}
          <div class="core" title="{c.toFixed(0)}%">
            <div class="corefill" style="height:{Math.max(4, c)}%; background:{color(c)}"></div>
          </div>
        {/each}
      </div>
    {/if}
    {#if s.temperature_sensors && Object.keys(s.temperature_sensors).length}
      <div class="sub">🌡 {Object.values(s.temperature_sensors).flat().slice(0, 4).map((x: any) => `${x.label || "sensor"}: ${x.current}°`).join(" · ")}</div>
    {/if}
  </div>

  <!-- Temizlik Analizi / Hızlı Temizle -->
  <section class="card">
    <h3>{$t("Temizlik Analizi")}</h3>
    {#if scanning}
      <p class="muted">⟳ {$t("Taranıyor…")} {fmt(reclaimable)}</p>
    {:else if scanned && scanItems.length}
      <p>
        ♻ {$t("Geri kazanılabilir:")} <strong class="green">{fmt(reclaimable)}</strong>
        <span class="muted">({chosen.length}/{scanItems.length} {$t("seçili")})</span>
      </p>
      <div class="row">
        <button class="primary" onclick={() => (confirmOpen = true)} disabled={!chosen.length}>
          {$t("Hızlı Temizle")} ({chosen.length})
        </button>
        <button onclick={() => setAll(true)}>{$t("Tümünü Seç")}</button>
        <button onclick={() => setAll(false)}>{$t("Hiçbiri")}</button>
        <button onclick={quickScan}>{$t("Yeniden Tara")}</button>
      </div>
      <div class="cleanlist">
        <VirtualList items={scanItems} rowHeight={24}>
          {#snippet row(item)}
            <label class="clitem">
              <input type="checkbox" checked={item.selected} onchange={() => toggle(item)} />
              <span class="csize">{fmt(item.size)}</span>
              <span class="ccat">{item.category ?? ""}</span>
              <span class="cpath">{item.path}</span>
            </label>
          {/snippet}
        </VirtualList>
      </div>
    {:else if scanned}
      <p class="green">{$t("✓ Temizlenecek önemli bir şey yok")}</p>
      <button onclick={quickScan}>{$t("Yeniden Tara")}</button>
    {:else}
      <p class="muted">{$t("Güvenli temizlik adaylarını tara ve tek tıkla temizle.")}</p>
      <button class="primary" onclick={quickScan}>{$t("Hızlı Tara")}</button>
    {/if}
  </section>

  <!-- Temizlik Geçmişi -->
  <section class="card">
    <h3>{$t("Yapılan Temizlikler")}</h3>
    {#if hist.count > 0}
      <p>
        {$t("Toplam boşaltılan:")} <strong class="green">{fmt(hist.total_freed)}</strong>
        <span class="muted">({hist.count} işlem · son: {hist.last_ts})</span>
      </p>
      {#if (hist.recent ?? []).length}
        {@const maxSz = Math.max(...(hist.recent ?? []).map((e: any) => e.size), 1)}
        <div class="chart">
          {#each hist.recent ?? [] as e}
            <div class="cbar" title="{fmt(e.size)} · {e.path}">
              <div class="cbarfill" style="height:{Math.max(3, (e.size / maxSz) * 100)}%"></div>
            </div>
          {/each}
        </div>
      {/if}
      <ul class="histlist">
        {#each hist.recent ?? [] as e}
          <li>
            <span class="green">{fmt(e.size)}</span>
            <span class="muted">{e.path}</span>
          </li>
        {/each}
      </ul>
    {:else}
      <p class="muted">{$t("Henüz temizlik kaydı yok.")}</p>
    {/if}
  </section>
</div>

<ConfirmModal
  open={confirmOpen}
  count={chosen.length}
  bytes={reclaimable}
  onConfirm={quickClean}
  onCancel={() => (confirmOpen = false)}
/>
<StatusBar label={progress.label} done={progress.done} total={progress.total} onCancel={null} />

<style>
  .dash { display: flex; flex-direction: column; gap: 14px; font-family: monospace; }
  h2 { margin: 0; color: var(--fg); }
  h3 { margin: 0 0 10px; color: #58d6a0; font-size: 14px; }
  .meters {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .meter { display: flex; align-items: center; gap: 12px; }
  .ml { color: var(--muted); min-width: 60px; }
  .bar { flex: 1; height: 12px; background: var(--border); border-radius: 6px; overflow: hidden; }
  .fill { height: 100%; transition: width 0.3s; }
  .mv { color: var(--fg); min-width: 70px; text-align: right; }
  .sub { color: var(--faint); font-size: 12px; margin-top: 4px; }
  .cores { display: flex; align-items: flex-end; gap: 3px; margin-top: 6px; height: 36px; }
  .core { width: 10px; height: 100%; background: var(--border); border-radius: 2px; display: flex; align-items: flex-end; overflow: hidden; }
  .corefill { width: 100%; border-radius: 2px; }
  .chart { display: flex; align-items: flex-end; gap: 4px; height: 60px; margin: 8px 0; }
  .cbar { flex: 1; height: 100%; display: flex; align-items: flex-end; background: var(--bg); border-radius: 3px; overflow: hidden; }
  .cbarfill { width: 100%; background: #58d6a0; border-radius: 3px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .muted { color: var(--faint); }
  .green { color: #58d6a0; }
  .row { display: flex; gap: 10px; margin-top: 8px; }
  button {
    background: var(--btn); color: var(--fg); border: none; padding: 8px 16px;
    border-radius: 5px; cursor: pointer; font-family: monospace;
  }
  button.primary { background: #2ea043; color: white; }
  .histlist { list-style: none; padding: 0; margin: 8px 0 0; display: flex; flex-direction: column; gap: 3px; }
  .histlist li { display: flex; gap: 10px; font-size: 12px; }
  .histlist .green { min-width: 80px; text-align: right; }
  .histlist .muted { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cleanlist { height: 220px; margin-top: 10px; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; }
  .clitem { display: flex; gap: 10px; align-items: center; padding: 0 10px; width: 100%; font-size: 12px; cursor: pointer; }
  .csize { color: #58d6a0; min-width: 70px; text-align: right; }
  .ccat { color: #d29922; min-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cpath { color: var(--muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>

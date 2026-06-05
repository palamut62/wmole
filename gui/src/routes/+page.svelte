<script lang="ts">
  import { onMount } from "svelte";
  import { get } from "svelte/store";
  import "$lib/sidecar"; // listener'ı başlat
  import { theme, applyTheme } from "$lib/theme";
  import { lang } from "$lib/i18n";
  import TopBar from "$lib/components/TopBar.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import Dashboard from "$lib/components/Dashboard.svelte";
  import Analyze from "$lib/components/Analyze.svelte";
  import ScanView from "$lib/components/ScanView.svelte";
  import Uninstall from "$lib/components/Uninstall.svelte";
  import Optimize from "$lib/components/Optimize.svelte";
  import Ports from "$lib/components/Ports.svelte";
  import Processes from "$lib/components/Processes.svelte";
  import Startup from "$lib/components/Startup.svelte";
  import Duplicates from "$lib/components/Duplicates.svelte";
  import Maintenance from "$lib/components/Maintenance.svelte";
  import Settings from "$lib/components/Settings.svelte";
  import Help from "$lib/components/Help.svelte";
  import Toast from "$lib/components/Toast.svelte";
  import AppStatusBar from "$lib/components/AppStatusBar.svelte";
  import UpdateModal from "$lib/components/UpdateModal.svelte";
  import { checkForUpdate } from "$lib/updater";
  import { t } from "$lib/i18n";

  let active = $state("Dashboard");
  let showOnboard = $state(false);
  const scanModes = ["Clean", "Purge", "Installers", "Categories"];

  // routeId → çevrilebilir Türkçe etiket (Sidebar ile aynı), status bar için
  const sectionLabels: Record<string, string> = {
    Dashboard: "Gösterge Paneli", Analyze: "Gezgin", Categories: "Kategoriler",
    Clean: "Temizle", Purge: "Artıklar", Installers: "Kurulumlar",
    Duplicates: "Yinelenenler", Uninstall: "Kaldır", Optimize: "Optimize",
    Startup: "Başlangıç", Ports: "Portlar", Processes: "İşlemler",
    Maintenance: "Bakım", Settings: "Ayarlar", Help: "Yardım",
  };

  onMount(() => {
    applyTheme(get(theme));
    if (typeof localStorage !== "undefined" && !localStorage.getItem("wmole-onboarded")) {
      showOnboard = true;
    }
    // Açılışta + 6 saatte bir sessiz güncelleme kontrolü.
    setTimeout(() => checkForUpdate(true), 4000);
    const iv = setInterval(() => checkForUpdate(true), 6 * 60 * 60 * 1000);
    return () => clearInterval(iv);
  });
  function dismissOnboard(goClean: boolean) {
    showOnboard = false;
    if (typeof localStorage !== "undefined") localStorage.setItem("wmole-onboarded", "1");
    if (goClean) active = "Dashboard";
  }
</script>

<div class="app">
  <TopBar />
  <div class="body">
    <Sidebar {active} onSelect={(v) => (active = v)} />
    <main class="content">
      {#if active === "Dashboard"}
        <Dashboard />
      {:else if active === "Analyze"}
        <Analyze />
      {:else if scanModes.includes(active)}
        {#key active}
          <ScanView mode={active.toLowerCase()} />
        {/key}
      {:else if active === "Uninstall"}
        <Uninstall />
      {:else if active === "Optimize"}
        <Optimize />
      {:else if active === "Startup"}
        <Startup />
      {:else if active === "Ports"}
        <Ports />
      {:else if active === "Processes"}
        <Processes />
      {:else if active === "Duplicates"}
        <Duplicates />
      {:else if active === "Maintenance"}
        <Maintenance />
      {:else if active === "Settings"}
        <Settings />
      {:else if active === "Help"}
        <Help />
      {/if}
    </main>
  </div>
  <AppStatusBar section={$t(sectionLabels[active] ?? active)} />
</div>
<Toast />
<UpdateModal />

{#if showOnboard}
  <div class="ob-backdrop">
    <div class="ob-modal">
      <h2>{$lang === "tr" ? "wmole'a hoş geldin 🐹" : "Welcome to wmole 🐹"}</h2>
      <p>
        {$lang === "tr"
          ? "Windows için bakım araç seti. Sol menüden temizlik, sistem ve gelişmiş araçlara ulaşırsın. Tüm silmeler önce Geri Dönüşüm Kutusu'na gider ve onay penceresiyle korunur."
          : "A maintenance toolkit for Windows. Use the left menu for cleaning, system and advanced tools. All deletes go to the Recycle Bin first and are guarded by a confirmation dialog."}
      </p>
      <p class="muted">
        {$lang === "tr"
          ? 'İpucu: Gösterge Paneli\'nde "Hızlı Tara → Hızlı Temizle" ile tek tıkla yer aç.'
          : 'Tip: On the Dashboard use "Quick Scan → Quick Clean" to free space in one click.'}
      </p>
      <div class="ob-actions">
        <button class="primary" onclick={() => dismissOnboard(true)}>{$lang === "tr" ? "Başla" : "Start"}</button>
      </div>
    </div>
  </div>
{/if}

<style>
  :global(:root) {
    --bg: #0d1117;
    --panel: #11161c;
    --border: #1b2530;
    --fg: #e6edf3;
    --muted: #9aa7b4;
    --faint: #6e7681;
    --btn: #243140;
  }
  :global(html[data-theme="light"]) {
    --bg: #f4f6f8;
    --panel: #ffffff;
    --border: #d6dde4;
    --fg: #1a2027;
    --muted: #51606e;
    --faint: #8a97a3;
    --btn: #e3e9ef;
  }
  :global(html, body) {
    margin: 0;
    height: 100%;
    background: var(--bg);
    color: var(--fg);
  }
  /* Scrollbar'ı gizle (kaydırma çalışmaya devam eder) */
  :global(*) {
    scrollbar-width: none; /* Firefox */
    -ms-overflow-style: none; /* IE/Edge */
  }
  :global(*::-webkit-scrollbar) {
    width: 0;
    height: 0;
    display: none; /* Chromium/WebView2 */
  }
  .app {
    display: flex;
    flex-direction: column;
    height: 100vh;
  }
  .body {
    display: flex;
    flex: 1;
    min-height: 0;
  }
  .content {
    flex: 1;
    overflow: auto;
    padding: 16px;
    display: flex;
    flex-direction: column;
  }
  .ob-backdrop {
    position: fixed; inset: 0; background: rgba(0, 0, 0, 0.65);
    display: flex; align-items: center; justify-content: center; z-index: 200;
  }
  .ob-modal {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 28px; max-width: 460px; font-family: monospace; color: var(--fg);
  }
  .ob-modal h2 { margin: 0 0 12px; color: #58d6a0; }
  .ob-modal p { color: var(--muted); line-height: 1.5; }
  .ob-modal .muted { color: var(--faint); font-size: 13px; }
  .ob-actions { display: flex; justify-content: flex-end; margin-top: 16px; }
  .primary { background: #2ea043; color: white; border: none; padding: 9px 20px; border-radius: 6px; cursor: pointer; font-family: monospace; }
</style>

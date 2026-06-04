<script lang="ts">
  import { onMount } from "svelte";
  import { get } from "svelte/store";
  import "$lib/sidecar"; // listener'ı başlat
  import { theme, applyTheme } from "$lib/theme";
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

  let active = $state("Dashboard");
  let showOnboard = $state(false);
  const scanModes = ["Clean", "Purge", "Installers", "Categories"];

  onMount(() => {
    applyTheme(get(theme));
    if (typeof localStorage !== "undefined" && !localStorage.getItem("wmole-onboarded")) {
      showOnboard = true;
    }
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
</div>
<Toast />

{#if showOnboard}
  <div class="ob-backdrop">
    <div class="ob-modal">
      <h2>wmole'a hoş geldin 🐹</h2>
      <p>
        Windows için bakım araç seti. Sol menüden temizlik (Clean/Purge),
        sistem (Uninstall/Optimize/Ports) ve gelişmiş araçlara (Yinelenenler,
        Başlangıç, İşlemler) ulaşırsın. Tüm silmeler önce Geri Dönüşüm
        Kutusu'na gider ve onay penceresiyle korunur.
      </p>
      <p class="muted">İpucu: Gösterge Paneli'nde "Hızlı Tara → Hızlı Temizle" ile tek tıkla yer aç.</p>
      <div class="ob-actions">
        <button class="primary" onclick={() => dismissOnboard(true)}>Başla</button>
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

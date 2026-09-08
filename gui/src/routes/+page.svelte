<script lang="ts">
  import { onMount } from "svelte";
  import "$lib/sidecar"; // listener'ı başlat
  import { applyCurrent } from "$lib/theme";
  import { lang } from "$lib/i18n";
  import TitleBar from "$lib/components/TitleBar.svelte";
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
  import SecurityCenter from "$lib/components/SecurityCenter.svelte";
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
    Maintenance: "Bakım", Security: "Güvenlik Merkezi", Settings: "Ayarlar", Help: "Yardım",
  };

  onMount(() => {
    applyCurrent();
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
  <TitleBar />
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
      {:else if active === "Security"}
        <SecurityCenter />
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
  /* ── Design tokens ────────────────────────────────────────────────────
   * Two axes drive everything: `data-theme` picks the layout identity
   * (palette, radius, typeface, shadow) and `data-mode` picks light/dark.
   * `data-density` scales spacing. Components read only the tokens, so a new
   * theme is a block of custom properties rather than a component rewrite.
   */
  :global(:root) {
    --bg: #0d1117;
    --panel: #11161c;
    --border: #1b2530;
    --fg: #e6edf3;
    --muted: #9aa7b4;
    --faint: #6e7681;
    --btn: #243140;
    --accent: #3b82f6;
    --accent-fg: #ffffff;
    --ok: #58d6a0;
    --warn: #d29922;
    --danger: #e5534b;
    --radius: 8px;
    --radius-lg: 12px;
    --font: ui-sans-serif, "Segoe UI", system-ui, sans-serif;
    --font-mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
    --shadow: none;
    --panel-blur: none;
  }

  /* Spacing scale. Compact is the default — this is a dense, list-heavy tool. */
  :global(html[data-density="compact"]) {
    --gap: 8px;
    --pad: 10px;
    --row-h: 26px;
    --fs: 12px;
    --fs-sm: 11px;
    --fs-lg: 15px;
  }
  :global(html[data-density="cozy"]) {
    --gap: 14px;
    --pad: 16px;
    --row-h: 34px;
    --fs: 13px;
    --fs-sm: 12px;
    --fs-lg: 18px;
  }

  /* ── Fluent: Windows 11 look — soft neutrals, translucency, rounded ── */
  :global(html[data-theme="fluent"][data-mode="dark"]) {
    --bg: #16181d;
    --panel: rgba(32, 35, 42, 0.72);
    --border: rgba(255, 255, 255, 0.09);
    --fg: #eef1f5;
    --muted: #a8b0bb;
    --faint: #79818d;
    --btn: rgba(255, 255, 255, 0.07);
    --accent: #4cc2ff;
    --accent-fg: #06212e;
  }
  :global(html[data-theme="fluent"][data-mode="light"]) {
    --bg: #f3f3f3;
    --panel: rgba(255, 255, 255, 0.78);
    --border: rgba(0, 0, 0, 0.08);
    --fg: #1b1b1b;
    --muted: #5b5f66;
    --faint: #8b9099;
    --btn: rgba(0, 0, 0, 0.05);
    --accent: #005fb8;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="fluent"]) {
    --radius: 8px;
    --radius-lg: 12px;
    --shadow: 0 2px 10px rgba(0, 0, 0, 0.18);
    --panel-blur: blur(20px);
  }

  /* ── Linear: dense indigo/monospace, squared-off, no shadows ── */
  :global(html[data-theme="linear"][data-mode="dark"]) {
    --bg: #070b12;
    --panel: #0c111d;
    --border: #1d2433;
    --fg: #e6e9ef;
    --muted: #98a1b3;
    --faint: #646d7e;
    --btn: #161d2b;
    --accent: #6366f1;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="linear"][data-mode="light"]) {
    --bg: #fbfbfd;
    --panel: #ffffff;
    --border: #e3e5ec;
    --fg: #14161c;
    --muted: #545a68;
    --faint: #878d9b;
    --btn: #f1f2f6;
    --accent: #5b5bd6;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="linear"]) {
    --radius: 5px;
    --radius-lg: 7px;
    --font: var(--font-mono);
    --shadow: none;
  }

  /* ── Cards: airy, purple accent, generous radii ── */
  :global(html[data-theme="cards"][data-mode="dark"]) {
    --bg: #0f0d15;
    --panel: #1a1622;
    --border: #2c2438;
    --fg: #f0ecf7;
    --muted: #ada2be;
    --faint: #7d7290;
    --btn: #271f33;
    --accent: #a855f7;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="cards"][data-mode="light"]) {
    --bg: #f7f6fb;
    --panel: #ffffff;
    --border: #e6e2ef;
    --fg: #1d1a24;
    --muted: #5c5568;
    --faint: #8e8799;
    --btn: #f0ecf7;
    --accent: #7c3aed;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="cards"]) {
    --radius: 14px;
    --radius-lg: 20px;
    --shadow: 0 4px 16px rgba(0, 0, 0, 0.12);
  }

  /* ── Bento: tile grid, teal accent, flat and boxy ── */
  :global(html[data-theme="bento"][data-mode="dark"]) {
    --bg: #080c0b;
    --panel: #101817;
    --border: #1d2b29;
    --fg: #e8f2f0;
    --muted: #9db3af;
    --faint: #6b807c;
    --btn: #172422;
    --accent: #14b8a6;
    --accent-fg: #04211d;
  }
  :global(html[data-theme="bento"][data-mode="light"]) {
    --bg: #f4f7f6;
    --panel: #ffffff;
    --border: #dde7e5;
    --fg: #17211f;
    --muted: #4f605d;
    --faint: #829490;
    --btn: #e8f1ef;
    --accent: #0d9488;
    --accent-fg: #ffffff;
  }
  :global(html[data-theme="bento"]) {
    --radius: 16px;
    --radius-lg: 22px;
    --shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
  }

  /* ── HUD: instrument panel. Fixed dark identity, cyan on near-black. ── */
  :global(html[data-theme="hud"]) {
    --bg: #03070d;
    --panel: #060f18;
    --border: #124a5c;
    --fg: #c7f2ff;
    --muted: #6fb3c8;
    --faint: #3f7488;
    --btn: #08202b;
    --accent: #22d3ee;
    --accent-fg: #03212a;
    --ok: #34d399;
    --radius: 4px;
    --radius-lg: 10px;
    --font: var(--font-mono);
    --shadow: 0 0 0 1px rgba(34, 211, 238, 0.08);
  }
  :global(html, body) {
    margin: 0;
    height: 100%;
    background: var(--bg);
    color: var(--fg);
    font-family: var(--font);
    font-size: var(--fs);
  }
  /* Shared surface treatment: every panel picks up the active theme's radius,
   * shadow and (for Fluent) translucency without restating them. */
  :global(.surface) {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow);
    backdrop-filter: var(--panel-blur);
  }
  /* Themes whose identity is a flat grid drop the inner chrome on nested
   * panels, so tiles read as one surface rather than boxes inside boxes. */
  :global(html[data-theme="bento"] .surface) {
    border-width: 1px;
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
    padding: var(--pad);
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

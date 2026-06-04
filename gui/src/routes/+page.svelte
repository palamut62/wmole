<script lang="ts">
  import "$lib/sidecar"; // listener'ı başlat
  import TopBar from "$lib/components/TopBar.svelte";
  import Sidebar from "$lib/components/Sidebar.svelte";
  import Dashboard from "$lib/components/Dashboard.svelte";
  import ScanView from "$lib/components/ScanView.svelte";
  import Uninstall from "$lib/components/Uninstall.svelte";
  import Optimize from "$lib/components/Optimize.svelte";
  import Ports from "$lib/components/Ports.svelte";
  import Maintenance from "$lib/components/Maintenance.svelte";
  import Toast from "$lib/components/Toast.svelte";

  let active = $state("Dashboard");
  const scanModes = ["Analyze", "Clean", "Purge", "Installers"];
</script>

<div class="app">
  <TopBar />
  <div class="body">
    <Sidebar {active} onSelect={(v) => (active = v)} />
    <main class="content">
      {#if active === "Dashboard"}
        <Dashboard />
      {:else if scanModes.includes(active)}
        {#key active}
          <ScanView mode={active.toLowerCase()} />
        {/key}
      {:else if active === "Uninstall"}
        <Uninstall />
      {:else if active === "Optimize"}
        <Optimize />
      {:else if active === "Ports"}
        <Ports />
      {:else if active === "Maintenance"}
        <Maintenance />
      {/if}
    </main>
  </div>
</div>
<Toast />

<style>
  :global(html, body) {
    margin: 0;
    height: 100%;
    background: #0d1117;
    color: #e6edf3;
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
</style>

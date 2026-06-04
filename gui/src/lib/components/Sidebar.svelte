<script lang="ts">
  let { active, onSelect }: { active: string; onSelect: (view: string) => void } =
    $props();
  const views = [
    "Dashboard",
    "Analyze",
    "Clean",
    "Purge",
    "Installers",
    "Uninstall",
    "Optimize",
    "Ports",
    "Maintenance",
  ];
</script>

<nav class="sidebar">
  {#each views as v}
    <button class:active={active === v} onclick={() => onSelect(v)}>{v}</button>
  {/each}
</nav>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px;
    background: #11161c;
    min-width: 150px;
    border-right: 1px solid #1b2530;
  }
  button {
    background: none;
    border: none;
    color: #9aa7b4;
    text-align: left;
    padding: 8px 12px;
    font-family: monospace;
    cursor: pointer;
    border-radius: 4px;
  }
  button:hover { background: #1b2530; color: #e6edf3; }
  button.active { background: #243140; color: #58d6a0; }
</style>

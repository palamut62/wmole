<script lang="ts">
  import { t } from "$lib/i18n";
  let { active, onSelect }: { active: string; onSelect: (view: string) => void } =
    $props();

  const groups = [
    { label: "General", items: [["Dashboard", "▣"]] },
    {
      label: "Cleaning",
      items: [
        ["Analyze", "🔍"],
        ["Categories", "▦"],
        ["Clean", "🧹"],
        ["Purge", "🗑"],
        ["Installers", "📦"],
        ["Duplicates", "⧉"],
      ],
    },
    {
      label: "System",
      items: [
        ["Uninstall", "⊟"],
        ["Optimize", "⚙"],
        ["Startup", "⏻"],
        ["Ports", "🔌"],
        ["Processes", "▤"],
        ["Maintenance", "🛠"],
      ],
    },
    { label: "Other", items: [["Settings", "⚙"], ["Help", "?"]] },
  ];
</script>

<nav class="sidebar">
  {#each groups as g}
    <div class="group-label">{$t(g.label)}</div>
    {#each g.items as [name, icon]}
      <button class:active={active === name} onclick={() => onSelect(name)}>
        <span class="icon">{icon}</span>{$t(name)}
      </button>
    {/each}
  {/each}
</nav>

<style>
  .sidebar {
    display: flex;
    flex-direction: column;
    gap: 1px;
    padding: 8px;
    background: var(--panel);
    min-width: 168px;
    border-right: 1px solid var(--border);
    overflow-y: auto;
  }
  .group-label {
    color: #4d5862;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 10px 12px 4px;
    font-family: monospace;
  }
  button {
    display: flex;
    align-items: center;
    gap: 9px;
    background: none;
    border: none;
    color: var(--muted);
    text-align: left;
    padding: 7px 12px;
    font-family: monospace;
    font-size: 13px;
    cursor: pointer;
    border-radius: 5px;
  }
  .icon { width: 18px; text-align: center; opacity: 0.85; }
  button:hover { background: var(--border); color: var(--fg); }
  button.active { background: var(--btn); color: #58d6a0; }
</style>

<script lang="ts">
  let { active, onSelect }: { active: string; onSelect: (view: string) => void } =
    $props();

  const groups = [
    {
      label: "Genel",
      items: [["Dashboard", "▣"]],
    },
    {
      label: "Temizlik",
      items: [
        ["Analyze", "🔍"],
        ["Categories", "▦"],
        ["Clean", "🧹"],
        ["Purge", "🗑"],
        ["Installers", "📦"],
      ],
    },
    {
      label: "Sistem",
      items: [
        ["Uninstall", "⊟"],
        ["Optimize", "⚙"],
        ["Ports", "🔌"],
        ["Maintenance", "🛠"],
      ],
    },
    {
      label: "Diğer",
      items: [["Help", "?"]],
    },
  ];
</script>

<nav class="sidebar">
  {#each groups as g}
    <div class="group-label">{g.label}</div>
    {#each g.items as [name, icon]}
      <button class:active={active === name} onclick={() => onSelect(name)}>
        <span class="icon">{icon}</span>{name}
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
    background: #11161c;
    min-width: 168px;
    border-right: 1px solid #1b2530;
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
    color: #9aa7b4;
    text-align: left;
    padding: 7px 12px;
    font-family: monospace;
    font-size: 13px;
    cursor: pointer;
    border-radius: 5px;
  }
  .icon { width: 18px; text-align: center; opacity: 0.85; }
  button:hover { background: #1b2530; color: #e6edf3; }
  button.active { background: #243140; color: #58d6a0; }
</style>

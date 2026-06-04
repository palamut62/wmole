<script lang="ts">
  import { t } from "$lib/i18n";
  let { active, onSelect }: { active: string; onSelect: (view: string) => void } =
    $props();

  // [routeId, icon, trLabel] — routeId yönlendirme için sabit, etiket çevrilir
  const groups = [
    { label: "Genel", items: [["Dashboard", "▣", "Gösterge Paneli"]] },
    {
      label: "Temizlik",
      items: [
        ["Analyze", "🔍", "Gezgin"],
        ["Categories", "▦", "Kategoriler"],
        ["Clean", "🧹", "Temizle"],
        ["Purge", "🗑", "Artıklar"],
        ["Installers", "📦", "Kurulumlar"],
        ["Duplicates", "⧉", "Yinelenenler"],
      ],
    },
    {
      label: "Sistem",
      items: [
        ["Uninstall", "⊟", "Kaldır"],
        ["Optimize", "⚙", "Optimize"],
        ["Startup", "⏻", "Başlangıç"],
        ["Ports", "🔌", "Portlar"],
        ["Processes", "▤", "İşlemler"],
        ["Maintenance", "🛠", "Bakım"],
      ],
    },
    { label: "Diğer", items: [["Settings", "⚙", "Ayarlar"], ["Help", "?", "Yardım"]] },
  ];
</script>

<nav class="sidebar">
  {#each groups as g}
    <div class="group-label">{$t(g.label)}</div>
    {#each g.items as [id, icon, label]}
      <button class:active={active === id} onclick={() => onSelect(id)}>
        <span class="icon">{icon}</span>{$t(label)}
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

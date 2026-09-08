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
        ["Security", "🛡", "Güvenlik Merkezi"],
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
    padding: calc(var(--pad) * 0.6);
    background: var(--panel);
    min-width: 156px;
    border-right: 1px solid var(--border);
    backdrop-filter: var(--panel-blur);
    overflow-y: auto;
  }
  .group-label {
    color: var(--faint);
    font-size: var(--fs-sm);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: calc(var(--gap) * 0.8) 12px 3px;
    font-family: var(--font);
  }
  button {
    display: flex;
    align-items: center;
    gap: 9px;
    background: none;
    border: none;
    color: var(--muted);
    text-align: left;
    padding: calc(var(--row-h) * 0.16) 12px;
    font-family: var(--font);
    font-size: var(--fs);
    cursor: pointer;
    border-radius: var(--radius);
  }
  .icon { width: 18px; text-align: center; opacity: 0.85; }
  button:hover { background: var(--border); color: var(--fg); }
  button.active { background: var(--btn); color: var(--accent); font-weight: 600; }
</style>

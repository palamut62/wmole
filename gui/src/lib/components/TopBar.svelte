<script lang="ts">
  import { connected } from "$lib/sidecar";
  import { theme, toggleTheme } from "$lib/theme";
  import { lang, toggleLang } from "$lib/i18n";
</script>

<header class="topbar">
  <span class="brand">wmole</span>
  <span class="spacer"></span>
  <button class="chip" onclick={toggleLang} title="Dil / Language">{$lang.toUpperCase()}</button>
  <button class="chip" onclick={toggleTheme} title="Tema">{$theme === "dark" ? "🌙" : "☀"}</button>
  <span class="status" class:on={$connected}>
    {$connected ? "● sidecar bağlı" : "○ bağlanıyor…"}
  </span>
</header>

<style>
  .topbar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 8px 14px;
    background: var(--bg);
    border-bottom: 1px solid var(--border);
    font-family: monospace;
  }
  .brand { color: #58d6a0; font-weight: bold; }
  .spacer { flex: 1; }
  .chip {
    background: var(--btn); color: var(--fg); border: none; padding: 3px 9px;
    border-radius: 4px; cursor: pointer; font-family: monospace; font-size: 12px;
  }
  .status { color: var(--faint); font-size: 12px; }
  .status.on { color: #58d6a0; }
</style>

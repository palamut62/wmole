<script lang="ts">
  import { connected } from "$lib/sidecar";
  import {
    theme, mode, compact, setTheme, toggleMode, toggleDensity,
    LAYOUT_THEMES, THEME_LABELS, FIXED_DARK,
  } from "$lib/theme";
  import { lang, toggleLang, t } from "$lib/i18n";

  // HUD is a fixed dark instrument panel; offering it a light mode would be a
  // switch that visibly does nothing.
  let modeApplies = $derived(!FIXED_DARK.includes($theme));
</script>

<!-- No brand mark here: the OS title bar already reads "wmole", and repeating
     it costs a row of space in a compact layout without telling you anything. -->
<header class="topbar">
  <!-- Bu boşluk başlık çubuğunun sürüklenebilir alanının bir parçası; işaret
       konmazsa çubuğun ortasından tutup pencereyi taşımak çalışmaz. -->
  <span class="spacer" data-tauri-drag-region></span>

  <div class="themes" role="group" aria-label={$t("Arayüz teması")}>
    {#each LAYOUT_THEMES as name}
      <button
        class="chip theme"
        class:on={$theme === name}
        aria-pressed={$theme === name}
        onclick={() => setTheme(name)}
        title={THEME_LABELS[name]}>{THEME_LABELS[name]}</button
      >
    {/each}
  </div>

  <button
    class="chip"
    onclick={toggleDensity}
    title={$t("Yoğunluk")}
    aria-pressed={$compact}>{$compact ? "▪ " + $t("Kompakt") : "▫ " + $t("Ferah")}</button
  >
  {#if modeApplies}
    <button class="chip" onclick={toggleMode} title={$t("Açık / Koyu")}
      >{$mode === "light" ? "☀" : "🌙"}</button
    >
  {/if}
  <button class="chip" onclick={toggleLang} title="Dil / Language">{$lang.toUpperCase()}</button>

  <span class="status" class:on={$connected}>
    {$connected ? "● " + $t("sidecar bağlı") : "○ " + $t("bağlanıyor…")}
  </span>
</header>

<style>
  /* Bu şerit başlık çubuğunun içinde yaşıyor: zemini, çerçevesi ve dikey
     dolgusu oradan gelir, burada tekrarlanmaz. */
  .topbar {
    display: flex;
    align-items: center;
    gap: var(--gap);
    padding: 0 var(--pad);
    font-family: var(--font);
  }
  .spacer { flex: 1; }
  .themes {
    display: flex;
    gap: 2px;
    padding: 2px;
    background: var(--btn);
    border-radius: var(--radius);
  }
  .chip {
    background: var(--btn);
    color: var(--muted);
    border: none;
    padding: 3px 9px;
    border-radius: var(--radius);
    cursor: pointer;
    font-family: var(--font);
    font-size: var(--fs-sm);
    line-height: 1.6;
  }
  .chip:hover { color: var(--fg); }
  .theme { background: transparent; }
  .theme.on {
    background: var(--accent);
    color: var(--accent-fg);
    font-weight: 600;
  }
  .status { color: var(--faint); font-size: var(--fs-sm); }
  .status.on { color: var(--ok); }
</style>

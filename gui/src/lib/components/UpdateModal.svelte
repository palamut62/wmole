<script lang="ts">
  import {
    update,
    modalOpen,
    downloadUpdate,
    applyUpdate,
    checkForUpdate,
    dismissUpdate,
  } from "$lib/updater";
  import { t } from "$lib/i18n";
</script>

{#if $modalOpen && $update.phase !== "idle" && $update.phase !== "checking"}
  <div class="overlay" role="dialog" aria-modal="true">
    <div class="card">
      {#if $update.phase === "available"}
        <h3>🚀 {$t("Yeni sürüm hazır")} v{$update.latest}</h3>
        <p class="cur">{$t("Mevcut")}: v{$update.current}</p>
        <pre class="notes">{$update.notes}</pre>
        <div class="row">
          <button class="ghost" onclick={dismissUpdate}>{$t("Sonra")}</button>
          <button class="primary" onclick={downloadUpdate}>{$t("İndir")}</button>
        </div>
      {:else if $update.phase === "downloading"}
        <h3>{$t("İndiriliyor")}… {$update.pct}%</h3>
        <div class="bar"><div class="fill" style="width:{$update.pct}%"></div></div>
      {:else if $update.phase === "ready"}
        <h3>✅ {$t("İndirme tamamlandı")}</h3>
        <p>{$t("Güncellemeyi uygulamak için uygulama yeniden başlatılacak.")}</p>
        <div class="row">
          <button class="ghost" onclick={dismissUpdate}>{$t("Sonra")}</button>
          <button class="primary" onclick={applyUpdate}
            >{$t("Yeniden başlat ve kur")}</button
          >
        </div>
      {:else if $update.phase === "error"}
        <h3>⚠ {$t("Güncelleme hatası")}</h3>
        <p class="err">{$update.error}</p>
        <div class="row">
          <button class="ghost" onclick={dismissUpdate}>{$t("Kapat")}</button>
          <button class="primary" onclick={() => checkForUpdate(false)}
            >{$t("Tekrar dene")}</button
          >
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1000;
  }
  .card {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 20px;
    width: 460px;
    max-width: 90vw;
    font-family: monospace;
    color: var(--fg);
  }
  h3 {
    margin: 0 0 10px;
  }
  .notes {
    max-height: 220px;
    overflow: auto;
    background: var(--btn);
    padding: 10px;
    border-radius: 6px;
    white-space: pre-wrap;
    font-size: 12px;
    margin: 0;
  }
  .row {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 14px;
  }
  .primary {
    background: #58d6a0;
    color: #04150d;
    border: none;
    padding: 7px 14px;
    border-radius: 5px;
    cursor: pointer;
    font-weight: bold;
    font-family: monospace;
  }
  .ghost {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 7px 14px;
    border-radius: 5px;
    cursor: pointer;
    font-family: monospace;
  }
  .bar {
    height: 12px;
    background: var(--btn);
    border-radius: 6px;
    overflow: hidden;
  }
  .fill {
    height: 100%;
    background: #58d6a0;
    transition: width 0.2s;
  }
  .err {
    color: #ff6b6b;
  }
  .cur {
    color: var(--faint);
    font-size: 12px;
  }
</style>

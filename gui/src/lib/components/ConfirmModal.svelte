<script lang="ts">
  let {
    open = false,
    count = 0,
    bytes = 0,
    facts = null,
    measuring = false,
    onConfirm,
    onCancel,
  }: {
    open?: boolean;
    count?: number;
    /** Logical size — kept visible, but it is not what the drive reclaims. */
    bytes?: number;
    /** Allocated-byte measurement of the exact selection, when available. */
    facts?: DiskFacts | null;
    measuring?: boolean;
    onConfirm: (permanent: boolean) => void;
    onCancel: () => void;
  } = $props();

  import { t } from "$lib/i18n";
  import type { DiskFacts } from "$lib/types";
  let permanent = $state(false);
  let confirmPermanent = $state(false);

  function fmt(n: number) {
    const u = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < u.length - 1) {
      v /= 1024;
      i++;
    }
    return v.toFixed(1) + " " + u[i];
  }
  function confirm() {
    if (permanent && !confirmPermanent) {
      confirmPermanent = true;
      return;
    }
    onConfirm(permanent);
  }
</script>

{#if open}
  <div class="backdrop" onclick={onCancel} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
      <h3>{$t("Silme Onayı")}</h3>
      <p>{count} {$t("öğe")} · {fmt(bytes)} <span class="dim">({$t("mantıksal")})</span></p>
      {#if measuring}
        <p class="dim">{$t("Diskte gerçekten boşalacak alan ölçülüyor…")}</p>
      {:else if facts}
        <p class="freed">
          {$t("Diskte gerçekten boşalacak")}: <strong>{fmt(facts.on_disk)}</strong>
          {#if facts.truncated}<span class="dim"> ({$t("en az")})</span>{/if}
        </p>
        {#if facts.placeholder_files}
          <p class="warn">
            ☁ {facts.placeholder_files} {$t("bulut yer tutucusu")}
            ({fmt(facts.placeholder_bytes)}) {$t("yalnızca bulutta duruyor: silmek yer açmaz.")}
          </p>
        {/if}
      {/if}
      <label class="opt">
        <input
          type="checkbox"
          bind:checked={permanent}
          onchange={() => (confirmPermanent = false)}
        />
        {$t("Kalıcı sil (Geri Dönüşüm Kutusu'nu atla)")}
      </label>
      {#if permanent && confirmPermanent}
        <p class="warn">
          {$t("⚠ Bu işlem GERİ ALINAMAZ. Onaylamak için tekrar \"Kalıcı Sil\" butonuna bas.")}
        </p>
      {/if}
      <div class="actions">
        <button class="ghost" onclick={onCancel}>{$t("Vazgeç")}</button>
        <button class:danger={permanent} onclick={confirm}>
          {permanent
            ? confirmPermanent
              ? $t("Kalıcı Sil (onayla)")
              : $t("Kalıcı Sil")
            : $t("Geri Dönüşüm Kutusu'na Taşı")}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.6);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
  }
  .modal {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: calc(var(--pad) * 1.6);
    min-width: 360px;
    font-family: var(--font);
    color: var(--fg);
  }
  h3 { margin: 0 0 12px; color: var(--ok); }
  .opt {
    display: flex;
    gap: 8px;
    align-items: center;
    margin: 12px 0;
    color: var(--muted);
  }
  .warn { color: var(--danger); font-size: var(--fs); }
  .dim { color: var(--faint); }
  .freed { color: var(--ok); margin: 4px 0; }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 16px;
  }
  button {
    border: none;
    padding: 8px 14px;
    border-radius: var(--radius);
    cursor: pointer;
    font-family: var(--font);
  }
  .ghost { background: var(--btn); color: var(--fg); }
  button.danger { background: var(--danger); color: white; }
  button:not(.ghost):not(.danger) { background: var(--accent); color: white; }
</style>

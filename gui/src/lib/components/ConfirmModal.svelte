<script lang="ts">
  let {
    open = false,
    count = 0,
    bytes = 0,
    onConfirm,
    onCancel,
  }: {
    open?: boolean;
    count?: number;
    bytes?: number;
    onConfirm: (permanent: boolean) => void;
    onCancel: () => void;
  } = $props();

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
      <h3>Silme Onayı</h3>
      <p>{count} öğe · {fmt(bytes)}</p>
      <label class="opt">
        <input
          type="checkbox"
          bind:checked={permanent}
          onchange={() => (confirmPermanent = false)}
        />
        Kalıcı sil (Geri Dönüşüm Kutusu'nu atla)
      </label>
      {#if permanent && confirmPermanent}
        <p class="warn">
          ⚠ Bu işlem GERİ ALINAMAZ. Onaylamak için tekrar "Kalıcı Sil" butonuna
          bas.
        </p>
      {/if}
      <div class="actions">
        <button class="ghost" onclick={onCancel}>Vazgeç</button>
        <button class:danger={permanent} onclick={confirm}>
          {permanent
            ? confirmPermanent
              ? "Kalıcı Sil (onayla)"
              : "Kalıcı Sil"
            : "Geri Dönüşüm Kutusu'na Taşı"}
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
    background: #11161c;
    border: 1px solid #1b2530;
    border-radius: 10px;
    padding: 22px;
    min-width: 360px;
    font-family: monospace;
    color: #e6edf3;
  }
  h3 { margin: 0 0 12px; color: #58d6a0; }
  .opt {
    display: flex;
    gap: 8px;
    align-items: center;
    margin: 12px 0;
    color: #9aa7b4;
  }
  .warn { color: #e5534b; font-size: 13px; }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 16px;
  }
  button {
    border: none;
    padding: 8px 14px;
    border-radius: 5px;
    cursor: pointer;
    font-family: monospace;
  }
  .ghost { background: #243140; color: #e6edf3; }
  button.danger { background: #e5534b; color: white; }
  button:not(.ghost):not(.danger) { background: #2ea043; color: white; }
</style>

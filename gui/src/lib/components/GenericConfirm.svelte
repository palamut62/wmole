<script lang="ts">
  let {
    open = false,
    title = "Onay",
    message = "",
    danger = false,
    confirmLabel = "Onayla",
    onConfirm,
    onCancel,
  }: {
    open?: boolean;
    title?: string;
    message?: string;
    danger?: boolean;
    confirmLabel?: string;
    onConfirm: () => void;
    onCancel: () => void;
  } = $props();
</script>

{#if open}
  <div class="backdrop" onclick={onCancel} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
      <h3 class:danger>{title}</h3>
      <p>{message}</p>
      <div class="actions">
        <button class="ghost" onclick={onCancel}>Vazgeç</button>
        <button class:danger onclick={onConfirm}>{confirmLabel}</button>
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
    max-width: 520px;
    font-family: monospace;
    color: #e6edf3;
  }
  h3 { margin: 0 0 12px; color: #58d6a0; }
  h3.danger { color: #e5534b; }
  p { color: #9aa7b4; white-space: pre-wrap; }
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

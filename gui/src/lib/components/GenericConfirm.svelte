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
  import { t } from "$lib/i18n";
</script>

{#if open}
  <div class="backdrop" onclick={onCancel} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="presentation">
      <h3 class:danger>{$t(title)}</h3>
      <p>{message}</p>
      <div class="actions">
        <button class="ghost" onclick={onCancel}>{$t("Vazgeç")}</button>
        <button class:danger onclick={onConfirm}>{$t(confirmLabel)}</button>
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
    border-radius: 10px;
    padding: 22px;
    min-width: 360px;
    max-width: 520px;
    font-family: monospace;
    color: var(--fg);
  }
  h3 { margin: 0 0 12px; color: #58d6a0; }
  h3.danger { color: #e5534b; }
  p { color: var(--muted); white-space: pre-wrap; }
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
  .ghost { background: var(--btn); color: var(--fg); }
  button.danger { background: #e5534b; color: white; }
  button:not(.ghost):not(.danger) { background: #2ea043; color: white; }
</style>

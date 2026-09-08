<script lang="ts">
  import { toasts } from "$lib/toast";
</script>

<div class="wrap">
  {#each $toasts as t (t.id)}
    <div class="toast {t.kind}">{t.text}</div>
  {/each}
</div>

<style>
  .wrap {
    position: fixed;
    bottom: 16px;
    right: 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    z-index: 50;
  }
  .toast {
    padding: 10px 14px;
    border-radius: 6px;
    font-family: var(--font);
    font-size: 13px;
    background: var(--border);
    color: var(--fg);
    border-left: 3px solid var(--ok);
  }
  .toast.err { border-left-color: var(--danger); }
  .toast.info { border-left-color: #539bf5; }
</style>

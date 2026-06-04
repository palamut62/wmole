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
    font-family: monospace;
    font-size: 13px;
    background: #1b2530;
    color: #e6edf3;
    border-left: 3px solid #58d6a0;
  }
  .toast.err { border-left-color: #e5534b; }
  .toast.info { border-left-color: #539bf5; }
</style>

<script lang="ts">
  let {
    label = "",
    done = 0,
    total = 0,
    onCancel = null,
  }: {
    label?: string;
    done?: number;
    total?: number;
    onCancel?: (() => void) | null;
  } = $props();
  let pct = $derived(total > 0 ? Math.round((done / total) * 100) : 0);
</script>

{#if total > 0}
  <footer class="statusbar">
    <div class="bar"><div class="fill" style="width:{pct}%"></div></div>
    <span class="text">{done}/{total} · {label}</span>
    {#if onCancel}<button onclick={onCancel}>İptal ✕</button>{/if}
  </footer>
{/if}

<style>
  .statusbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 14px;
    background: var(--bg);
    border-top: 1px solid var(--border);
    font-family: monospace;
    font-size: 12px;
  }
  .bar {
    flex: 1;
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    overflow: hidden;
  }
  .fill { height: 100%; background: #58d6a0; transition: width 0.15s; }
  .text {
    color: var(--muted);
    white-space: nowrap;
    max-width: 40%;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  button {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 3px 8px;
    border-radius: 4px;
    cursor: pointer;
    font-family: monospace;
  }
</style>

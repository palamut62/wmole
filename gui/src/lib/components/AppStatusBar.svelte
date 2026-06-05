<script lang="ts">
  import { connected, activity } from "$lib/sidecar";
  import { t } from "$lib/i18n";

  let { section = "" }: { section?: string } = $props();
  const VERSION = "v0.5.0";

  let pct = $derived(
    $activity.total > 0 ? Math.round(($activity.done / $activity.total) * 100) : 0,
  );
</script>

<footer class="appstatus">
  <span class="dot" class:on={$connected}></span>
  <span class="conn">{$connected ? $t("sidecar bağlı") : $t("bağlanıyor…")}</span>
  <span class="sep">·</span>
  <span class="section">{section}</span>

  <span class="spacer"></span>

  {#if $activity.busy}
    <span class="work">
      ⟳ {$t("Analiz ediliyor")}{$activity.label ? ": " + $activity.label : "…"}
      {#if $activity.total > 0}<span class="muted">({$activity.done}/{$activity.total})</span>{/if}
    </span>
    {#if $activity.total > 0}
      <span class="bar"><span class="fill" style="width:{pct}%"></span></span>
    {/if}
  {:else}
    <span class="ready">{$t("Hazır")}</span>
  {/if}

  <span class="sep">·</span>
  <span class="ver">{VERSION}</span>
</footer>

<style>
  .appstatus {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 14px;
    background: var(--bg);
    border-top: 1px solid var(--border);
    font-family: monospace;
    font-size: 12px;
    color: var(--muted);
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--faint); }
  .dot.on { background: #58d6a0; box-shadow: 0 0 6px #58d6a0; }
  .conn { color: var(--muted); }
  .section { color: var(--fg); }
  .sep { color: var(--faint); }
  .spacer { flex: 1; }
  .work { color: #58d6a0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45%; }
  .muted { color: var(--faint); }
  .ready { color: var(--faint); }
  .bar { width: 120px; height: 6px; background: var(--border); border-radius: 3px; overflow: hidden; }
  .fill { display: block; height: 100%; background: #58d6a0; transition: width 0.15s; }
  .ver { color: var(--faint); }
</style>

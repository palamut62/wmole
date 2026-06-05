<script lang="ts">
  import { onMount } from "svelte";
  import { getVersion } from "@tauri-apps/api/app";
  import { connected, activity } from "$lib/sidecar";
  import { update, openUpdateModal } from "$lib/updater";
  import { t } from "$lib/i18n";

  let { section = "" }: { section?: string } = $props();

  let version = $state("");
  onMount(async () => {
    try { version = await getVersion(); } catch {}
  });

  let pct = $derived(
    $activity.total > 0 ? Math.round(($activity.done / $activity.total) * 100) : 0,
  );

  // Güncelleme durumu: available/ready/downloading → status bar göstergesi.
  let updReady = $derived(
    $update.phase === "available" || $update.phase === "ready",
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
  <span class="ver">v{version || "…"}</span>

  {#if $update.phase === "downloading"}
    <button class="upd dl" title={$t("İndiriliyor")} onclick={openUpdateModal}>
      ⟳ {$update.pct}%
    </button>
  {:else if updReady}
    <button
      class="upd"
      title={$t("Yeni sürüm hazır") + " v" + $update.latest}
      onclick={openUpdateModal}
      aria-label={$t("Yeni sürüm hazır")}
    >
      ⬇ v{$update.latest}
    </button>
  {/if}
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
  .upd {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    background: #58d6a0;
    color: #04150d;
    border: none;
    padding: 2px 8px;
    border-radius: 10px;
    cursor: pointer;
    font-family: monospace;
    font-size: 11px;
    font-weight: bold;
    animation: pulse 2s ease-in-out infinite;
  }
  .upd.dl { background: var(--btn); color: var(--fg); animation: none; }
  @keyframes pulse {
    0%, 100% { box-shadow: 0 0 0 0 rgba(88, 214, 160, 0.5); }
    50% { box-shadow: 0 0 6px 2px rgba(88, 214, 160, 0.5); }
  }
</style>

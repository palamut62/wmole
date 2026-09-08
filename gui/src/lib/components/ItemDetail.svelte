<script lang="ts">
  // Per-item detail: the six questions, the risk breakdown, and what the
  // delete really frees. Everything here is fetched on demand for one item —
  // scoring every scan candidate up front would cost a stat and a git probe
  // per row.
  import { request } from "$lib/sidecar";
  import type { Explanation, Risk, DiskFacts } from "$lib/types";
  import { t, tr } from "$lib/i18n";

  let { path, kind = "" }: { path: string | null; kind?: string } = $props();

  let explain = $state<Explanation | null>(null);
  let risk = $state<Risk | null>(null);
  let facts = $state<DiskFacts | null>(null);
  let loading = $state(false);
  let measuring = $state(false);

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

  const freedLabel: Record<string, string> = {
    immediate: "hemen",
    "quarantine-expiry": "karantina süresi dolunca",
    "empty-recycle-bin": "Geri Dönüşüm Kutusu boşaltılınca",
    reboot: "yeniden başlatmada",
  };

  $effect(() => {
    const target = path;
    explain = null;
    risk = null;
    facts = null;
    if (!target) return;
    loading = true;
    request({ op: "explain", path: target, kind, risk: true, check_git: true }, () => {})
      .then((e) => {
        const row = (e.payload as any)?.items?.[0];
        if (row) {
          explain = row.explain as Explanation;
          risk = row.risk as Risk;
        }
      })
      .finally(() => (loading = false));
  });

  async function measure() {
    if (!path) return;
    measuring = true;
    try {
      const e = await request({ op: "disk_facts", paths: [path] }, () => {});
      facts = ((e.payload as any)?.total ?? null) as DiskFacts | null;
    } finally {
      measuring = false;
    }
  }
</script>

{#if path}
  <aside class="detail">
    <header>
      <span class="path" title={path}>{path}</span>
      {#if risk}
        <span class="score {risk.level}">{risk.score}/100 · {$t(risk.level)}</span>
      {/if}
    </header>

    {#if loading}
      <p class="muted">{$t("Yükleniyor")}…</p>
    {:else if explain}
      <dl>
        <dt>{$t("Bu ne?")}</dt>
        <dd>{explain.what}</dd>
        <dt>{$t("Ne kaybolur?")}</dt>
        <dd>{explain.lost}</dd>
        <dt>{$t("Ne kalır?")}</dt>
        <dd>{explain.kept}</dd>
        <dt>{$t("Nasıl yeniden oluşturulur?")}</dt>
        <dd>
          {#if explain.rebuild}
            <code>{explain.rebuild}</code>
            <span class="note">{$t("yalnızca gösterilir — wmole bu komutu asla çalıştırmaz")}</span>
          {:else}
            <span class="warn">{$t("Hiçbir komut bunu geri getirmez.")}</span>
          {/if}
        </dd>
        <dt>{$t("Alan ne zaman boşalır?")}</dt>
        <dd>{$t(freedLabel[explain.when_freed] ?? explain.when_freed)}</dd>
        <dt>{$t("Geri alınabilir mi?")}</dt>
        <dd>{explain.reversible}</dd>
      </dl>
    {/if}

    {#if risk && risk.modifiers.length}
      <section>
        <h4>{$t("Risk puanı nereden geliyor")}</h4>
        <ul class="mods">
          {#each risk.modifiers as m}
            <li>
              <span class="pts" class:neg={m.points < 0}>
                {m.points >= 0 ? "+" : ""}{m.points}
              </span>
              <span>{m.label}</span>
            </li>
          {/each}
        </ul>
      </section>
    {/if}

    <section>
      <h4>{$t("Diskte gerçekten kapladığı")}</h4>
      {#if facts}
        <p>
          <strong>{fmt(facts.on_disk)}</strong>
          <span class="muted">
            ({$t("mantıksal")} {fmt(facts.logical)}, {facts.files} {$t("dosya")})
          </span>
        </p>
        {#if facts.placeholder_files}
          <p class="warn">
            ☁ {facts.placeholder_files}
            {$t("bulut yer tutucusu")} ({fmt(facts.placeholder_bytes)}) —
            {$t("silmek yer açmaz")}
          </p>
        {/if}
        {#if facts.truncated}
          <p class="muted">{$t("Ölçüm bütçesi doldu; gerçek rakam daha yüksek olabilir.")}</p>
        {/if}
      {:else}
        <button onclick={measure} disabled={measuring}>
          {measuring ? $t("Ölçülüyor…") : $t("Ölç")}
        </button>
        <span class="note">{$t("küme yuvarlaması, seyrek dosyalar ve bulut yer tutucuları dahil")}</span>
      {/if}
    </section>
  </aside>
{/if}

<style>
  .detail {
    border: 1px solid var(--border);
    background: var(--panel);
    border-radius: var(--radius-lg);
    padding: var(--pad);
    font-family: var(--font);
    font-size: var(--fs);
    color: var(--fg);
    overflow: auto;
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: center;
    border-bottom: 1px solid var(--border);
    padding-bottom: 8px;
    margin-bottom: 10px;
  }
  .path {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    direction: rtl;
    text-align: left;
  }
  .score {
    flex: none;
    padding: 2px 8px;
    border-radius: 999px;
    font-weight: bold;
  }
  .score.low { background: #1f6f4a; color: #d7ffe9; }
  .score.moderate { background: #7a5d12; color: #ffeec2; }
  .score.high { background: #8a3b22; color: #ffd9c9; }
  .score.critical { background: #8a1f1f; color: #ffd5d5; }
  dl { margin: 0; display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; }
  dt { color: var(--faint); white-space: nowrap; }
  dd { margin: 0; color: var(--fg); }
  code {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1px 6px;
  }
  .note { color: var(--faint); margin-left: 6px; }
  .muted { color: var(--muted); }
  .warn { color: var(--warn); }
  section { margin-top: 12px; }
  h4 { margin: 0 0 6px; color: var(--faint); font-weight: normal; text-transform: uppercase; font-size: var(--fs-sm); }
  .mods { list-style: none; margin: 0; padding: 0; }
  .mods li { display: flex; gap: 10px; }
  .pts { min-width: 42px; text-align: right; color: var(--danger); }
  .pts.neg { color: var(--ok); }
  button {
    background: var(--btn);
    color: var(--fg);
    border: none;
    padding: 4px 12px;
    border-radius: var(--radius);
    cursor: pointer;
    font-family: var(--font);
  }
  button:disabled { opacity: 0.5; cursor: default; }
</style>

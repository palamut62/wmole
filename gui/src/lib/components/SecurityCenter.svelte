<script lang="ts">
  import { onMount } from "svelte";
  import { request } from "$lib/sidecar";
  import { toast } from "$lib/toast";
  import { t, tr } from "$lib/i18n";

  interface QuarantineEntry { id: string; original: string; name: string; size: number; created: number; }
  interface SecretFinding { type: string; path: string; line: number; preview: string; severity: string; }
  interface AuditResult { project: string; tool: string; status: string; code?: number; output: string; }
  interface LogEntry { timestamp: string; event: string; details: Record<string, unknown>; signature: string; }

  let root = $state("");
  let loading = $state(false);
  let quarantine = $state<QuarantineEntry[]>([]);
  let logs = $state<LogEntry[]>([]);
  let logValid = $state(true);
  let protectedProcesses = $state<string[]>([]);
  let quarantineEnabled = $state(true);
  let retentionDays = $state(30);
  let newProcess = $state("");
  let secrets = $state<{ scanned: number; findings: SecretFinding[]; truncated?: boolean } | null>(null);
  let supply = $state<{ sbom: Array<{ path: string; name: string; sha256: string }>; audits: AuditResult[] } | null>(null);

  function fmt(n: number) {
    const units = ["B", "KB", "MB", "GB"]; let value = n || 0; let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    return `${value.toFixed(1)} ${units[index]}`;
  }

  async function load() {
    const result = await request({ op: "security_overview", limit: 100 });
    const payload: any = result.payload ?? {};
    quarantine = payload.quarantine ?? [];
    logs = [...(payload.security_log ?? [])].reverse();
    logValid = payload.log_valid !== false;
    protectedProcesses = payload.config?.protected_processes ?? [];
    quarantineEnabled = payload.config?.quarantine_enabled !== false;
    retentionDays = payload.config?.quarantine_retention_days ?? 30;
    if (!root) root = payload.config?.analyze_start_path ?? "";
  }

  async function scanSecrets() {
    loading = true;
    try {
      const result = await request({ op: "secret_scan", path: root });
      secrets = (result.payload as any) ?? { scanned: 0, findings: [] };
      toast(`${secrets?.findings.length ?? 0} ${tr("secret bulgusu")}`, secrets?.findings.length ? "err" : "ok");
      await load();
    } finally { loading = false; }
  }

  async function auditSupply() {
    loading = true;
    try {
      const result = await request({ op: "supply_audit", path: root });
      supply = (result.payload as any) ?? { sbom: [], audits: [] };
      toast(tr("Bağımlılık denetimi tamamlandı"), "ok");
      await load();
    } finally { loading = false; }
  }

  async function restore(id: string) {
    const result = await request({ op: "quarantine_restore", entry_id: id });
    toast(tr(result.ok ? "Karantina öğesi geri yüklendi" : "Geri yükleme başarısız"), result.ok ? "ok" : "err");
    await load();
  }

  async function saveProtection() {
    await request({ op: "settings_set", config: {
      quarantine_enabled: quarantineEnabled,
      quarantine_retention_days: Number(retentionDays),
      protected_processes: protectedProcesses,
    }});
    toast(tr("Koruma ayarları kaydedildi"), "ok");
    await load();
  }

  function addProcess() {
    const name = newProcess.trim().toLowerCase();
    if (name && !protectedProcesses.includes(name)) protectedProcesses = [...protectedProcesses, name];
    newProcess = "";
  }

  onMount(load);
</script>

<div class="security">
  <div class="head">
    <div><h2>{$t("Güvenlik Merkezi")}</h2><p>Git, secret, bağımlılık, süreç, karantina ve denetim korumaları</p></div>
    <span class:bad={!logValid} class="integrity">{logValid ? "✓ LOG ZİNCİRİ DOĞRULANDI" : "⚠ LOG BÜTÜNLÜĞÜ BOZUK"}</span>
  </div>

  <div class="cards">
    <section><strong>Git-aware koruma</strong><span>Dirty ve untracked dosyalar silinmeden önce bloklanır.</span></section>
    <section><strong>Karantina</strong><span>{quarantine.length} öğe, {retentionDays} gün saklama</span></section>
    <section><strong>Süreç koruması</strong><span>{protectedProcesses.length} geliştirici süreci</span></section>
    <section><strong>İmzalı günlük</strong><span>{logs.length} son güvenlik olayı</span></section>
  </div>

  <section class="panel">
    <h3>Proje Güvenlik Taraması</h3>
    <div class="row"><input bind:value={root} placeholder="C:\projeler" /><button onclick={scanSecrets} disabled={loading}>Secret Tara</button><button onclick={auditSupply} disabled={loading}>Supply-chain + SBOM</button></div>
    {#if secrets}
      <p class="summary">{secrets.scanned} dosya tarandı, <b>{secrets.findings.length}</b> bulgu.</p>
      <div class="results">
        {#each secrets.findings as finding}
          <div class="finding"><b>{finding.type}</b><span>{finding.path}:{finding.line}</span><code>{finding.preview}</code></div>
        {/each}
      </div>
    {/if}
    {#if supply}
      <p class="summary">SBOM: {supply.sbom.length} manifest/lockfile, denetim: {supply.audits.length}</p>
      <div class="results">
        {#each supply.audits as audit}
          <details class:danger={audit.status === "findings"}><summary>{audit.tool} - {audit.status} - {audit.project}</summary><pre>{audit.output || "Çıktı yok"}</pre></details>
        {/each}
      </div>
    {/if}
  </section>

  <section class="panel">
    <h3>Geri Alma Kasası</h3>
    {#if !quarantine.length}<p class="muted">Karantinada öğe yok.</p>{/if}
    {#each quarantine as entry}
      <div class="entry"><div><b>{entry.name}</b><span>{entry.original} · {fmt(entry.size)} · {new Date(entry.created * 1000).toLocaleString()}</span></div><button onclick={() => restore(entry.id)}>Geri Yükle</button></div>
    {/each}
  </section>

  <section class="panel">
    <h3>Koruma Politikası</h3>
    <div class="row"><label><input type="checkbox" bind:checked={quarantineEnabled} /> Güvenli silmeleri karantinaya al</label><label>Saklama günü <input class="small" type="number" min="1" bind:value={retentionDays} /></label></div>
    <div class="chips">{#each protectedProcesses as name, i}<span>{name}<button onclick={() => protectedProcesses = protectedProcesses.filter((_, x) => x !== i)}>×</button></span>{/each}</div>
    <div class="row"><input bind:value={newProcess} placeholder="ör. idea64.exe" onkeydown={(e) => e.key === "Enter" && addProcess()} /><button onclick={addProcess}>Süreç Ekle</button><button class="primary" onclick={saveProtection}>Kaydet</button></div>
  </section>

  <section class="panel">
    <h3>Güvenlik Günlüğü</h3>
    <div class="results log">{#each logs as item}<div class="event"><time>{item.timestamp}</time><b>{item.event}</b><span>{JSON.stringify(item.details)}</span></div>{/each}</div>
  </section>
</div>

<style>
  .security { display: flex; flex-direction: column; gap: 12px; font-family: monospace; overflow: auto; }
  .head { display: flex; justify-content: space-between; align-items: center; } h2, h3 { margin: 0; } .head p { color: var(--muted); margin: 5px 0 0; }
  .integrity { color: #58d6a0; font-size: 11px; } .integrity.bad, .danger summary { color: #e5534b; }
  .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; } .cards section, .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 13px; }
  .cards strong { color: #58d6a0; display: block; margin-bottom: 6px; } .cards span, .muted, .summary { color: var(--muted); font-size: 12px; }
  .panel { display: flex; flex-direction: column; gap: 10px; } .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  input { background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 7px 10px; border-radius: 5px; font-family: monospace; flex: 1; } input.small { max-width: 70px; }
  button { background: var(--btn); color: var(--fg); border: 0; border-radius: 5px; padding: 7px 12px; cursor: pointer; font-family: monospace; } button.primary { background: #2ea043; color: white; }
  .results { max-height: 260px; overflow: auto; display: flex; flex-direction: column; gap: 5px; } .finding, .event, .entry { display: flex; align-items: center; gap: 10px; border-top: 1px solid var(--border); padding: 7px 0; }
  .finding span, .event span, .entry span { color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; } .finding span, .event span { flex: 1; } code { color: #d29922; }
  .entry { justify-content: space-between; } .entry div { min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  details { border-top: 1px solid var(--border); padding: 7px 0; } pre { white-space: pre-wrap; max-height: 180px; overflow: auto; color: var(--muted); font-size: 11px; }
  .chips { display: flex; flex-wrap: wrap; gap: 5px; } .chips span { background: var(--btn); border-radius: 12px; padding: 4px 6px 4px 10px; color: var(--muted); } .chips button { padding: 0 4px; background: none; color: #e5534b; }
  time { color: var(--faint); font-size: 10px; } .log { max-height: 300px; }
  @media (max-width: 900px) { .cards { grid-template-columns: repeat(2, 1fr); } }
</style>

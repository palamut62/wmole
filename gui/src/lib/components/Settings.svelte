<script lang="ts">
  import { onMount } from "svelte";
  import { invoke } from "@tauri-apps/api/core";
  import { request } from "$lib/sidecar";
  import { toast } from "$lib/toast";
  import { t } from "$lib/i18n";

  let whitelist = $state<string[]>([]);
  let denylist = $state<string[]>([]);
  let purgePaths = $state<string[]>([]);
  let largeMinMb = $state(512);
  let isAdmin = $state(false);
  let schedule = $state<{ enabled: boolean; detail: string }>({ enabled: false, detail: "" });
  let schedDay = $state("SUN");
  let schedTime = $state("20:00");

  let newWl = $state("");
  let newDl = $state("");
  let newPp = $state("");

  async function load() {
    const s = await request({ op: "settings_get" });
    const p: any = s.payload ?? {};
    whitelist = p.whitelist ?? [];
    denylist = p.denylist ?? [];
    purgePaths = p.purge_paths ?? [];
    largeMinMb = p.config?.large_file_min_mb ?? 512;
    const a = await request({ op: "is_admin" });
    isAdmin = (a.payload as any)?.is_admin ?? false;
    const sc = await request({ op: "schedule_get" });
    schedule = (sc.payload as any) ?? { enabled: false, detail: "" };
  }
  onMount(load);

  async function save() {
    await request({
      op: "settings_set",
      whitelist,
      denylist,
      purge_paths: purgePaths,
      config: { large_file_min_mb: Number(largeMinMb) },
    });
    toast("Ayarlar kaydedildi", "ok");
  }

  function addTo(list: "wl" | "dl" | "pp") {
    if (list === "wl" && newWl.trim()) { whitelist = [...whitelist, newWl.trim()]; newWl = ""; }
    if (list === "dl" && newDl.trim()) { denylist = [...denylist, newDl.trim()]; newDl = ""; }
    if (list === "pp" && newPp.trim()) { purgePaths = [...purgePaths, newPp.trim()]; newPp = ""; }
  }
  function rm(list: "wl" | "dl" | "pp", i: number) {
    if (list === "wl") whitelist = whitelist.filter((_, x) => x !== i);
    if (list === "dl") denylist = denylist.filter((_, x) => x !== i);
    if (list === "pp") purgePaths = purgePaths.filter((_, x) => x !== i);
  }

  async function installCompletion() {
    const d = await request({ op: "completion_install" });
    toast(d.ok ? "PowerShell completion kuruldu" : "Kurulum başarısız", d.ok ? "ok" : "err");
  }
  async function elevate() {
    try {
      await invoke("relaunch_admin");
    } catch (e) {
      toast("Yükseltme başarısız: " + e, "err");
    }
  }
  async function setSchedule() {
    const d = await request({ op: "schedule_set", day: schedDay, time: schedTime });
    toast(d.ok ? "Haftalık temizlik planlandı" : "Planlama başarısız", d.ok ? "ok" : "err");
    load();
  }
  async function clearSchedule() {
    const d = await request({ op: "schedule_clear" });
    toast(d.ok ? "Zamanlama kaldırıldı" : "Kaldırma başarısız", d.ok ? "ok" : "err");
    load();
  }
</script>

{#snippet listEditor(title: string, items: string[], which: "wl" | "dl" | "pp", val: string, setVal: (v: string) => void, ph: string)}
  <section class="card">
    <h3>{$t(title)}</h3>
    <ul class="paths">
      {#each items as p, i}
        <li><span>{p}</span><button class="mini danger" onclick={() => rm(which, i)}>✕</button></li>
      {/each}
      {#if !items.length}<li class="muted">{$t("Boş")}</li>{/if}
    </ul>
    <div class="row">
      <input placeholder={ph} value={val} oninput={(e) => setVal((e.target as HTMLInputElement).value)} onkeydown={(e) => e.key === "Enter" && addTo(which)} />
      <button onclick={() => addTo(which)}>{$t("Ekle")}</button>
    </div>
  </section>
{/snippet}

<div class="settings">
  <div class="head">
    <h2>{$t("Ayarlar")}</h2>
    <button class="primary" onclick={save}>{$t("Kaydet")}</button>
  </div>

  <section class="card">
    <h3>{$t("Yönetici Durumu")}</h3>
    <p class={isAdmin ? "green" : "muted"}>{isAdmin ? $t("✓ Yönetici olarak çalışıyor") : $t("○ Standart kullanıcı — bazı optimize işlemleri admin gerektirir")}</p>
    {#if !isAdmin}<button onclick={elevate}>{$t("Yönetici Olarak Yeniden Başlat")}</button>{/if}
  </section>

  <section class="card">
    <h3>{$t("Eşikler")}</h3>
    <label class="field">{$t("Büyük dosya alt sınırı (MB)")}
      <input type="number" bind:value={largeMinMb} min="1" />
    </label>
  </section>

  {@render listEditor("Whitelist (korunan yollar)", whitelist, "wl", newWl, (v) => (newWl = v), "C:\\korunacak\\yol")}
  {@render listEditor("Denylist (asla silme)", denylist, "dl", newDl, (v) => (newDl = v), "C:\\engellenen\\yol")}
  {@render listEditor("Purge kök yolları", purgePaths, "pp", newPp, (v) => (newPp = v), "C:\\src;D:\\repos")}

  <section class="card">
    <h3>{$t("Zamanlanmış Haftalık Temizlik")}</h3>
    <p class={schedule.enabled ? "green" : "muted"}>{schedule.enabled ? $t("✓ Etkin (Windows Task Scheduler)") : $t("○ Devre dışı")}</p>
    <div class="row">
      <select bind:value={schedDay}>
        {#each [["SUN", "Pazar"], ["MON", "Pazartesi"], ["TUE", "Salı"], ["WED", "Çarşamba"], ["THU", "Perşembe"], ["FRI", "Cuma"], ["SAT", "Cumartesi"]] as [v, l]}
          <option value={v}>{$t(l)}</option>
        {/each}
      </select>
      <input type="time" bind:value={schedTime} />
      <button class="primary" onclick={setSchedule}>{$t("Planla")}</button>
      {#if schedule.enabled}<button class="danger" onclick={clearSchedule}>{$t("Kaldır")}</button>{/if}
    </div>
  </section>

  <section class="card">
    <h3>{$t("PowerShell Tamamlama")}</h3>
    <p class="muted">{$t("PowerShell Tamamlama")} — <code>wmole</code></p>
    <button onclick={installCompletion}>{$t("Completion Kur")}</button>
  </section>
</div>

<style>
  .settings { display: flex; flex-direction: column; gap: 14px; font-family: monospace; overflow: auto; }
  .head { display: flex; align-items: center; justify-content: space-between; }
  h2 { margin: 0; color: var(--fg, var(--fg)); }
  h3 { margin: 0 0 10px; color: var(--accent, #58d6a0); font-size: 14px; }
  .card { background: var(--panel, var(--panel)); border: 1px solid var(--border, var(--border)); border-radius: 8px; padding: 14px; }
  .muted { color: var(--faint); }
  .green { color: #58d6a0; }
  code { color: #d29922; }
  .field { display: flex; flex-direction: column; gap: 6px; color: var(--muted); max-width: 240px; }
  .paths { list-style: none; padding: 0; margin: 0 0 10px; display: flex; flex-direction: column; gap: 4px; }
  .paths li { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: var(--muted); }
  .row { display: flex; gap: 8px; align-items: center; }
  input, select { background: var(--bg); border: 1px solid var(--border); color: var(--fg); padding: 6px 10px; border-radius: 4px; font-family: monospace; }
  input[type="text"], .row input:not([type]) { flex: 1; }
  button { background: var(--btn); color: var(--fg); border: none; padding: 7px 14px; border-radius: 5px; cursor: pointer; font-family: monospace; }
  button.primary { background: #2ea043; color: white; }
  button.danger { background: #e5534b; color: white; }
  button.mini { padding: 2px 8px; font-size: 12px; }
</style>

<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { request } from "$lib/sidecar";

  let s = $state<Record<string, any>>({});
  let timer: ReturnType<typeof setInterval>;

  async function poll() {
    const done = await request({ op: "status" });
    if (done.payload) s = done.payload as Record<string, any>;
  }
  function gb(n: number) {
    return (n / 1024 ** 3).toFixed(1) + " GB";
  }

  onMount(() => {
    poll();
    timer = setInterval(poll, 2000);
  });
  onDestroy(() => clearInterval(timer));
</script>

<div class="grid">
  <div class="card">
    <h3>CPU</h3>
    <div class="big">{s.cpu_percent ?? "—"}%</div>
  </div>
  <div class="card">
    <h3>Bellek</h3>
    <div class="big">{s.memory_percent ?? "—"}%</div>
    <small
      >{s.memory_used ? gb(s.memory_used) : ""}
      {s.memory_total ? "/ " + gb(s.memory_total) : ""}</small
    >
  </div>
  <div class="card">
    <h3>Disk</h3>
    <div class="big">{s.disk_percent ?? "—"}%</div>
    <small>{s.disk_free ? gb(s.disk_free) + " boş" : ""}</small>
  </div>
  <div class="card">
    <h3>Sağlık</h3>
    <div class="big">{s.health ?? "—"}</div>
  </div>
  <div class="card">
    <h3>Uptime</h3>
    <div class="big">
      {s.uptime_seconds ? Math.floor(s.uptime_seconds / 3600) + "s" : "—"}
    </div>
  </div>
  <div class="card">
    <h3>Batarya</h3>
    <div class="big">
      {s.battery_percent ?? "—"}{s.battery_percent != null ? "%" : ""}
    </div>
    <small>{s.power_plugged ? "şarjda" : ""}</small>
  </div>
</div>

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
  }
  .card {
    background: #11161c;
    border: 1px solid #1b2530;
    border-radius: 8px;
    padding: 14px;
    font-family: monospace;
  }
  h3 {
    margin: 0 0 8px;
    color: #6e7681;
    font-size: 12px;
    text-transform: uppercase;
  }
  .big { font-size: 28px; color: #58d6a0; }
  small { color: #9aa7b4; }
</style>

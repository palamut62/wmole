<script lang="ts">
  import { connected } from "$lib/sidecar";
</script>

<header class="topbar">
  <span class="brand">wmole</span>
  <span class="spacer"></span>
  <span class="status" class:on={$connected}>
    {$connected ? "● sidecar bağlı" : "○ bağlanıyor…"}
  </span>
</header>

<style>
  .topbar {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 8px 14px;
    background: #0d1117;
    border-bottom: 1px solid #1b2530;
    font-family: monospace;
  }
  .brand { color: #58d6a0; font-weight: bold; }
  .spacer { flex: 1; }
  .status { color: #6e7681; font-size: 12px; }
  .status.on { color: #58d6a0; }
</style>

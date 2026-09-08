<script lang="ts">
  /**
   * Uygulamanın kendi başlık çubuğu.
   *
   * Pencere `decorations: false` ile açılır. Windows'un yerel çubuğu temayı
   * izlemediği için Cards ya da HUD seçiliyken pencerenin üst şeridi koyu gri
   * kalıyor ve gövdeyle uyumsuz görünüyordu. Çubuğu kendimiz çizince tema
   * pencerenin tamamına yayılır — renkler token'lardan geldiği için ayrıca
   * tema başına dallanmaya gerek yok.
   *
   * Yerel çubuğu kaldırmak taşıma ve pencere düğmelerini de kaldırır; ikisi de
   * burada elle geri verilir.
   */
  import { onMount } from "svelte";
  import { t } from "$lib/i18n";
  import TopBar from "./TopBar.svelte";

  let { title = "wmole" }: { title?: string } = $props();

  let maximized = $state(false);
  // Tarayıcıda (vite dev) Tauri API'si yok; çubuk görünür ama düğmeler gizlenir.
  let inTauri = $state(false);

  async function win() {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    return getCurrentWindow();
  }

  onMount(() => {
    inTauri = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
    if (!inTauri) return;
    let disposed = false;
    let stop: (() => void) | undefined;

    void (async () => {
      const w = await win();
      const sync = async () => {
        const value = await w.isMaximized();
        if (!disposed) maximized = value;
      };
      await sync();
      // Pencere kenardan da büyütülebilir; ikon gerçeği göstersin diye olayı dinle.
      stop = await w.onResized(() => void sync());
    })();

    return () => {
      disposed = true;
      stop?.();
    };
  });

  const minimize = async () => (await win()).minimize();
  const toggle = async () => (await win()).toggleMaximize();
  const close = async () => (await win()).close();

  /**
   * Sürüklemeyi elle başlat.
   *
   * `data-tauri-drag-region` işareti tek başına güvenilir olmadı: çubuğun
   * içinde iç içe bileşenler var ve mousedown çoğu zaman işareti taşımayan bir
   * alt öğeye düşüyor. `startDragging()` doğrudan çağrılınca pencere her
   * durumda taşınır. Düğme/giriş öğeleri hariç tutulur, yoksa tema çipine
   * tıklarken pencere kayar.
   */
  function onPointerDown(event: MouseEvent) {
    if (!inTauri || event.button !== 0) return;
    const el = event.target as HTMLElement | null;
    if (el?.closest("button, input, select, textarea, a, label")) return;
    event.preventDefault();
    void win().then((w) => w.startDragging());
  }

  /** Başlık çubuğuna çift tıklamak büyütme durumunu değiştirir (Windows adeti). */
  function onDoubleClick(event: MouseEvent) {
    if (!inTauri) return;
    const el = event.target as HTMLElement | null;
    if (el?.closest("button, input, select, textarea, a, label")) return;
    void toggle();
  }
</script>

<!-- Başlık, uygulama denetimleri ve pencere düğmeleri tek şeritte. Ayrı bir
     başlık çubuğu + ayrı bir araç çubuğu, kompakt düzende iki sıra yer yerdi. -->
<div
  class="titlebar"
  data-tauri-drag-region
  role="presentation"
  onmousedown={onPointerDown}
  ondblclick={onDoubleClick}
>
  <span class="title" data-tauri-drag-region>{title}</span>
  <div class="mid"><TopBar /></div>
  {#if inTauri}
    <div class="controls">
      <button class="ctl" aria-label={$t("Küçült")} title={$t("Küçült")} onclick={minimize}>
        <svg viewBox="0 0 16 16" aria-hidden="true"><rect x="3" y="7.5" width="10" height="1" /></svg>
      </button>
      <button
        class="ctl"
        aria-label={maximized ? $t("Geri yükle") : $t("Büyüt")}
        title={maximized ? $t("Geri yükle") : $t("Büyüt")}
        onclick={toggle}
      >
        <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor">
          {#if maximized}
            <rect x="3" y="5.5" width="7.5" height="7.5" />
            <path d="M5.5 5.5V3h7.5v7.5h-2.5" />
          {:else}
            <rect x="3.5" y="3.5" width="9" height="9" />
          {/if}
        </svg>
      </button>
      <button class="ctl danger" aria-label={$t("Kapat")} title={$t("Kapat")} onclick={close}>
        <svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor">
          <path d="M4 4l8 8M12 4l-8 8" />
        </svg>
      </button>
    </div>
  {/if}
</div>

<style>
  .titlebar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 32px;
    flex: none;
    user-select: none;
    padding-left: 12px;
    background: var(--panel);
    border-bottom: 1px solid var(--border);
    backdrop-filter: var(--panel-blur);
    color: var(--fg);
    font-family: var(--font);
  }
  .title {
    font-size: var(--fs-sm);
    font-weight: 600;
    opacity: 0.8;
    /* Yazının üstünde sürükleme çalışsın diye tıklamayı çubuğa bırak. */
    pointer-events: none;
  }
  .mid { flex: 1; min-width: 0; display: flex; justify-content: flex-end; }
  .controls { display: flex; height: 100%; }
  .ctl {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 44px;
    height: 100%;
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    transition: background 0.12s, color 0.12s;
  }
  .ctl svg { width: 15px; height: 15px; fill: currentColor; stroke-width: 1; }
  .ctl:hover { background: var(--btn); color: var(--fg); }
  .ctl.danger:hover { background: var(--danger); color: #fff; }
</style>

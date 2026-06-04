<script lang="ts">
  import type { Snippet } from "svelte";
  let {
    items = [],
    rowHeight = 26,
    row,
  }: {
    items: any[];
    rowHeight?: number;
    row: Snippet<[any]>;
  } = $props();

  let scrollTop = $state(0);
  let height = $state(400);
  let viewport: HTMLDivElement;

  let start = $derived(Math.max(0, Math.floor(scrollTop / rowHeight) - 5));
  let visibleCount = $derived(Math.ceil(height / rowHeight) + 10);
  let slice = $derived(items.slice(start, start + visibleCount));
</script>

<div
  class="vp"
  bind:this={viewport}
  bind:clientHeight={height}
  onscroll={() => (scrollTop = viewport.scrollTop)}
>
  <div style="height:{items.length * rowHeight}px; position:relative;">
    {#each slice as item, i (item.path)}
      <div
        class="row"
        style="position:absolute; top:{(start + i) *
          rowHeight}px; height:{rowHeight}px;"
      >
        {@render row(item)}
      </div>
    {/each}
  </div>
</div>

<style>
  .vp { overflow: auto; height: 100%; }
  .row { left: 0; right: 0; display: flex; align-items: center; }
</style>

<script lang="ts">
  const sections = [
    {
      title: "Temizlik",
      items: [
        ["Dashboard", "Sistem özeti, temizlik analizi ve geçmiş. Tek tıkla 'Hızlı Temizle'."],
        ["Analyze", "Dosya sistemini canlı boyutlarla gez; klasöre çift girip yukarı çık, büyük dosyaları ve sürücüleri gör."],
        ["Categories", "Disk kullanımını kategoriye göre (cache, tarayıcı, sistem) listele."],
        ["Clean", "Güvenli temizlik kategorilerini tara ve Geri Dönüşüm Kutusu'na taşı."],
        ["Purge", "node_modules / build / dist gibi geliştirici artıklarını toplu sil."],
        ["Installers", "İndirilenler ve Masaüstü'ndeki eski kurulum dosyalarını bul."],
      ],
    },
    {
      title: "Sistem",
      items: [
        ["Uninstall", "Kurulu programları listele, kaldır, ve kalıntı dosya/registry'yi temizle."],
        ["Optimize", "DNS flush, Winsock/IP reset, Prefetch, Windows Update reset vb. (Dry-run destekli)."],
        ["Ports", "Dinleyen geliştirici portlarını listele ve süreçleri kapat."],
        ["Maintenance", "Güncelleme kontrolü/uygulama ve ~/.wmole durumunu kaldırma."],
      ],
    },
    {
      title: "Güvenlik",
      items: [
        ["Varsayılan: Geri Dönüşüm Kutusu", "Silmeler varsayılan olarak Geri Dönüşüm Kutusu'na gider; kalıcı silme için modalda ek onay gerekir."],
        ["Korumalı yollar", "Sürücü kökleri, C:\\Windows, Program Files, kullanıcı profili ve .git içeren yollar engellenir."],
        ["İptal edilebilir", "Uzun işlemler alt şeritteki 'İptal' ile durdurulabilir; arayüz hiç donmaz."],
        ["İşlem günlüğü", "Tüm silmeler ~/.wmole/logs/operations.log dosyasına yazılır."],
      ],
    },
  ];
</script>

<div class="help">
  <h2>Yardım & Özellikler</h2>
  <p class="intro">
    wmole, Windows için bakım araç setidir. Sol menüden bir bölüm seç. Tüm yıkıcı
    işlemler uygulama-içi onay penceresiyle korunur.
  </p>
  {#each sections as sec}
    <section class="card">
      <h3>{sec.title}</h3>
      <dl>
        {#each sec.items as [name, desc]}
          <div class="item">
            <dt>{name}</dt>
            <dd>{desc}</dd>
          </div>
        {/each}
      </dl>
    </section>
  {/each}
</div>

<style>
  .help { display: flex; flex-direction: column; gap: 14px; font-family: monospace; overflow: auto; }
  h2 { margin: 0; color: #e6edf3; }
  .intro { color: #9aa7b4; margin: 0; }
  .card { background: #11161c; border: 1px solid #1b2530; border-radius: 8px; padding: 14px; }
  h3 { margin: 0 0 10px; color: #58d6a0; }
  dl { margin: 0; display: flex; flex-direction: column; gap: 8px; }
  .item { display: grid; grid-template-columns: 180px 1fr; gap: 12px; }
  dt { color: #d29922; }
  dd { margin: 0; color: #9aa7b4; }
</style>

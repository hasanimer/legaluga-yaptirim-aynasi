# Legaluga Yaptırım Aynası

ABD Hazine Bakanlığı OFAC'ın **SDN** ve **Non-SDN Consolidated** yaptırım listelerinin
GitHub Releases üzerinde tutulan, bayt bayt aynası.

> **Bu depo resmî bir OFAC kaynağı değildir** ve OFAC ya da ABD Hazine Bakanlığı ile bir
> bağı yoktur. Ayna gecikebilir veya çalışmayabilir. Uyum (compliance) kararları için listeleri
> her zaman OFAC'ın kendi kaynaklarından doğrulayın:
> [Sanctions List Service](https://sanctionslistservice.ofac.treas.gov/) ·
> [Sanctions List Search](https://sanctionssearch.ofac.treas.gov/).

## Ne yapar

GitHub Actions, OFAC Sanctions List Service'teki iki dosyayı her gün 3 saatte bir, hafta içi
OFAC'ın yayın saatlerinde (12:17–21:17 UTC) saatte bir denetler ([neden bu sıklık](#neden-bu-sıklık)):

| Anahtar | Dosya | Resmî kaynak |
|---|---|---|
| `ofac:sdn` | `sdn.xml` | https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml |
| `ofac:non_sdn` | `consolidated.xml` | https://sanctionslistservice.ofac.treas.gov/api/download/consolidated.xml |

Her çalıştırma önce OFAC'ın **resmî dosya özetlerini** (SHA-256 ve boyut; SLS arayüzünün
kullandığı `POST /api/PublicationPreview/SdnList` ve `/ConsolidatedList` yanıtı) alır.

- Özetler son sürümdekiyle aynıysa dosya indirilmez; son sürümün `manifest.json`'u yalnız
  `son_kontrol_at` alanı yenilenerek yeniden yüklenir (**nabız**).
- Farklıysa dosyalar indirilir ve doğrulanır; **boyut ve SHA-256 OFAC'ın resmî özetiyle birebir
  aynı değilse hiçbir şey yayımlanmaz.** Doğrulanan dosyalar `ayna-YYYYMMDDHHMM` (UTC) etiketli
  yeni bir Release olarak yayımlanır ve **latest** olarak işaretlenir.
- Resmî özet uç noktası yanıt vermez ya da biçimi değişirse çalıştırma kırmızıya düşer, hiçbir
  şey yayımlanmaz. En yeni 30 sürüm saklanır, daha eskileri etiketleriyle birlikte silinir.

Her sürümde üç dosya bulunur:

- `sdn.xml` ve `consolidated.xml`: OFAC'ın yayımladığı dosyaların **değiştirilmemiş, bayt bayt
  kopyası**. Sıkıştırma, yeniden biçimlendirme veya ayıklama yapılmaz.
- `manifest.json`: dosyaların özeti, boyutu, OFAC yayın tarihi ve kayıt sayısı.

Neden: Legaluga'nın Cloudflare Workers üzerinde çalışan yaptırım taraması OFAC indirme adresine
doğrudan ulaşamıyor. OFAC adresleri imzalı bir AWS GovCloud S3 adresine yönlendiriyor ve bu
bağlantı Workers'tan HTTP 525 ile başarısız oluyor. GitHub'ın barındırdığı çalıştırıcılar ise
dosyaları indirebiliyor.

## manifest.json

```json
{
  "surum": 1,
  "etiket": "ayna-202610041539",
  "olusturuldu_at": "2026-10-04T15:39:04Z",
  "son_kontrol_at": "2026-10-04T15:39:09Z",
  "dosyalar": {
    "ofac:sdn": {
      "ad": "sdn.xml",
      "kaynak_url": "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml",
      "indirildi_at": "2026-10-04T15:39:00Z",
      "sha256": "0367408834e4af4d262ccc7e329e300b1a5b1430e1e4bcc4debbdd5b927b4cad",
      "boyut": 29240384,
      "publish_date": "10/02/2026",
      "record_count": 19488,
      "resmi_sha256": "0367408834e4af4d262ccc7e329e300b1a5b1430e1e4bcc4debbdd5b927b4cad"
    },
    "ofac:non_sdn": {
      "ad": "consolidated.xml",
      "kaynak_url": "https://sanctionslistservice.ofac.treas.gov/api/download/consolidated.xml",
      "indirildi_at": "2026-10-04T15:39:04Z",
      "sha256": "10203fcdbff6b02985ed835798708fac24f4be6cac5ec35d27236748a300d125",
      "boyut": 1062957,
      "publish_date": "09/14/2026",
      "record_count": 481,
      "resmi_sha256": "10203fcdbff6b02985ed835798708fac24f4be6cac5ec35d27236748a300d125"
    }
  }
}
```

| Alan | Anlamı |
|---|---|
| `surum` | Manifest biçiminin sürümü (şu an `1`). |
| `etiket` | Release etiketi, `^ayna-\d{12}$` (UTC yıl-ay-gün-saat-dakika). |
| `olusturuldu_at` | Sürümün (dosyaların) yayımlandığı an (ISO 8601, UTC). |
| `son_kontrol_at` | Aynanın OFAC'ın resmî özetlerini en son karşılaştırdığı an (nabız); her başarılı çalıştırmada yenilenir. |
| `dosyalar.<anahtar>.ad` | Release'teki dosya adı. |
| `kaynak_url` | Dosyanın indirildiği resmî OFAC adresi. |
| `indirildi_at` | Bu sürüm için indirmenin tamamlandığı an (ISO 8601, UTC). |
| `sha256` / `boyut` | Dosyanın SHA-256 özeti (küçük harf onaltılık) ve bayt cinsinden boyutu. |
| `publish_date` | XML içindeki `sdnList/publshInformation/Publish_Date` (MM/DD/YYYY). |
| `record_count` | XML içindeki `Record_Count`; dosyadaki `sdnEntry` sayısına eşittir. |
| `resmi_sha256` | Dosyanın doğrulandığı, OFAC'ın yayımladığı resmî SHA-256 (her zaman `sha256` ile aynı). |

Anahtarlar (`ofac:sdn`, `ofac:non_sdn`) Legaluga'nın yaptırım modülündeki liste anahtarlarıyla
aynıdır.

> **Tazelik ölçütü `son_kontrol_at`'tir.** `olusturuldu_at` ve `indirildi_at` listenin **son
> değiştiği** çalıştırmanın zamanıdır; OFAC günlerce yayın yapmayabileceği için sağlıklı bir
> aynada da günlerce eski olabilir. `son_kontrol_at` ise her başarılı çalıştırmada (planlı olarak
> en geç 3 saatte bir; GitHub'ın gecikmeleri yüzünden pratikte daha seyrek, bkz.
> [neden bu sıklık](#neden-bu-sıklık)) yenilenir; bu alan da eskiyorsa ayna çalışmıyordur.
> Listenin tarihi `publish_date`'tir.

## Doğrulama

Her çalıştırmada betik şunları denetler; biri tutmazsa sürüm yayımlanmaz ve iş kırmızıya düşer:

- İndirme ve her yönlendirme adımı yalnız `https` ile ve yalnız izin verilen sunuculara gider:
  `sanctionslistservice.ofac.treas.gov` ve AWS GovCloud S3 (`*.s3.us-gov-west-1.amazonaws.com`).
  OFAC başka bir sunucuya yönlendirirse çalıştırma durur (çıkış 2); yeni sunucu doğrulanıp
  `scripts/ayna.py` içindeki izin listesine eklenmelidir. Günlüklere imzalı adresin sorgu dizgesi
  yazılmaz.
- İndirme HTTP 200 ile, sıkıştırılmadan ve `Content-Length` kadar tamamlandı; dosya en çok
  40 MiB (Legaluga'nın okuyucusunun sınırı; daha büyük dosyalı manifest okuyucuda bütünüyle
  reddedilirdi).
- XML iyi biçimli; kök öğe `sdnList` (OFAC XSD ad alanıyla veya ad alanı olmadan).
- `Publish_Date` var, MM/DD/YYYY biçiminde geçerli bir tarih ve bugünden (UTC) en çok 2 gün
  ileride. İleri tarihli bir dosya kabul edilseydi sonraki bütün gerçek dosyalar "geriledi"
  sayılırdı.
- `Record_Count` tamsayı (yalnız ASCII rakamlar) ve `sdnEntry` öğelerinin sayısına eşit.
- Eşikler: `sdn.xml` en az 5000; `consolidated.xml` en az 50, en çok 4999 kayıt. İki dosya
  birbirinin aynı olamaz (SHA-256'ları farklı olmalı). Böylece bir adres yanlış dosyayı verirse
  (ör. consolidated adresi SDN dosyasını) yayın yapılmaz.
- Önceki sürüme göre: `Publish_Date` geriye gidemez; yayın tarihi ilerlemeden kayıt sayısı
  %10'dan fazla düşemez. Bu denetim gerçek bir OFAC düzeltmesini de durdurursa elle atlanabilir
  (aşağıya bakın).

İndirdiğiniz dosyaları kendiniz de doğrulayabilirsiniz:

```sh
DEPO=https://github.com/hasanimer/legaluga-yaptirim-aynasi
curl -fsSL -o manifest.json "$DEPO/releases/latest/download/manifest.json"
ETIKET=$(python3 -c 'import json; print(json.load(open("manifest.json"))["etiket"])')
curl -fsSLO "$DEPO/releases/download/$ETIKET/sdn.xml"

# 1. Özet: manifest.json içindeki "sha256" ile aynı olmalı (Linux'ta: sha256sum)
shasum -a 256 sdn.xml

# 2. Yayın tarihi ve kayıt sayısı: manifest.json içindeki publish_date ve record_count
grep -m1 -o '<Publish_Date>[^<]*' sdn.xml
grep -m1 -o '<Record_Count>[^<]*' sdn.xml
grep -c '<sdnEntry>' sdn.xml

# 3. Resmî kaynakla karşılaştırma: OFAC o arada yeni yayın yapmadıysa özet aynı çıkar
curl -fsSL https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml | shasum -a 256
```

GitHub, Release dosyalarının SHA-256 özetini de gösterir; bu değer manifestteki `sha256` ile
aynı olmalıdır.

## Zamanlama ve hata bildirimi

- İş akışı: [`.github/workflows/ayna.yml`](.github/workflows/ayna.yml), iki cron satırı (UTC);
  gerekçesi aşağıda ([neden bu sıklık](#neden-bu-sıklık)). GitHub zamanlanmış işleri saatlerce
  geciktirebilir ya da hiç başlatmayabilir. Actions sekmesinden **Run workflow** ile elle de
  çalıştırılabilir.
- Aynı anda tek çalıştırma olur (`concurrency`).
- Doğrulama veya indirme hatasında iş başarısız olur ve GitHub depo sahibine e-posta gönderir.
  O çalıştırmada hiçbir şey yayımlanmaz; son iyi sürüm `latest` olarak kalır.
- Yayımlandıktan sonra sürüm denetlenir (taslak değil, 3 dosya, boyut ve SHA-256 aynı, `latest`).
  Denetim geçmezse bu çalıştırmanın oluşturduğu sürüm silinir; önceki sürüm `latest` olarak
  kalır ve sonraki çalıştırma yeniden yayımlar.
- **Gerileme denetimini elle atlamak:** OFAC aynı `Publish_Date` ile kayıtların %10'undan
  fazlasını kaldıran bir düzeltme yayımlarsa ya da önceki sürüm hatalıysa (ör. ileri tarihli)
  her çalıştırma "geriledi" hatası verir. Durumu OFAC'ın kendi sayfasından doğruladıktan sonra
  Actions → **Yaptırım aynası** → **Run workflow** ile `gerileme_denetimini_atla` kutusunu
  işaretleyip bir kez çalıştırın. Öteki bütün denetimler yine uygulanır; yeni sürüm sonraki
  çalıştırmaların karşılaştırma tabanı olur.
- GitHub, herkese açık depolarda 60 gün depo etkinliği olmazsa zamanlanmış iş akışlarını kapatır.
  Bunu önlemek için `canli-tut` işi her çalıştırmada iş akışını GitHub API'siyle yeniden
  etkinleştirir (`PUT /repos/{depo}/actions/workflows/ayna.yml/enable`, `actions: write`
  izniyle); boş commit gerekmez. İş akışı yine de kapanırsa Actions sekmesinden yeniden
  etkinleştirin.
- İzinler iş başına verilir (`ayna`: `contents: write`, `canli-tut`: `actions: write`). Jeton
  yalnız `gh` çağıran adımlara verilir; testler ve OFAC dosyasını işleyen adım jetonu görmez.
  Kullanılan eylemler commit SHA'sına sabitlenmiştir.

### Neden bu sıklık

Sıklık iki ayrı ihtiyaçtan çıkar; `ayna.yml`'deki iki cron satırının her biri yalnız birine
hizmet eder. Saatler UTC'dir.

- **Taban, `17 */3 * * *`: her gün 3 saatte bir.** Legaluga `son_kontrol_at` 48 saatten eskiyse
  aynayı kullanmaz. Taban planlı iki kontrol arasını 3 saatle sınırlar; sınırın aşılması için art
  arda 16 planlı çalıştırmanın kaybolması gerekir. OFAC'ın ne zaman yayımladığına dair bir
  varsayıma dayanmaz: pencere satırı değiştirilse ya da silinse de bu güvence kalır. Akşam ya da
  hafta sonu yapılan seyrek yayınlar da bu satırla yakalanır.
- **OFAC penceresi, `17 13,14,16,17,19,20 * * 1-5`: hafta içi 12:17–21:17 arasında saatte bir.**
  OFAC'ın önceden belirlenmiş bir yayın takvimi yoktur, ancak SDN güncellemeleri pratikte ABD iş
  günlerinde, ABD Doğu saatiyle (ET) 09:00–16:00 arasında, çoğunlukla saat başını birkaç dakika
  geçe (en sık 10:00) yayımlanır. Bu satır tabanın o saatlerdeki boşluklarını doldurur.

  | Hafta içi pencere | UTC | ET | Türkiye |
  |---|---|---|---|
  | ABD yaz saati (Mart–Kasım) | 12:17–21:17 | 08:17–17:17 | 15:17–00:17 |
  | ABD kış saati (Kasım–Mart) | 12:17–21:17 | 07:17–16:17 | 15:17–00:17 |

  İki durumda da 09:00–16:00 ET kapsandığı için ABD'deki saat değişiminde cron'a dokunulmaz.
  Pencere satırının saatleri tabanınkilerle (0, 3, 6 … 21) çakışmamalıdır.
- Dakika 17, GitHub'ın yoğun olduğu saat başından uzak durmak içindir. Planlı çalıştırma sayısı
  hafta içi günde 14, hafta sonu günde 8'dir (haftada 86). Değişiklik yoksa bir çalıştırma
  yaklaşık 15 saniye sürer ve OFAC'a yalnız iki küçük özet isteği gider. ABD resmî tatillerinde de
  pencere çalışır; birkaç boş kontrol için tatil takvimi tutmaya değmez.

**Cron bir istektir, söz değil.** Planlı gecikme pencerede en çok 1 saat, pencere dışında en çok
3 saattir; gerçek gecikmeyi GitHub belirler. Ekim 2026'da 3 saatlik cron'un planlı
çalıştırmalarının yalnız %40 kadarı gerçekleşti; çalıştırmalar arası ortalama 7,8, en çok 10,4
saat oldu. Böyle dönemlerde cron'u sıklaştırmak gerçek çalıştırma sayısını orantılı artırmaz
(pencerenin 15 dakikalık değil saatlik olmasının nedeni budur); tazeliğin güvencesi 48 saat
kuralıdır. Bir yayının belirli bir süre içinde (ör. 1 saatte) aynaya geçmesi gerekirse iş akışı
dışarıdaki bir zamanlayıcıdan `workflow_dispatch` ile tetiklenmeli, cron yedek olarak kalmalıdır.

## Legaluga nasıl kullanır

1. `https://github.com/hasanimer/legaluga-yaptirim-aynasi/releases/latest/download/manifest.json`
   okunur.
2. Her dosya, manifestteki etiketle indirilir:
   `https://github.com/hasanimer/legaluga-yaptirim-aynasi/releases/download/<etiket>/<ad>`.
   Veri dosyaları için `latest` kullanılmaz; böylece manifest okunduktan sonra yeni bir sürüm
   yayımlansa bile manifest ile dosyalar aynı sürümden gelir.
3. İndirilen dosyanın `boyut` ve `sha256` değerleri manifestle, XML'deki `Publish_Date` ve kayıt
   sayısı da manifestle karşılaştırılır; liste tarihi olarak `publish_date` gösterilir.
4. `son_kontrol_at` 48 saatten eskiyse ayna kullanılmaz (Legaluga resmî kaynağı dener).

## Yerelde çalıştırma

Yalnızca Python 3 standart kütüphanesi gerekir.

```sh
python3 -m unittest discover -s tests -v
python3 scripts/ayna.py --cikti cikti --notlar notlar.md
python3 scripts/ayna.py --cikti cikti --onceki onceki/manifest.json
python3 scripts/ayna.py --cikti cikti --onceki onceki/manifest.json --gerileme-denetimini-atla
python3 scripts/ayna.py --degisti-mi --yeni cikti/manifest.json --onceki onceki/manifest.json
```

Çıkış kodları: `0` başarılı, `2` doğrulama hatası, `3` indirme hatası, `64` kullanım hatası.
`--degisti-mi` kipi indirme yapmaz; iki manifestteki `sha256` değerlerini karşılaştırıp
`degisti` veya `ayni` yazar.

## İşletim notları

- Liste değişmediği sürece yeni sürüm yayımlanmaz; uzun süre yeni sürüm olmaması tek başına
  aynanın bozulduğu anlamına gelmez. Son çalıştırmaların durumu Actions sekmesinde görünür.
- `latest` sürüm elle açılmış bir sürümse ya da içinde `manifest.json` yoksa iş akışı uyarı verir,
  ilk yayın gibi davranır ve yeni bir ayna sürümünü `latest` yapar.
- Eski sürümler silinirken etiketleri ayrıca silinir. Depoda değişmez sürümler (immutable
  releases) açılırsa etiket silinemez; bu durumda yalnız uyarı yazılır, iş kırmızıya düşmez.

## Lisans

- **Veri:** `sdn.xml` ve `consolidated.xml` ABD federal hükümetinin eseridir ve
  [17 U.S.C. § 105](https://www.law.cornell.edu/uscode/text/17/105) uyarınca ABD'de telif
  korumasına tabi değildir (kamu malı).
- **Betikler ve belgeler:** [MIT](LICENSE) © 2026 Hasan İmer Akın. MIT lisansı OFAC verisini
  kapsamaz.

---

## English summary

A byte-for-byte mirror of the U.S. Treasury OFAC **SDN** (`sdn.xml`) and **Non-SDN Consolidated**
(`consolidated.xml`) sanctions lists, published as GitHub Releases.

- Every 3 hours every day (cron `17 */3 * * *`), plus hourly on weekdays from 12:17 to 21:17 UTC
  (cron `17 13,14,16,17,19,20 * * 1-5`), a GitHub Actions workflow downloads both files from the
  official OFAC Sanctions List Service, validates them (https-only download and redirects to an
  allow-listed host, at most 40 MiB, well-formed XML, root `sdnList`, a valid MM/DD/YYYY
  `Publish_Date` no more than 2 days in the future, `Record_Count` equal to the number of
  `sdnEntry` elements, record bounds of at least 5000 for SDN and 50 to 4999 for consolidated,
  the two files must differ, no regression against the previous release) and, only if a file
  changed, publishes a release tagged `ayna-YYYYMMDDHHMM` (UTC) marked as latest. A release that
  fails the post-publish check is deleted. The newest 30 releases are kept.
- The regression check can be skipped once from **Run workflow** (`gerileme_denetimini_atla`)
  after a genuine OFAC correction has been verified by hand.
- A separate `canli-tut` job re-enables the workflow through the GitHub API on every run, so
  GitHub's 60-day inactivity rule does not switch off the schedule.
- Each run first fetches OFAC's official file digests (SHA-256 and size, from the
  `POST /api/PublicationPreview/SdnList` / `ConsolidatedList` endpoint the SLS UI uses). If they
  match the latest release, nothing is downloaded and only `manifest.json` is re-uploaded with a
  new `son_kontrol_at` (heartbeat). Otherwise the files are downloaded and **published only if
  their size and SHA-256 equal OFAC's official values**.
- Release assets: the unmodified official files plus `manifest.json` with the SHA-256 (and the
  official SHA-256 it was verified against), size, `Publish_Date` and `Record_Count` of each file.
- Legaluga reads `releases/latest/download/manifest.json`, then fetches each file by the tag in
  the manifest (never via `latest`) and checks size and SHA-256.
- Schedule rationale: the 3-hourly floor alone keeps `son_kontrol_at` within the 48-hour rule and
  assumes nothing about OFAC; the weekday line adds hourly checks during OFAC's usual publication
  hours (09:00–16:00 US Eastern in both EDT and EST). Scheduled runs are best-effort and GitHub
  often starts them hours late; if a bounded detection time is needed, trigger the workflow via
  `workflow_dispatch` from an external clock and keep the cron as a fallback.
- Freshness: `son_kontrol_at` is refreshed on every successful run (scheduled at most 3 hours
  apart, in practice less often); Legaluga stops using the mirror when it is older than 48 hours. `olusturuldu_at` / `indirildi_at` record the
  run in which a list last changed.
- **This is not an official OFAC source** and is not affiliated with OFAC or the U.S. Department
  of the Treasury. It may be delayed or unavailable. Verify with OFAC for any compliance decision.
- Data: U.S. Government work, public domain in the U.S. under 17 U.S.C. § 105. Scripts and docs:
  MIT licensed (see [LICENSE](LICENSE)); the license does not cover the OFAC data.

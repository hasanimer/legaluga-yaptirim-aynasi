#!/usr/bin/env python3
"""Legaluga yaptırım aynası.

OFAC SDN ve Non-SDN Consolidated listelerini resmî OFAC Sanctions List Service
adresinden indirir, bayt bayt saklar, doğrular ve GitHub Release'e konacak
dosyaları (sdn.xml, consolidated.xml, manifest.json) hazırlar.

Yalnızca Python 3 standart kütüphanesi kullanır.

Çıkış kodları:
  0  başarılı
  2  doğrulama hatası (XML, sayım, tarih, önceki sürüme göre gerileme, manifest,
     izin verilmeyen yönlendirme hedefi)
  3  indirme hatası (ağ, HTTP, zaman aşımı; yeniden denemeler tükendi)
  64 komut satırı kullanım hatası

Kullanım:
  python3 scripts/ayna.py --cikti cikti [--onceki onceki/manifest.json] [--notlar notlar.md]
                          [--gerileme-denetimini-atla]
  python3 scripts/ayna.py --degisti-mi --yeni cikti/manifest.json [--onceki onceki/manifest.json]
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

USER_AGENT = (
    "Legaluga-Yaptirim-Aynasi/1.0 (+https://github.com/hasanimer/legaluga-yaptirim-aynasi)"
)
ZAMAN_ASIMI_S = 120  # tek bir soket işlemi (bağlanma / okuma) için
DENEME_TOPLAM_S = 600  # tek bir indirme denemesinin toplam süre sınırı
YENIDEN_DENEME = 3  # ilk denemeden sonra en çok 3 yeniden deneme (toplam 4)
BEKLEMELER_S = (5, 15, 45)  # yeniden denemeler arası bekleme
# 40 MiB sert sınır. Legaluga'nın okuyucusu (karar-ara src/lib/mcp/yaptirim-ayna.ts,
# AYNA_DOSYA_MAX_BYTES) daha büyük dosyalı manifesti bütünüyle reddeder; ayna da bu sınırı aşan
# dosyayı yayımlamaz.
AZAMI_BOYUT = 40 * 1024 * 1024
PARCA_BOYUTU = 1024 * 1024
DUSUS_ESIGI_YUZDE = 10  # aynı Publish_Date ile kayıt sayısı en çok %10 düşebilir
GELECEK_PAYI_GUN = 2  # Publish_Date bugünden (UTC) en çok 2 gün ileride olabilir
MANIFEST_SURUM = 1
MANIFEST_AZAMI_BAYT = 16 * 1024
# OFAC'ın resmî dosya özetleri (SHA-256/384/512). SLS arayüzünün kullandığı, belgelenmemiş bir
# uç nokta: boş gövdeli POST, her dosya için fileName, size ve hashCodes (JSON metni) döner.
# Ayna her dosyayı bu özete ve boyuta karşı doğrular; uç nokta yanıt vermez ya da biçimi
# değişirse çalıştırma durur (hiçbir şey yayımlanmaz).
RESMI_OZET_KOKU = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/"
RESMI_OZET_AZAMI_BAYT = 1024 * 1024

# İndirme ve her yönlendirme adımı yalnız https ve yalnız bu sunuculara gidebilir. OFAC bugün
# imzalı bir AWS GovCloud (us-gov-west-1) S3 adresine yönlendiriyor. Başka bir sunucuya
# yönlendirirse çalıştırma doğrulama hatasıyla (çıkış 2) durur; yeni sunucu doğrulanıp buraya
# eklenmelidir.
IZINLI_SUNUCULAR = frozenset(
    {"sanctionslistservice.ofac.treas.gov", "s3.us-gov-west-1.amazonaws.com"}
)
IZINLI_SUNUCU_SONEKLERI = (".s3.us-gov-west-1.amazonaws.com",)

# Yalnız ASCII rakamlar: str.isdigit() ve \d "²" ya da "١" gibi karakterleri de kabul eder.
ETIKET_RE = re.compile(r"ayna-[0-9]{12}")
TARIH_RE = re.compile(r"[0-9]{2}/[0-9]{2}/[0-9]{4}")
ISO_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
RAKAM_RE = re.compile(r"[0-9]+")

CIKIS_TAMAM = 0
CIKIS_DOGRULAMA = 2
CIKIS_INDIRME = 3
CIKIS_KULLANIM = 64

# Anahtarlar karar-ara src/lib/mcp/yaptirim.ts içindeki FEEDS anahtarlarıyla aynıdır.
LISTELER = (
    {
        "anahtar": "ofac:sdn",
        "ad": "sdn.xml",
        "kaynak_url": "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml",
        "resmi_liste": "SdnList",
        "resmi_dosya": "SDN.XML",
        "asgari_kayit": 5000,
        "azami_kayit": None,
    },
    {
        "anahtar": "ofac:non_sdn",
        "ad": "consolidated.xml",
        "kaynak_url": "https://sanctionslistservice.ofac.treas.gov/api/download/consolidated.xml",
        "resmi_liste": "ConsolidatedList",
        "resmi_dosya": "CONSOLIDATED.XML",
        "asgari_kayit": 50,
        # SDN tabanının (5000) altında: yanlış yönlendirilen bir SDN dosyası consolidated.xml
        # olarak yayımlanamaz. Bugün 481 kayıt var.
        "azami_kayit": 4999,
    },
)

GIRDI_ANAHTARLARI = (
    "ad",
    "kaynak_url",
    "indirildi_at",
    "sha256",
    "boyut",
    "publish_date",
    "record_count",
    "resmi_sha256",
)
# son_kontrol_at: aynanın OFAC'ın resmî özetlerini en son karşılaştırdığı an (nabız). Dosyalar
# değişmese de her çalıştırmada yenilenir; Legaluga aynanın tazeliğini bununla ölçer.
MANIFEST_ANAHTARLARI = ("surum", "etiket", "olusturuldu_at", "son_kontrol_at", "dosyalar")


class AynaHatasi(Exception):
    cikis_kodu = 1


class DogrulamaHatasi(AynaHatasi):
    cikis_kodu = CIKIS_DOGRULAMA


class IndirmeHatasi(AynaHatasi):
    cikis_kodu = CIKIS_INDIRME


class _GeciciHata(Exception):
    """Yeniden denenebilecek indirme hatası."""


# --------------------------------------------------------------------------- yardımcılar


def _iso(an: datetime) -> str:
    return an.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _etiket(an: datetime) -> str:
    return "ayna-" + an.astimezone(timezone.utc).strftime("%Y%m%d%H%M")


def _yerel_ad(etiket: str) -> str:
    """'{ad-alanı}sdnList' -> 'sdnList'."""
    return etiket.rsplit("}", 1)[-1] if isinstance(etiket, str) else ""


def tarih_coz(metin: str) -> datetime:
    """MM/DD/YYYY biçimindeki Publish_Date değerini çözer; uymazsa DogrulamaHatasi."""
    if not isinstance(metin, str) or not TARIH_RE.fullmatch(metin):
        raise DogrulamaHatasi(f"Publish_Date MM/DD/YYYY biçiminde değil: {metin!r}")
    try:
        return datetime.strptime(metin, "%m/%d/%Y")
    except ValueError as hata:
        raise DogrulamaHatasi(f"Publish_Date geçerli bir tarih değil: {metin!r}") from hata


def _tamsayi_mi(deger: object) -> bool:
    return isinstance(deger, int) and not isinstance(deger, bool)


def _sil(yol: str) -> None:
    try:
        os.remove(yol)
    except FileNotFoundError:
        pass


def _atomik_yaz(yol: str, veri: bytes) -> None:
    gecici = yol + ".tmp"
    with open(gecici, "wb") as dosya:
        dosya.write(veri)
    os.replace(gecici, yol)


# --------------------------------------------------------------------------- indirme


def resmi_adres_mi(url: str) -> bool:
    """Adres https mi ve sunucusu izin listesinde mi (varsayılan 443 portu, kullanıcı bilgisi yok)?"""
    try:
        parca = urllib.parse.urlsplit(url)
        port = parca.port
    except ValueError:
        return False
    if parca.scheme != "https" or parca.username is not None or parca.password is not None:
        return False
    if port not in (None, 443):
        return False
    sunucu = (parca.hostname or "").lower()
    return sunucu in IZINLI_SUNUCULAR or sunucu.endswith(IZINLI_SUNUCU_SONEKLERI)


def _adres_ozeti(url: str) -> str:
    """Günlüğe yazılacak adres: sorgu dizgesi (imzalı S3 adresindeki imza) atılır."""
    try:
        parca = urllib.parse.urlsplit(url)
        return urllib.parse.urlunsplit((parca.scheme, parca.netloc, parca.path, "", ""))
    except ValueError:
        return "<çözülemeyen adres>"


class _DenetimliYonlendirme(urllib.request.HTTPRedirectHandler):
    """Her yönlendirme adımını izlemeden önce adres denetiminden geçirir."""

    def __init__(self, adres_denetimi):
        super().__init__()
        self._adres_denetimi = adres_denetimi

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self._adres_denetimi(newurl):
            fp.close()
            raise DogrulamaHatasi(
                f"izin verilmeyen yönlendirme hedefi: {_adres_ozeti(newurl)} "
                "(yalnız https ve izin listesindeki sunucular)"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _indir_bir_kez(
    url: str, hedef: str, azami: int, zaman_asimi: float, adres_denetimi
) -> tuple[str, int]:
    if not adres_denetimi(url):
        raise DogrulamaHatasi(f"izin verilmeyen indirme adresi: {_adres_ozeti(url)}")
    acici = urllib.request.build_opener(_DenetimliYonlendirme(adres_denetimi))
    istek = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/xml, text/xml;q=0.9, */*;q=0.1",
            # Resmî dosyanın baytlarını değiştirmeden almak için sıkıştırma istenmez.
            "Accept-Encoding": "identity",
        },
    )
    baslangic = time.monotonic()
    ozet = hashlib.sha256()
    boyut = 0
    # Açıcı 3xx yönlendirmelerini (OFAC -> imzalı S3 adresi) izler; her adım denetlenir.
    with acici.open(istek, timeout=zaman_asimi) as yanit:
        if not adres_denetimi(yanit.geturl()):
            raise DogrulamaHatasi(f"izin verilmeyen son adres: {_adres_ozeti(yanit.geturl())}")
        durum = yanit.getcode()
        if durum != 200:
            raise IndirmeHatasi(f"beklenmeyen HTTP durumu {durum}")
        kodlama = (yanit.headers.get("Content-Encoding") or "identity").strip().lower()
        if kodlama not in ("", "identity"):
            raise DogrulamaHatasi(
                f"sunucu sıkıştırılmış gövde gönderdi (Content-Encoding: {kodlama}); "
                "bayt bayt ayna garanti edilemez"
            )
        uzunluk_basligi = (yanit.headers.get("Content-Length") or "").strip()
        # Geçersiz Content-Length (ör. "²") bilinmeyen uzunluk sayılır; sınır okurken uygulanır.
        beklenen = int(uzunluk_basligi) if RAKAM_RE.fullmatch(uzunluk_basligi) else None
        if beklenen is not None and beklenen > azami:
            raise DogrulamaHatasi(f"dosya boyut sınırını aşıyor: {beklenen} > {azami} bayt")
        with open(hedef, "wb") as dosya:
            while True:
                parca = yanit.read(PARCA_BOYUTU)
                if not parca:
                    break
                boyut += len(parca)
                if boyut > azami:
                    raise DogrulamaHatasi(f"dosya boyut sınırını aşıyor: >{azami} bayt")
                if time.monotonic() - baslangic > DENEME_TOPLAM_S:
                    raise _GeciciHata(f"deneme {DENEME_TOPLAM_S} saniyede tamamlanmadı")
                ozet.update(parca)
                dosya.write(parca)
    if beklenen is not None and boyut != beklenen:
        raise _GeciciHata(f"eksik gövde: {boyut}/{beklenen} bayt")
    if boyut == 0:
        raise _GeciciHata("boş gövde")
    return ozet.hexdigest(), boyut


def indir(
    url: str,
    hedef: str,
    *,
    azami: int = AZAMI_BOYUT,
    yeniden_deneme: int = YENIDEN_DENEME,
    beklemeler: tuple = BEKLEMELER_S,
    zaman_asimi: float = ZAMAN_ASIMI_S,
    uyku=time.sleep,
    adres_denetimi=resmi_adres_mi,
) -> tuple[str, int]:
    """URL'yi hedef dosyaya indirir; (sha256, boyut) döner.

    Ağ hataları, zaman aşımı, 408/425/429 ve 5xx yanıtları yeniden denenir. Diğer 4xx
    yanıtları kalıcı indirme hatasıdır. Boyut sınırı, sıkıştırılmış gövde ve izin verilmeyen
    adres (https olmayan ya da izin listesi dışındaki sunucu) doğrulama hatasıdır ve yeniden
    denenmez.
    """
    son_hata: object = None
    for deneme in range(yeniden_deneme + 1):
        if deneme:
            bekle = beklemeler[min(deneme - 1, len(beklemeler) - 1)] if beklemeler else 0
            print(
                f"ayna: {url} yeniden deneniyor ({deneme}/{yeniden_deneme}, {bekle} sn sonra): "
                f"{son_hata}",
                file=sys.stderr,
            )
            uyku(bekle)
        try:
            return _indir_bir_kez(url, hedef, azami, zaman_asimi, adres_denetimi)
        except AynaHatasi:
            _sil(hedef)
            raise
        except urllib.error.HTTPError as hata:
            _sil(hedef)
            hata.close()
            if hata.code in (408, 425, 429) or hata.code >= 500:
                son_hata = f"HTTP {hata.code}"
                continue
            raise IndirmeHatasi(f"{url}: HTTP {hata.code} (kalıcı hata)") from hata
        except (_GeciciHata, urllib.error.URLError, http.client.HTTPException, OSError) as hata:
            _sil(hedef)
            son_hata = f"{type(hata).__name__}: {hata}"
            continue
    raise IndirmeHatasi(f"{url}: {yeniden_deneme + 1} denemede indirilemedi ({son_hata})")


def _resmi_ozet_bir_kez(url: str, dosya_adi: str, zaman_asimi: float, adres_denetimi) -> dict:
    if not adres_denetimi(url):
        raise DogrulamaHatasi(f"izin verilmeyen özet adresi: {_adres_ozeti(url)}")
    acici = urllib.request.build_opener(_DenetimliYonlendirme(adres_denetimi))
    istek = urllib.request.Request(
        url,
        data=b"",
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    with acici.open(istek, timeout=zaman_asimi) as yanit:
        if yanit.getcode() != 200:
            raise IndirmeHatasi(f"resmî özet: beklenmeyen HTTP durumu {yanit.getcode()}")
        veri = yanit.read(RESMI_OZET_AZAMI_BAYT + 1)
    if len(veri) > RESMI_OZET_AZAMI_BAYT:
        raise DogrulamaHatasi("resmî özet yanıtı boyut sınırını aşıyor")

    def bicim(mesaj: str) -> DogrulamaHatasi:
        return DogrulamaHatasi(f"OFAC resmî özet yanıtı beklenen biçimde değil: {mesaj}")

    try:
        kayitlar = json.loads(veri.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as hata:
        raise bicim("JSON değil") from hata
    if not isinstance(kayitlar, list):
        raise bicim("liste değil")
    eslesen = [
        k for k in kayitlar if isinstance(k, dict) and str(k.get("fileName", "")).upper() == dosya_adi
    ]
    if len(eslesen) != 1:
        raise bicim(f"{dosya_adi} kaydı {len(eslesen)} kez bulundu")
    kayit = eslesen[0]
    ozetler = kayit.get("hashCodes")
    if isinstance(ozetler, str):
        try:
            ozetler = json.loads(ozetler)
        except json.JSONDecodeError as hata:
            raise bicim(f"{dosya_adi} hashCodes çözülemedi") from hata
    sha256 = ozetler.get("SHA-256") if isinstance(ozetler, dict) else None
    if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256.lower()):
        raise bicim(f"{dosya_adi} SHA-256 yok ya da geçersiz")
    boyut = kayit.get("size")
    if not _tamsayi_mi(boyut) or boyut <= 0:
        raise bicim(f"{dosya_adi} size geçersiz")
    return {"sha256": sha256.lower(), "boyut": boyut}


def resmi_ozet_al(
    liste: dict,
    *,
    yeniden_deneme: int = YENIDEN_DENEME,
    beklemeler: tuple = BEKLEMELER_S,
    zaman_asimi: float = 60,
    uyku=time.sleep,
    adres_denetimi=resmi_adres_mi,
) -> dict:
    """OFAC'ın listedeki dosya için yayımladığı resmî SHA-256 ve boyutu döner."""
    # "resmi_ozet_url" yalnızca testler içindir.
    url = liste.get("resmi_ozet_url", RESMI_OZET_KOKU + liste["resmi_liste"])
    son_hata: object = None
    for deneme in range(yeniden_deneme + 1):
        if deneme:
            uyku(beklemeler[min(deneme - 1, len(beklemeler) - 1)] if beklemeler else 0)
        try:
            return _resmi_ozet_bir_kez(url, liste["resmi_dosya"], zaman_asimi, adres_denetimi)
        except AynaHatasi:
            raise
        except urllib.error.HTTPError as hata:
            hata.close()
            if hata.code in (408, 425, 429) or hata.code >= 500:
                son_hata = f"HTTP {hata.code}"
                continue
            raise IndirmeHatasi(f"resmî özet ({liste['resmi_liste']}): HTTP {hata.code}") from hata
        except (urllib.error.URLError, http.client.HTTPException, OSError) as hata:
            son_hata = f"{type(hata).__name__}: {hata}"
            continue
    raise IndirmeHatasi(
        f"resmî özet ({liste['resmi_liste']}) {yeniden_deneme + 1} denemede alınamadı ({son_hata})"
    )


# --------------------------------------------------------------------------- doğrulama


def dogrula_xml(
    yol: str,
    asgari_kayit: int,
    azami_kayit: int | None = None,
    simdi: datetime | None = None,
) -> dict:
    """OFAC XML dosyasını akışla okuyup doğrular.

    Denetimler: iyi biçimli XML; kök öğenin yerel adı sdnList; tek bir publshInformation;
    Publish_Date MM/DD/YYYY ve (simdi verildiyse) bugünden (UTC) en çok GELECEK_PAYI_GUN gün
    ileride; Record_Count tamsayı ve kökün altındaki sdnEntry sayısına eşit; kayıt sayısı asgari
    ile (verildiyse) azami eşik arasında. Ad alanlı ve ad alansız dosyalar aynı işlenir.
    """
    derinlik = 0
    kok = None
    yayin_bilgisi = 0
    publish_date = None
    record_count_metni = None
    giris_sayisi = 0
    try:
        kaynak = open(yol, "rb")
    except OSError as hata:
        raise DogrulamaHatasi(f"dosya açılamadı: {hata}") from hata
    try:
        for olay, oge in ET.iterparse(kaynak, events=("start", "end")):
            if olay == "start":
                derinlik += 1
                if derinlik == 1:
                    kok = oge
                    if _yerel_ad(oge.tag) != "sdnList":
                        raise DogrulamaHatasi(
                            f"kök öğe sdnList değil: {_yerel_ad(oge.tag)!r}"
                        )
                continue
            if derinlik == 2:
                ad = _yerel_ad(oge.tag)
                if ad == "sdnEntry":
                    giris_sayisi += 1
                elif ad == "publshInformation":
                    yayin_bilgisi += 1
                    for cocuk in oge:
                        cocuk_adi = _yerel_ad(cocuk.tag)
                        if cocuk_adi == "Publish_Date":
                            publish_date = (cocuk.text or "").strip()
                        elif cocuk_adi == "Record_Count":
                            record_count_metni = (cocuk.text or "").strip()
                # İşlenen alt ağacı bırak; bellek kullanımı kayıt sayısından bağımsız kalır.
                kok.clear()
            derinlik -= 1
    except ET.ParseError as hata:
        raise DogrulamaHatasi(f"XML iyi biçimli değil: {hata}") from hata
    finally:
        kaynak.close()

    if kok is None:
        raise DogrulamaHatasi("XML kök öğesi yok")
    if yayin_bilgisi != 1:
        raise DogrulamaHatasi(f"publshInformation öğesi {yayin_bilgisi} kez bulundu (1 beklenir)")
    if not publish_date:
        raise DogrulamaHatasi("Publish_Date yok veya boş")
    yayin_tarihi = tarih_coz(publish_date)
    if simdi is not None:
        en_gec = simdi.astimezone(timezone.utc).date() + timedelta(days=GELECEK_PAYI_GUN)
        if yayin_tarihi.date() > en_gec:
            # İleri tarihli bir dosya kabul edilirse sonraki bütün gerçek dosyalar "geriledi"
            # sayılıp reddedilir; ayna kilitlenir.
            raise DogrulamaHatasi(
                f"Publish_Date ileri tarihli: {publish_date} (en geç {en_gec:%m/%d/%Y} kabul edilir)"
            )
    if not record_count_metni or not RAKAM_RE.fullmatch(record_count_metni):
        raise DogrulamaHatasi(f"Record_Count tamsayı değil: {record_count_metni!r}")
    record_count = int(record_count_metni)
    if record_count != giris_sayisi:
        raise DogrulamaHatasi(
            f"Record_Count ({record_count}) sdnEntry sayısına ({giris_sayisi}) eşit değil"
        )
    if giris_sayisi < asgari_kayit:
        raise DogrulamaHatasi(
            f"kayıt sayısı ({giris_sayisi}) asgari eşiğin ({asgari_kayit}) altında"
        )
    if azami_kayit is not None and giris_sayisi > azami_kayit:
        raise DogrulamaHatasi(
            f"kayıt sayısı ({giris_sayisi}) azami eşiğin ({azami_kayit}) üstünde; "
            "dosya yanlış listeye ait olabilir"
        )
    return {"publish_date": publish_date, "record_count": record_count}


def farkli_dosyalar_denetle(dosyalar: dict) -> None:
    """İki liste aynı baytları taşıyorsa (biri yanlış adrese yönlendirildiyse) DogrulamaHatasi."""
    gorulen: dict[str, str] = {}
    for anahtar, girdi in dosyalar.items():
        onceki_anahtar = gorulen.setdefault(girdi["sha256"], anahtar)
        if onceki_anahtar != anahtar:
            raise DogrulamaHatasi(
                f"{onceki_anahtar} ve {anahtar} aynı dosya (sha256 {girdi['sha256']}); "
                "adreslerden biri yanlış dosyayı veriyor"
            )


def onceki_ile_karsilastir(dosyalar: dict, onceki: dict) -> None:
    """Önceki manifeste göre gerilemeyi reddeder.

    Bir listenin Publish_Date değeri öncekinden eskiyse ya da Publish_Date ilerlemeden
    kayıt sayısı %10'dan fazla düştüyse DogrulamaHatasi.
    """
    eski_dosyalar = onceki.get("dosyalar") or {}
    sorunlar = []
    for anahtar, yeni in dosyalar.items():
        eski = eski_dosyalar.get(anahtar)
        if not isinstance(eski, dict):
            continue
        yeni_tarih = tarih_coz(yeni["publish_date"])
        eski_tarih = tarih_coz(eski["publish_date"])
        if yeni_tarih < eski_tarih:
            sorunlar.append(
                f"{anahtar}: Publish_Date geriledi ({eski['publish_date']} -> {yeni['publish_date']})"
            )
        elif yeni_tarih == eski_tarih:
            eski_sayi = eski["record_count"]
            yeni_sayi = yeni["record_count"]
            if (eski_sayi - yeni_sayi) * 100 > eski_sayi * DUSUS_ESIGI_YUZDE:
                sorunlar.append(
                    f"{anahtar}: Publish_Date aynıyken ({yeni['publish_date']}) kayıt sayısı "
                    f"%{DUSUS_ESIGI_YUZDE}'dan fazla düştü ({eski_sayi} -> {yeni_sayi})"
                )
    if sorunlar:
        raise DogrulamaHatasi("; ".join(sorunlar))


# --------------------------------------------------------------------------- manifest


def manifest_olustur(dosyalar: dict, an: datetime) -> dict:
    return {
        "surum": MANIFEST_SURUM,
        "etiket": _etiket(an),
        "olusturuldu_at": _iso(an),
        "son_kontrol_at": _iso(an),
        "dosyalar": dosyalar,
    }


def manifest_dogrula(manifest: object, beklenen_anahtarlar=None) -> None:
    """Manifestin sözleşmeye uyduğunu denetler; uymazsa DogrulamaHatasi."""

    def hata(mesaj: str) -> DogrulamaHatasi:
        return DogrulamaHatasi(f"manifest sözleşmeye uymuyor: {mesaj}")

    if not isinstance(manifest, dict):
        raise hata("nesne değil")
    if sorted(manifest) != sorted(MANIFEST_ANAHTARLARI):
        raise hata(f"üst düzey anahtarlar {sorted(manifest)}")
    if not _tamsayi_mi(manifest["surum"]) or manifest["surum"] != MANIFEST_SURUM:
        raise hata(f"surum {manifest['surum']!r}")
    if not isinstance(manifest["etiket"], str) or not ETIKET_RE.fullmatch(manifest["etiket"]):
        raise hata(f"etiket {manifest['etiket']!r}")
    if not isinstance(manifest["olusturuldu_at"], str) or not ISO_RE.fullmatch(
        manifest["olusturuldu_at"]
    ):
        raise hata(f"olusturuldu_at {manifest['olusturuldu_at']!r}")
    if not isinstance(manifest["son_kontrol_at"], str) or not ISO_RE.fullmatch(
        manifest["son_kontrol_at"]
    ):
        raise hata(f"son_kontrol_at {manifest['son_kontrol_at']!r}")
    if manifest["son_kontrol_at"] < manifest["olusturuldu_at"]:
        raise hata("son_kontrol_at olusturuldu_at'tan önce")
    dosyalar = manifest["dosyalar"]
    if not isinstance(dosyalar, dict) or not dosyalar:
        raise hata("dosyalar boş veya nesne değil")
    if beklenen_anahtarlar is not None and sorted(dosyalar) != sorted(beklenen_anahtarlar):
        raise hata(f"dosyalar anahtarları {sorted(dosyalar)}")
    adlar = set()
    for anahtar, girdi in dosyalar.items():
        if not isinstance(girdi, dict) or sorted(girdi) != sorted(GIRDI_ANAHTARLARI):
            raise hata(f"{anahtar} girdisinin alanları hatalı")
        ad = girdi["ad"]
        if not isinstance(ad, str) or not ad or "/" in ad or "\\" in ad or ad.startswith("."):
            raise hata(f"{anahtar}.ad {ad!r}")
        if ad in adlar:
            raise hata(f"{anahtar}.ad tekrar ediyor")
        adlar.add(ad)
        if not isinstance(girdi["kaynak_url"], str) or not girdi["kaynak_url"].startswith(
            "https://"
        ):
            raise hata(f"{anahtar}.kaynak_url")
        if not isinstance(girdi["indirildi_at"], str) or not ISO_RE.fullmatch(girdi["indirildi_at"]):
            raise hata(f"{anahtar}.indirildi_at {girdi['indirildi_at']!r}")
        if not isinstance(girdi["sha256"], str) or not SHA256_RE.fullmatch(girdi["sha256"]):
            raise hata(f"{anahtar}.sha256")
        if not _tamsayi_mi(girdi["boyut"]) or girdi["boyut"] <= 0:
            raise hata(f"{anahtar}.boyut {girdi['boyut']!r}")
        try:
            tarih_coz(girdi["publish_date"])
        except DogrulamaHatasi as alt:
            raise hata(f"{anahtar}.publish_date: {alt}") from alt
        if not _tamsayi_mi(girdi["record_count"]) or girdi["record_count"] < 0:
            raise hata(f"{anahtar}.record_count {girdi['record_count']!r}")
        if girdi["resmi_sha256"] != girdi["sha256"]:
            raise hata(f"{anahtar}.resmi_sha256 sha256 ile aynı değil")


def manifest_baytlari(manifest: dict) -> bytes:
    veri = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(veri) >= MANIFEST_AZAMI_BAYT:
        raise DogrulamaHatasi(f"manifest {len(veri)} bayt; sınır {MANIFEST_AZAMI_BAYT}")
    return veri


def manifest_oku(yol: str) -> dict:
    try:
        with open(yol, "rb") as dosya:
            veri = dosya.read(MANIFEST_AZAMI_BAYT + 1)
    except OSError as hata:
        raise DogrulamaHatasi(f"manifest okunamadı ({yol}): {hata}") from hata
    if len(veri) > MANIFEST_AZAMI_BAYT:
        raise DogrulamaHatasi(f"manifest {MANIFEST_AZAMI_BAYT} bayttan büyük: {yol}")
    try:
        manifest = json.loads(veri.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as hata:
        raise DogrulamaHatasi(f"manifest geçerli UTF-8 JSON değil ({yol}): {hata}") from hata
    manifest_dogrula(manifest)
    return manifest


def degisti_mi(yeni: dict, onceki) -> bool:
    """Yeni manifestteki dosyalardan biri öncekinden farklıysa (veya önceki yoksa) True."""
    if not onceki:
        return True
    eski_dosyalar = onceki.get("dosyalar") or {}
    yeni_dosyalar = yeni.get("dosyalar") or {}
    if sorted(eski_dosyalar) != sorted(yeni_dosyalar):
        return True
    for anahtar, girdi in yeni_dosyalar.items():
        eski = eski_dosyalar.get(anahtar)
        if not isinstance(eski, dict):
            return True
        if eski.get("sha256") != girdi.get("sha256") or eski.get("ad") != girdi.get("ad"):
            return True
    return False


def notlar_olustur(manifest: dict) -> str:
    satirlar = [
        f"OFAC yaptırım listelerinin bayt bayt aynası — `{manifest['etiket']}` "
        f"({manifest['olusturuldu_at']}).",
        "",
        "| Liste | Dosya | Publish_Date | Record_Count | Boyut (bayt) | SHA-256 |",
        "|---|---|---|---|---|---|",
    ]
    for anahtar, girdi in manifest["dosyalar"].items():
        satirlar.append(
            f"| `{anahtar}` | `{girdi['ad']}` | {girdi['publish_date']} | {girdi['record_count']} "
            f"| {girdi['boyut']} | `{girdi['sha256']}` |"
        )
    satirlar.append("")
    for anahtar, girdi in manifest["dosyalar"].items():
        satirlar.append(
            f"- `{girdi['ad']}` kaynağı: {girdi['kaynak_url']} (indirildi {girdi['indirildi_at']})"
        )
    satirlar += [
        "",
        "Dosyalar OFAC'ın yayımladığı dosyaların değiştirilmemiş kopyasıdır; doğrulama için "
        "`manifest.json` içindeki `sha256`, `publish_date` ve `record_count` değerlerini kullanın.",
        "Bu depo resmî bir OFAC kaynağı değildir. Uyum kararları için OFAC Sanctions List "
        "Service'i esas alın.",
        "",
        "Byte-for-byte mirror of the official OFAC files. Not an official OFAC source; verify "
        "with OFAC for compliance decisions.",
        "",
    ]
    return "\n".join(satirlar)


def ozet_satiri(manifest: dict, degisti: bool) -> str:
    parcalar = [f"ayna: {manifest['etiket']} {'degisti' if degisti else 'ayni'}"]
    for anahtar, girdi in manifest["dosyalar"].items():
        parcalar.append(
            f"{anahtar} {girdi['ad']} publish_date={girdi['publish_date']} "
            f"record_count={girdi['record_count']} boyut={girdi['boyut']} sha256={girdi['sha256']}"
        )
    return " | ".join(parcalar)


def _github_ciktisi_yaz(degerler: dict) -> None:
    yol = os.environ.get("GITHUB_OUTPUT")
    if not yol:
        return
    with open(yol, "a", encoding="utf-8") as dosya:
        for anahtar, deger in degerler.items():
            dosya.write(f"{anahtar}={deger}\n")


# --------------------------------------------------------------------------- komut satırı


class _Ayristirici(argparse.ArgumentParser):
    def error(self, message):  # argparse'ın 2 kodu doğrulama hatasıyla karışmasın
        self.print_usage(sys.stderr)
        self.exit(CIKIS_KULLANIM, f"{self.prog}: hata: {message}\n")


def _ayristirici() -> argparse.ArgumentParser:
    ayr = _Ayristirici(
        prog="ayna.py",
        description="OFAC SDN ve Non-SDN Consolidated listelerini indirir, doğrular, "
        "manifest.json üretir.",
    )
    ayr.add_argument("--cikti", help="çıktı dizini (sdn.xml, consolidated.xml, manifest.json)")
    ayr.add_argument("--onceki", help="önceki sürümün manifest.json yolu (gerileme denetimi)")
    ayr.add_argument("--notlar", help="sürüm notlarının (Markdown) yazılacağı dosya")
    ayr.add_argument(
        "--degisti-mi",
        action="store_true",
        help="indirme yapmadan --yeni ile --onceki manifestlerini karşılaştırır; "
        "'degisti' veya 'ayni' yazar",
    )
    ayr.add_argument("--yeni", help="--degisti-mi ile: yeni manifest.json yolu")
    ayr.add_argument(
        "--gerileme-denetimini-atla",
        action="store_true",
        help="önceki manifeste göre gerileme denetimini atlar (Publish_Date geriye gitmesi, "
        "aynı tarihte %%10'dan fazla düşüş). Yalnız elle doğrulanmış bir OFAC düzeltmesi ya da "
        "hatalı bir önceki sürüm için; değişiklik algılaması yine önceki manifestle yapılır.",
    )
    return ayr


def _hata_bildir(hata: AynaHatasi) -> int:
    print(f"ayna: HATA (çıkış {hata.cikis_kodu}): {hata}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        tur = "doğrulama" if hata.cikis_kodu == CIKIS_DOGRULAMA else "indirme"
        tek_satir = str(hata).replace("\n", " ")
        print(f"::error title=Yaptırım aynası {tur} hatası::{tek_satir}")
    return hata.cikis_kodu


def _uyari_bildir(mesaj: str) -> None:
    print(f"ayna: UYARI: {mesaj}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Yaptırım aynası::{mesaj}")


def main(
    argv=None,
    *,
    listeler=LISTELER,
    uyku=time.sleep,
    saat=None,
    adres_denetimi=resmi_adres_mi,
) -> int:
    saat = saat or (lambda: datetime.now(timezone.utc))
    ayr = _ayristirici()
    args = ayr.parse_args(argv)

    if args.degisti_mi:
        if not args.yeni:
            ayr.error("--degisti-mi için --yeni gerekli")
        try:
            yeni = manifest_oku(args.yeni)
            onceki = manifest_oku(args.onceki) if args.onceki else None
        except AynaHatasi as hata:
            return _hata_bildir(hata)
        print("degisti" if degisti_mi(yeni, onceki) else "ayni")
        return CIKIS_TAMAM

    if not args.cikti:
        ayr.error("--cikti gerekli")

    gecici_dosyalar = []
    try:
        onceki = manifest_oku(args.onceki) if args.onceki else None
        os.makedirs(args.cikti, exist_ok=True)
        anahtarlar = [liste["anahtar"] for liste in listeler]
        resmi = {
            liste["anahtar"]: resmi_ozet_al(liste, uyku=uyku, adres_denetimi=adres_denetimi)
            for liste in listeler
        }
        if onceki is not None and sorted(onceki["dosyalar"]) == sorted(anahtarlar) and all(
            onceki["dosyalar"][anahtar]["sha256"] == resmi[anahtar]["sha256"]
            for anahtar in anahtarlar
        ):
            # Nabız: OFAC'ın resmî özetleri önceki sürümle aynı; dosya indirilmez, yalnız
            # son_kontrol_at yenilenmiş manifest yazılır (iş akışı onu son sürüme yükler).
            manifest = dict(onceki, son_kontrol_at=_iso(saat()))
            manifest_dogrula(manifest, anahtarlar)
            _atomik_yaz(os.path.join(args.cikti, "manifest.json"), manifest_baytlari(manifest))
            _github_ciktisi_yaz(
                {"etiket": manifest["etiket"], "degisti": "false", "nabiz": "true"}
            )
            print(ozet_satiri(manifest, False) + " | nabız")
            return CIKIS_TAMAM
        dosyalar = {}
        for liste in listeler:
            gecici = os.path.join(args.cikti, f".{liste['ad']}.part")
            gecici_dosyalar.append(gecici)
            # "indirme_url" yalnızca testler içindir; manifestte her zaman resmî kaynak_url yazılır.
            url = liste.get("indirme_url", liste["kaynak_url"])
            try:
                sha256, boyut = indir(url, gecici, uyku=uyku, adres_denetimi=adres_denetimi)
            except DogrulamaHatasi as hata:
                raise DogrulamaHatasi(f"{liste['anahtar']} ({liste['ad']}): {hata}") from hata
            beklenen = resmi[liste["anahtar"]]
            if sha256 != beklenen["sha256"] or boyut != beklenen["boyut"]:
                raise DogrulamaHatasi(
                    f"{liste['anahtar']} ({liste['ad']}): indirilen dosya OFAC'ın resmî özetiyle "
                    f"uyuşmuyor (sha256 {sha256[:12]}… / {beklenen['sha256'][:12]}…, "
                    f"boyut {boyut} / {beklenen['boyut']})"
                )
            simdi = saat()
            indirildi_at = _iso(simdi)
            try:
                bilgi = dogrula_xml(
                    gecici, liste["asgari_kayit"], liste.get("azami_kayit"), simdi=simdi
                )
            except DogrulamaHatasi as hata:
                raise DogrulamaHatasi(f"{liste['anahtar']} ({liste['ad']}): {hata}") from hata
            dosyalar[liste["anahtar"]] = {
                "ad": liste["ad"],
                "kaynak_url": liste["kaynak_url"],
                "indirildi_at": indirildi_at,
                "sha256": sha256,
                "boyut": boyut,
                "publish_date": bilgi["publish_date"],
                "record_count": bilgi["record_count"],
                "resmi_sha256": beklenen["sha256"],
            }
            print(
                f"ayna: {liste['anahtar']} {liste['ad']} indirildi ve doğrulandı "
                f"({boyut} bayt, {bilgi['record_count']} kayıt, {bilgi['publish_date']})",
                file=sys.stderr,
            )
        farkli_dosyalar_denetle(dosyalar)
        if onceki is not None:
            if args.gerileme_denetimini_atla:
                _uyari_bildir(
                    "gerileme denetimi elle atlandı (--gerileme-denetimini-atla); "
                    "Publish_Date ve kayıt sayısı önceki sürümle karşılaştırılmadı"
                )
            else:
                onceki_ile_karsilastir(dosyalar, onceki)
        manifest = manifest_olustur(dosyalar, saat())
        manifest_dogrula(manifest, [liste["anahtar"] for liste in listeler])
        manifest_verisi = manifest_baytlari(manifest)

        # Bütün denetimler geçti: dosyaları son adlarına taşı, manifesti en son yaz.
        for liste, gecici in zip(listeler, gecici_dosyalar):
            os.replace(gecici, os.path.join(args.cikti, liste["ad"]))
        _atomik_yaz(os.path.join(args.cikti, "manifest.json"), manifest_verisi)

        degisti = degisti_mi(manifest, onceki)
        if args.notlar:
            _atomik_yaz(args.notlar, notlar_olustur(manifest).encode("utf-8"))
        _github_ciktisi_yaz(
            {"etiket": manifest["etiket"], "degisti": "true" if degisti else "false", "nabiz": "false"}
        )
        print(ozet_satiri(manifest, degisti))
        return CIKIS_TAMAM
    except AynaHatasi as hata:
        return _hata_bildir(hata)
    finally:
        for gecici in gecici_dosyalar:
            _sil(gecici)


if __name__ == "__main__":
    sys.exit(main())

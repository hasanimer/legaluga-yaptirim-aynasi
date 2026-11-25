"""scripts/ayna.py birim testleri. Ağa çıkmaz; indirme testleri 127.0.0.1 üzerinde çalışır.

Çalıştırma: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

import ayna  # noqa: E402

OFAC_NS = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/XML"


def xml_uret(
    kayit: int = 3,
    record_count=None,
    publish_date: str | None = "10/02/2026",
    kok: str = "sdnList",
    ad_alani: str | None = None,
    yayin_bilgisi: bool = True,
    uidler=None,
    degisen: dict | None = None,
) -> bytes:
    """OFAC SDN biçiminde küçük, sentetik bir XML üretir.

    uidler verilirse kayıtlar bu uid'lerle (kayit yerine) üretilir; degisen {uid: soyad}
    o kayıtların lastName değerini değiştirir.
    """
    uidler = list(range(1, kayit + 1)) if uidler is None else list(uidler)
    kayit = len(uidler)
    degisen = degisen or {}
    xmlns = (
        f' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="{ad_alani}"'
        if ad_alani
        else ""
    )
    if record_count is None:
        record_count = kayit
    parcalar = ['<?xml version="1.0" standalone="yes"?>\n', f"<{kok}{xmlns}>\n"]
    if yayin_bilgisi:
        parcalar.append("  <publshInformation>\n")
        if publish_date is not None:
            parcalar.append(f"    <Publish_Date>{publish_date}</Publish_Date>\n")
        parcalar.append(f"    <Record_Count>{record_count}</Record_Count>\n")
        parcalar.append("  </publshInformation>\n")
    for i in uidler:
        parcalar.append(
            f"  <sdnEntry><uid>{i}</uid><lastName>{degisen.get(i, f'DENEME {i}')}</lastName>"
            "<sdnType>Entity</sdnType>"
            "<programList><program>SDGT</program></programList>"
            f"<akaList><aka><uid>{1000 + i}</uid><type>a.k.a.</type><category>strong</category>"
            f"<lastName>TAKMA {i}</lastName></aka></akaList></sdnEntry>\n"
        )
    parcalar.append(f"</{kok}>\n")
    return "".join(parcalar).encode("utf-8")


def girdi(sha="a" * 64, publish_date="10/02/2026", record_count=100, ad="sdn.xml") -> dict:
    return {
        "ad": ad,
        "kaynak_url": f"https://sanctionslistservice.ofac.treas.gov/api/download/{ad}",
        "indirildi_at": "2026-10-04T15:30:05Z",
        "sha256": sha,
        "boyut": 1234,
        "publish_date": publish_date,
        "record_count": record_count,
        "resmi_sha256": sha,
    }


def manifest(sdn=None, non_sdn=None) -> dict:
    return ayna.manifest_olustur(
        {
            "ofac:sdn": sdn or girdi(),
            "ofac:non_sdn": non_sdn
            or girdi(sha="b" * 64, publish_date="09/14/2026", record_count=50, ad="consolidated.xml"),
        },
        datetime(2026, 10, 4, 15, 30, 12, tzinfo=timezone.utc),
    )


class GeciciDizinli(unittest.TestCase):
    def setUp(self):
        self._dizin = tempfile.TemporaryDirectory()
        self.dizin = self._dizin.name

    def tearDown(self):
        self._dizin.cleanup()

    def yaz(self, ad: str, veri: bytes) -> str:
        yol = os.path.join(self.dizin, ad)
        with open(yol, "wb") as dosya:
            dosya.write(veri)
        return yol


class XmlDogrulamaTestleri(GeciciDizinli):
    def test_gecerli_ad_alansiz(self):
        yol = self.yaz("a.xml", xml_uret(kayit=7))
        self.assertEqual(
            ayna.dogrula_xml(yol, 1), {"publish_date": "10/02/2026", "record_count": 7}
        )

    def test_gecerli_ad_alanli(self):
        yol = self.yaz("a.xml", xml_uret(kayit=5, ad_alani=OFAC_NS, publish_date="09/14/2026"))
        self.assertEqual(
            ayna.dogrula_xml(yol, 5), {"publish_date": "09/14/2026", "record_count": 5}
        )

    def test_record_count_uyusmazligi(self):
        for ad_alani in (None, OFAC_NS):
            yol = self.yaz("a.xml", xml_uret(kayit=4, record_count=5, ad_alani=ad_alani))
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, "Record_Count"):
                ayna.dogrula_xml(yol, 1)

    def test_record_count_tamsayi_degil(self):
        # "²" ve "٢" str.isdigit() için rakamdır; int("²") ValueError verir, int("٢") 2 döner.
        for hatali in ("iki", "²", "٢", "-2", "+2", " 2 2"):
            yol = self.yaz("a.xml", xml_uret(kayit=2, record_count=hatali))
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, "tamsayı", msg=hatali):
                ayna.dogrula_xml(yol, 1)

    def test_yanlis_kok(self):
        for ad_alani in (None, OFAC_NS):
            yol = self.yaz("a.xml", xml_uret(kok="CONSOLIDATED_LIST", ad_alani=ad_alani))
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, "kök öğe"):
                ayna.dogrula_xml(yol, 1)

    def test_publish_date_eksik(self):
        yol = self.yaz("a.xml", xml_uret(publish_date=None))
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "Publish_Date"):
            ayna.dogrula_xml(yol, 1)

    def test_publish_date_bicimi(self):
        for hatali in ("2026-10-02", "10/2/2026", "13/45/2026", "", "１０/０２/２０２６", "١٠/٠٢/٢٠٢٦"):
            yol = self.yaz("a.xml", xml_uret(publish_date=hatali))
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, "Publish_Date", msg=hatali):
                ayna.dogrula_xml(yol, 1)

    def test_ileri_tarihli_publish_date_reddedilir(self):
        simdi = datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc)
        for kabul in ("10/02/2026", "10/04/2026", "10/06/2026"):
            yol = self.yaz("a.xml", xml_uret(publish_date=kabul))
            self.assertEqual(ayna.dogrula_xml(yol, 1, simdi=simdi)["publish_date"], kabul)
        for ret in ("10/07/2026", "09/14/2062"):
            yol = self.yaz("a.xml", xml_uret(publish_date=ret))
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, "ileri tarihli", msg=ret):
                ayna.dogrula_xml(yol, 1, simdi=simdi)
        # Bugün UTC ile hesaplanır: İstanbul'da 5 Ekim 01:00, UTC'de hâlâ 4 Ekim.
        yerel = datetime.fromisoformat("2026-10-05T01:00:00+03:00")
        yol = self.yaz("a.xml", xml_uret(publish_date="10/07/2026"))
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.dogrula_xml(yol, 1, simdi=yerel)

    def test_yayin_bilgisi_yok(self):
        yol = self.yaz("a.xml", xml_uret(yayin_bilgisi=False))
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "publshInformation"):
            ayna.dogrula_xml(yol, 1)

    def test_bozuk_xml(self):
        yol = self.yaz("a.xml", xml_uret(kayit=3)[:-40])
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "iyi biçimli"):
            ayna.dogrula_xml(yol, 1)

    def test_bos_dosya(self):
        yol = self.yaz("a.xml", b"")
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.dogrula_xml(yol, 1)

    def test_asgari_kayit_esigi(self):
        yol = self.yaz("a.xml", xml_uret(kayit=49))
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "asgari"):
            ayna.dogrula_xml(yol, 50)

    def test_azami_kayit_esigi(self):
        yol = self.yaz("a.xml", xml_uret(kayit=6))
        self.assertEqual(ayna.dogrula_xml(yol, 1, 6)["record_count"], 6)
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "azami"):
            ayna.dogrula_xml(yol, 1, 5)

    def test_resmi_esikler(self):
        esikler = {
            liste["anahtar"]: (liste["asgari_kayit"], liste["azami_kayit"]) for liste in ayna.LISTELER
        }
        self.assertEqual(esikler, {"ofac:sdn": (5000, None), "ofac:non_sdn": (50, 4999)})
        # Aralıklar ayrık: bir SDN dosyası consolidated.xml eşiklerinden geçemez.
        self.assertLess(esikler["ofac:non_sdn"][1], esikler["ofac:sdn"][0])

    def test_boyut_siniri_okuyucuyla_ayni(self):
        # karar-ara src/lib/mcp/yaptirim-ayna.ts AYNA_DOSYA_MAX_BYTES = 40 MiB.
        self.assertEqual(ayna.AZAMI_BOYUT, 40 * 1024 * 1024)


class OncekiSurumTestleri(unittest.TestCase):
    def onceki(self, publish_date="10/02/2026", record_count=1000):
        return manifest(sdn=girdi(publish_date=publish_date, record_count=record_count))

    def yeni(self, publish_date, record_count):
        return {"ofac:sdn": girdi(sha="c" * 64, publish_date=publish_date, record_count=record_count)}

    def test_eski_publish_date_reddedilir(self):
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "geriledi"):
            ayna.onceki_ile_karsilastir(self.yeni("10/01/2026", 1000), self.onceki())

    def test_yil_sinirinda_tarih_karsilastirmasi(self):
        # Metin karşılaştırması "01/02/2027" < "12/30/2026" der; tarih olarak karşılaştırılmalı.
        ayna.onceki_ile_karsilastir(self.yeni("01/02/2027", 1000), self.onceki("12/30/2026"))
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.onceki_ile_karsilastir(self.yeni("12/30/2026", 1000), self.onceki("01/02/2027"))

    def test_ayni_tarihte_yuzde_onu_asan_dusus_reddedilir(self):
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "düştü"):
            ayna.onceki_ile_karsilastir(self.yeni("10/02/2026", 899), self.onceki())

    def test_ayni_tarihte_yuzde_on_dusus_kabul(self):
        ayna.onceki_ile_karsilastir(self.yeni("10/02/2026", 900), self.onceki())

    def test_yeni_tarihle_buyuk_dusus_kabul(self):
        ayna.onceki_ile_karsilastir(self.yeni("10/03/2026", 100), self.onceki())

    def test_artis_kabul(self):
        ayna.onceki_ile_karsilastir(self.yeni("10/02/2026", 5000), self.onceki())

    def test_oncekinde_olmayan_liste_atlanir(self):
        onceki = self.onceki()
        del onceki["dosyalar"]["ofac:sdn"]
        ayna.onceki_ile_karsilastir(self.yeni("01/01/2000", 1), onceki)


class ManifestTestleri(GeciciDizinli):
    def test_sozlesme_bicimi(self):
        m = manifest()
        ayna.manifest_dogrula(m, ["ofac:sdn", "ofac:non_sdn"])
        self.assertEqual(
            list(m), ["surum", "etiket", "olusturuldu_at", "son_kontrol_at", "dosyalar"]
        )
        self.assertEqual(m["son_kontrol_at"], m["olusturuldu_at"])
        self.assertEqual(m["surum"], 1)
        self.assertEqual(m["etiket"], "ayna-202610041530")
        self.assertRegex(m["etiket"], r"^ayna-\d{12}$")
        self.assertEqual(m["olusturuldu_at"], "2026-10-04T15:30:12Z")
        self.assertEqual(list(m["dosyalar"]), ["ofac:sdn", "ofac:non_sdn"])
        for deger in m["dosyalar"].values():
            self.assertEqual(
                list(deger),
                [
                    "ad",
                    "kaynak_url",
                    "indirildi_at",
                    "sha256",
                    "boyut",
                    "publish_date",
                    "record_count",
                    "resmi_sha256",
                ],
            )
        veri = ayna.manifest_baytlari(m)
        self.assertLess(len(veri), 16 * 1024)
        self.assertEqual(json.loads(veri.decode("utf-8")), m)

    def test_etiket_utc(self):
        an = datetime.fromisoformat("2026-10-04T18:30:12+03:00")
        m = ayna.manifest_olustur({"ofac:sdn": girdi()}, an)
        self.assertEqual(m["etiket"], "ayna-202610041530")
        self.assertEqual(m["olusturuldu_at"], "2026-10-04T15:30:12Z")

    def test_bozuk_manifestler_reddedilir(self):
        bozucular = [
            lambda m: m.update(surum=2),
            lambda m: m.update(surum=True),
            lambda m: m.update(etiket="ayna-2026"),
            lambda m: m.update(olusturuldu_at="2026-10-04 15:30:12"),
            lambda m: m.update(son_kontrol_at="dün"),
            lambda m: m.update(son_kontrol_at="2026-10-04T15:30:11Z"),
            lambda m: m.pop("son_kontrol_at"),
            lambda m: m["dosyalar"]["ofac:sdn"].update(resmi_sha256="c" * 64),
            lambda m: m["dosyalar"]["ofac:sdn"].pop("resmi_sha256"),
            lambda m: m.update(fazla=1),
            lambda m: m.update(dosyalar={}),
            lambda m: m["dosyalar"]["ofac:sdn"].update(sha256="XYZ"),
            lambda m: m["dosyalar"]["ofac:sdn"].update(boyut="1234"),
            lambda m: m["dosyalar"]["ofac:sdn"].update(record_count=-1),
            lambda m: m["dosyalar"]["ofac:sdn"].update(publish_date="2026-10-02"),
            lambda m: m["dosyalar"]["ofac:sdn"].update(ad="../sdn.xml"),
            lambda m: m["dosyalar"]["ofac:sdn"].update(kaynak_url="http://ornek"),
            lambda m: m["dosyalar"]["ofac:sdn"].pop("indirildi_at"),
            lambda m: m["dosyalar"]["ofac:non_sdn"].update(ad="sdn.xml"),
        ]
        for bozucu in bozucular:
            m = copy.deepcopy(manifest())
            bozucu(m)
            with self.assertRaises(ayna.DogrulamaHatasi, msg=repr(m)):
                ayna.manifest_dogrula(m)

    def test_beklenen_anahtarlar(self):
        m = manifest()
        del m["dosyalar"]["ofac:non_sdn"]
        ayna.manifest_dogrula(m)
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.manifest_dogrula(m, ["ofac:sdn", "ofac:non_sdn"])

    def test_manifest_oku(self):
        yol = self.yaz("manifest.json", ayna.manifest_baytlari(manifest()))
        self.assertEqual(ayna.manifest_oku(yol), manifest())
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.manifest_oku(self.yaz("bozuk.json", b"{bozuk"))
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.manifest_oku(os.path.join(self.dizin, "yok.json"))
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.manifest_oku(self.yaz("buyuk.json", b" " * (16 * 1024 + 1)))

    def test_notlar(self):
        notlar = ayna.notlar_olustur(manifest())
        for parca in ("ayna-202610041530", "10/02/2026", "09/14/2026", "a" * 64, "b" * 64):
            self.assertIn(parca, notlar)
        self.assertIn("resmî bir OFAC kaynağı değildir", notlar)


class DegisiklikTestleri(GeciciDizinli):
    def test_onceki_yoksa_degisti(self):
        self.assertTrue(ayna.degisti_mi(manifest(), None))

    def test_ayni_sha_ayni(self):
        onceki = manifest()
        yeni = copy.deepcopy(onceki)
        yeni["etiket"] = "ayna-202610041830"
        yeni["dosyalar"]["ofac:sdn"]["indirildi_at"] = "2026-10-04T18:30:00Z"
        self.assertFalse(ayna.degisti_mi(yeni, onceki))

    def test_tek_dosya_degisirse_degisti(self):
        onceki = manifest()
        yeni = copy.deepcopy(onceki)
        yeni["dosyalar"]["ofac:non_sdn"]["sha256"] = "d" * 64
        self.assertTrue(ayna.degisti_mi(yeni, onceki))

    def test_liste_eksikse_degisti(self):
        onceki = manifest()
        del onceki["dosyalar"]["ofac:non_sdn"]
        self.assertTrue(ayna.degisti_mi(manifest(), onceki))

    def test_komut_satiri_degisti_mi(self):
        a = self.yaz("a.json", ayna.manifest_baytlari(manifest()))
        degismis = manifest()
        degismis["dosyalar"]["ofac:sdn"]["sha256"] = "e" * 64
        degismis["dosyalar"]["ofac:sdn"]["resmi_sha256"] = "e" * 64
        b = self.yaz("b.json", ayna.manifest_baytlari(degismis))
        for argv, beklenen in (
            (["--degisti-mi", "--yeni", a, "--onceki", a], "ayni"),
            (["--degisti-mi", "--yeni", b, "--onceki", a], "degisti"),
            (["--degisti-mi", "--yeni", a], "degisti"),
        ):
            cikti = io.StringIO()
            with contextlib.redirect_stdout(cikti):
                self.assertEqual(ayna.main(argv), 0)
            self.assertEqual(cikti.getvalue().strip(), beklenen)

    def test_kullanim_hatasi_64(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as baglam:
                ayna.main([])
        self.assertEqual(baglam.exception.code, 64)

    def test_ayni_dosya_iki_listede_reddedilir(self):
        ayna.farkli_dosyalar_denetle({"ofac:sdn": girdi(), "ofac:non_sdn": girdi(sha="b" * 64)})
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "aynı dosya"):
            ayna.farkli_dosyalar_denetle({"ofac:sdn": girdi(), "ofac:non_sdn": girdi()})


class AdresDenetimiTestleri(unittest.TestCase):
    def test_izinli_adresler(self):
        for url in (
            "https://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml",
            "https://SanctionsListService.OFAC.treas.gov:443/api/download/sdn.xml",
            "https://wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com/x/SDN.XML?X-Amz-Signature=abc",
            "https://s3.us-gov-west-1.amazonaws.com/kova/SDN.XML",
        ):
            self.assertTrue(ayna.resmi_adres_mi(url), url)

    def test_reddedilen_adresler(self):
        for url in (
            "http://sanctionslistservice.ofac.treas.gov/api/download/sdn.xml",
            "ftp://sanctionslistservice.ofac.treas.gov/sdn.xml",
            "https://sanctionslistservice.ofac.treas.gov:8443/api/download/sdn.xml",
            "https://kullanici@sanctionslistservice.ofac.treas.gov/api/download/sdn.xml",
            "https://ofac.treas.gov.ornek.com/sdn.xml",
            "https://ornek.com/sanctionslistservice.ofac.treas.gov",
            "https://s3.us-gov-west-1.amazonaws.com.ornek.com/x",
            "https://kotus3.us-gov-west-1.amazonaws.com/x",
            "https://kova.s3.us-east-1.amazonaws.com/x",
            "https://[::1]:99999/x",
            "/goreli/adres",
            "",
        ):
            self.assertFalse(ayna.resmi_adres_mi(url), url)


# --------------------------------------------------------------------------- yerel HTTP sunucusu


class _Isleyici(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        sunucu = self.server
        sunucu.istekler.append((self.path, dict(self.headers)))
        yanit = sunucu.yollar.get(self.path)
        if callable(yanit):
            yanit = yanit()
        if yanit == "KOPAR":  # yanıt göndermeden bağlantıyı kapat
            self.close_connection = True
            return
        if yanit is None:
            yanit = (404, {}, b"yok")
        durum, basliklar, govde = yanit
        self.send_response(durum)
        for ad, deger in basliklar.items():
            self.send_header(ad, deger)
        if "Content-Length" not in basliklar:
            self.send_header("Content-Length", str(len(govde)))
        self.end_headers()
        self.wfile.write(govde)

    do_POST = do_GET  # noqa: N815 (resmî özet uç noktası POST ister)

    def log_message(self, *args):
        pass


class SunuculuTest(GeciciDizinli):
    def setUp(self):
        super().setUp()
        self.sunucu = ThreadingHTTPServer(("127.0.0.1", 0), _Isleyici)
        self.sunucu.yollar = {}
        self.sunucu.istekler = []
        self.is_parcacigi = threading.Thread(
            target=self.sunucu.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self.is_parcacigi.start()
        self.taban = f"http://127.0.0.1:{self.sunucu.server_address[1]}"
        self.uykular = []

    def tearDown(self):
        self.sunucu.shutdown()
        self.sunucu.server_close()
        super().tearDown()

    def uyku(self, saniye):
        self.uykular.append(saniye)

    def yerel_adres_mi(self, url: str) -> bool:
        """Testlerde resmî izin listesinin yerine geçer: yalnız bu sınamanın sunucusu."""
        return url.startswith(self.taban + "/")

    def indir(self, yol: str, hedef: str, **secenekler):
        secenekler.setdefault("uyku", self.uyku)
        secenekler.setdefault("adres_denetimi", self.yerel_adres_mi)
        return ayna.indir(self.taban + yol, hedef, **secenekler)


class IndirmeTestleri(SunuculuTest):
    def test_yonlendirme_izlenir_ve_user_agent_gonderilir(self):
        govde = xml_uret(kayit=3)
        self.sunucu.yollar["/api/download/sdn.xml"] = (302, {"Location": "/s3/imzali?X-Amz=1"}, b"")
        self.sunucu.yollar["/s3/imzali?X-Amz=1"] = (200, {}, govde)
        hedef = os.path.join(self.dizin, "sdn.xml")
        sha, boyut = self.indir("/api/download/sdn.xml", hedef)
        self.assertEqual((sha, boyut), (hashlib.sha256(govde).hexdigest(), len(govde)))
        with open(hedef, "rb") as dosya:
            self.assertEqual(dosya.read(), govde)
        self.assertEqual(len(self.sunucu.istekler), 2)
        for _, basliklar in self.sunucu.istekler:
            self.assertEqual(basliklar.get("User-Agent"), ayna.USER_AGENT)
            self.assertEqual(basliklar.get("Accept-Encoding"), "identity")

    def test_5xx_yeniden_denenir(self):
        yanitlar = [(503, {}, b"mesgul"), (500, {}, b"hata"), (200, {}, b"<sdnList/>")]
        self.sunucu.yollar["/x"] = lambda: yanitlar.pop(0)
        hedef = os.path.join(self.dizin, "x")
        _, boyut = self.indir("/x", hedef)
        self.assertEqual(boyut, len(b"<sdnList/>"))
        self.assertEqual(self.uykular, [5, 15])

    def test_denemeler_tukenince_indirme_hatasi(self):
        self.sunucu.yollar["/x"] = (502, {}, b"")
        with self.assertRaises(ayna.IndirmeHatasi):
            with contextlib.redirect_stderr(io.StringIO()):
                self.indir("/x", os.path.join(self.dizin, "x"))
        self.assertEqual(len(self.sunucu.istekler), 4)  # ilk deneme + 3 yeniden deneme
        self.assertFalse(os.path.exists(os.path.join(self.dizin, "x")))

    def test_ag_hatasi_429_ve_eksik_govde_yeniden_denenir(self):
        govde = xml_uret(kayit=3)
        yanitlar = [
            "KOPAR",
            (429, {}, b""),
            (200, {"Content-Length": str(len(govde))}, govde[:50]),
            (200, {}, govde),
        ]
        self.sunucu.yollar["/x"] = lambda: yanitlar.pop(0)
        hedef = os.path.join(self.dizin, "x")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(
                self.indir("/x", hedef), (hashlib.sha256(govde).hexdigest(), len(govde))
            )
        self.assertEqual(self.uykular, [5, 15, 45])

    def test_bos_govde_yeniden_denenir(self):
        self.sunucu.yollar["/x"] = (200, {}, b"")
        with self.assertRaisesRegex(ayna.IndirmeHatasi, "boş gövde"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.indir("/x", os.path.join(self.dizin, "x"))
        self.assertEqual(len(self.sunucu.istekler), 4)

    def test_404_yeniden_denenmez(self):
        with self.assertRaisesRegex(ayna.IndirmeHatasi, "404"):
            self.indir("/yok", os.path.join(self.dizin, "x"))
        self.assertEqual(len(self.sunucu.istekler), 1)

    def test_boyut_siniri(self):
        self.sunucu.yollar["/buyuk"] = (200, {}, b"x" * 2048)
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "boyut"):
            self.indir("/buyuk", os.path.join(self.dizin, "b"), azami=1024)
        self.assertEqual(len(self.sunucu.istekler), 1)
        self.assertFalse(os.path.exists(os.path.join(self.dizin, "b")))

    def test_boyut_siniri_content_length_olmadan(self):
        self.sunucu.yollar["/buyuk"] = (200, {"Content-Length": "", "Connection": "close"}, b"x" * 4096)
        with self.assertRaises(ayna.DogrulamaHatasi):
            self.indir("/buyuk", os.path.join(self.dizin, "b"), azami=1024)

    def test_sikistirilmis_govde_reddedilir(self):
        self.sunucu.yollar["/gz"] = (200, {"Content-Encoding": "gzip"}, b"\x1f\x8b")
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "Content-Encoding"):
            self.indir("/gz", os.path.join(self.dizin, "g"))

    def test_gecersiz_content_length_cokertmez(self):
        # "²" str.isdigit() için rakamdır; eskiden int() ValueError ile çöküyordu.
        govde = xml_uret(kayit=2)
        self.sunucu.yollar["/cl"] = (200, {"Content-Length": "\u00b2", "Connection": "close"}, govde)
        hedef = os.path.join(self.dizin, "c")
        self.assertEqual(self.indir("/cl", hedef), (hashlib.sha256(govde).hexdigest(), len(govde)))
        self.sunucu.yollar["/cl2"] = (200, {"Content-Length": "\u00b2", "Connection": "close"}, b"x" * 4096)
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "boyut"):
            self.indir("/cl2", hedef, azami=1024)

    def test_izinsiz_yonlendirme_reddedilir(self):
        # Yönlendirme başka sunucuya (localhost != 127.0.0.1) ve imzalı sorgu dizgesiyle.
        port = self.sunucu.server_address[1]
        self.sunucu.yollar["/api/download/sdn.xml"] = (
            302,
            {"Location": f"http://localhost:{port}/s3/imzali?X-Amz-Signature=gizli"},
            b"",
        )
        self.sunucu.yollar["/s3/imzali?X-Amz-Signature=gizli"] = (200, {}, xml_uret())
        hedef = os.path.join(self.dizin, "sdn.xml")
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "yönlendirme") as baglam:
            self.indir("/api/download/sdn.xml", hedef)
        self.assertIn(f"localhost:{port}/s3/imzali", str(baglam.exception))
        self.assertNotIn("X-Amz-Signature", str(baglam.exception))
        self.assertEqual(len(self.sunucu.istekler), 1)  # hedefe hiç gidilmedi, yeniden denenmedi
        self.assertFalse(os.path.exists(hedef))

    def test_izinsiz_ilk_adres_reddedilir(self):
        with self.assertRaisesRegex(ayna.DogrulamaHatasi, "indirme adresi"):
            self.indir("/x", os.path.join(self.dizin, "x"), adres_denetimi=ayna.resmi_adres_mi)
        self.assertEqual(self.sunucu.istekler, [])


class UctanUcaTestler(SunuculuTest):
    def setUp(self):
        super().setUp()
        self.sdn = xml_uret(kayit=12, ad_alani=OFAC_NS, publish_date="10/02/2026")
        self.cons = xml_uret(kayit=4, ad_alani=OFAC_NS, publish_date="09/14/2026")
        self.sunucu.yollar["/api/download/sdn.xml"] = (302, {"Location": "/s3/sdn"}, b"")
        self.sunucu.yollar["/s3/sdn"] = lambda: (200, {}, self.sdn)
        self.sunucu.yollar["/api/download/consolidated.xml"] = (302, {"Location": "/s3/cons"}, b"")
        self.sunucu.yollar["/s3/cons"] = lambda: (200, {}, self.cons)
        # Resmî özetler o anki dosyalardan üretilir (testler self.sdn'i değiştirebilir).
        self.resmi_bozuk = {}
        self.sunucu.yollar["/api/PublicationPreview/SdnList"] = lambda: self.resmi_yanit(
            "SDN.XML", self.sdn
        )
        self.sunucu.yollar["/api/PublicationPreview/ConsolidatedList"] = lambda: self.resmi_yanit(
            "CONSOLIDATED.XML", self.cons
        )
        resmi = {liste["anahtar"]: liste["kaynak_url"] for liste in ayna.LISTELER}
        self.listeler = (
            {
                "anahtar": "ofac:sdn",
                "ad": "sdn.xml",
                "kaynak_url": resmi["ofac:sdn"],
                "indirme_url": self.taban + "/api/download/sdn.xml",
                "resmi_liste": "SdnList",
                "resmi_dosya": "SDN.XML",
                "resmi_ozet_url": self.taban + "/api/PublicationPreview/SdnList",
                "asgari_kayit": 10,
            },
            {
                "anahtar": "ofac:non_sdn",
                "ad": "consolidated.xml",
                "kaynak_url": resmi["ofac:non_sdn"],
                "indirme_url": self.taban + "/api/download/consolidated.xml",
                "resmi_liste": "ConsolidatedList",
                "resmi_dosya": "CONSOLIDATED.XML",
                "resmi_ozet_url": self.taban + "/api/PublicationPreview/ConsolidatedList",
                "asgari_kayit": 2,
            },
        )
        self.github_ciktisi = os.path.join(self.dizin, "github_output")
        open(self.github_ciktisi, "w").close()
        self.ortam = mock.patch.dict(
            os.environ, {"GITHUB_OUTPUT": self.github_ciktisi, "GITHUB_ACTIONS": ""}
        )
        self.ortam.start()

    def tearDown(self):
        self.ortam.stop()
        super().tearDown()

    def resmi_yanit(self, dosya_adi: str, govde: bytes):
        if dosya_adi in self.resmi_bozuk:
            return self.resmi_bozuk[dosya_adi]
        kayitlar = [
            {"fileName": "SDN.CSV", "size": 1, "hashCodes": json.dumps({"SHA-256": "f" * 64})},
            {
                "fileName": dosya_adi,
                "size": len(govde),
                "hashCodes": json.dumps({"SHA-256": hashlib.sha256(govde).hexdigest().upper()}),
            },
        ]
        return (200, {"Content-Type": "application/json"}, json.dumps(kayitlar).encode("utf-8"))

    def test_nabiz_dosya_indirmeden_son_kontrol_at_yeniler(self):
        ilk = os.path.join(self.dizin, "ilk")
        kod, _, hata = self.calistir(["--cikti", ilk])
        self.assertEqual(kod, 0, hata)
        onceki = os.path.join(ilk, "manifest.json")
        self.assertEqual(
            ayna.manifest_oku(onceki)["dosyalar"]["ofac:sdn"]["resmi_sha256"],
            hashlib.sha256(self.sdn).hexdigest(),
        )
        self.sunucu.istekler.clear()
        open(self.github_ciktisi, "w").close()
        ikinci = os.path.join(self.dizin, "ikinci")
        kod, cikti, hata = self.calistir(
            ["--cikti", ikinci, "--onceki", onceki],
            an=datetime(2026, 10, 4, 18, 30, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(kod, 0, hata)
        self.assertIn("nabız", cikti)
        self.assertEqual(os.listdir(ikinci), ["manifest.json"])
        self.assertTrue(all("/api/PublicationPreview/" in yol for yol, _ in self.sunucu.istekler))
        m = ayna.manifest_oku(os.path.join(ikinci, "manifest.json"))
        self.assertEqual(m["etiket"], "ayna-202610041530")
        self.assertEqual(m["olusturuldu_at"], "2026-10-04T15:30:12Z")
        self.assertEqual(m["son_kontrol_at"], "2026-10-04T18:30:00Z")
        self.assertEqual(
            self.github_ciktilari(),
            {"etiket": "ayna-202610041530", "degisti": "false", "nabiz": "true"},
        )

    def test_resmi_ozetle_uyusmayan_dosya_2_ve_cikti_yok(self):
        self.resmi_bozuk["SDN.XML"] = (
            200,
            {},
            json.dumps(
                [{"fileName": "SDN.XML", "size": len(self.sdn), "hashCodes": json.dumps({"SHA-256": "0" * 64})}]
            ).encode("utf-8"),
        )
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("resmî özetiyle uyuşmuyor", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])

    def test_resmi_ozet_bicimi_bozuksa_2(self):
        for bozuk in (
            (200, {}, b"<html>bakim</html>"),
            (200, {}, b"{}"),
            (200, {}, json.dumps([{"fileName": "SDN.XML", "size": 5}]).encode("utf-8")),
        ):
            self.resmi_bozuk["SDN.XML"] = bozuk
            self.uykular.clear()
            kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
            self.assertEqual(kod, 2, hata)
            self.assertIn("resmî özet yanıtı beklenen biçimde değil", hata)
            # Yalnız JSON olmayan gövde (bakım sayfası olabilir) yeniden denenir; biçimi yanlış
            # geçerli JSON hemen hatadır.
            self.assertEqual(
                len(self.uykular), ayna.YENIDEN_DENEME if bozuk[2].startswith(b"<") else 0, bozuk
            )

    def test_resmi_ozet_bir_kez_json_degilse_yeniden_denenir(self):
        asil = self.sunucu.yollar["/api/PublicationPreview/SdnList"]
        yanitlar = [(200, {}, b"<html>bakim</html>")]
        self.sunucu.yollar["/api/PublicationPreview/SdnList"] = lambda: (
            yanitlar.pop(0) if yanitlar else asil()
        )
        kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
        self.assertEqual(kod, 0, hata)
        self.assertEqual(self.uykular, [5])

    def test_resmi_ozet_ag_hatasi_ve_429_yeniden_denenir(self):
        asil = self.sunucu.yollar["/api/PublicationPreview/SdnList"]
        yanitlar = ["KOPAR", (429, {}, b"")]
        self.sunucu.yollar["/api/PublicationPreview/SdnList"] = lambda: (
            yanitlar.pop(0) if yanitlar else asil()
        )
        kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
        self.assertEqual(kod, 0, hata)
        self.assertEqual(self.uykular[:2], [5, 15])

    def test_resmi_ozet_baglanti_koparsa_3(self):
        self.resmi_bozuk["SDN.XML"] = "KOPAR"
        kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
        self.assertEqual(kod, 3, hata)
        self.assertEqual(len(self.uykular), ayna.YENIDEN_DENEME)

    def test_resmi_ozet_alinamazsa_3(self):
        self.resmi_bozuk["SDN.XML"] = (503, {}, b"")
        kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
        self.assertEqual(kod, 3, hata)
        self.assertEqual(len(self.uykular), ayna.YENIDEN_DENEME)

    # Bu sınıfın testleri değişiklik verisi kapalıyken (özellikten önceki davranış) çalışır;
    # DegisiklikUctanUcaTestleri açık hâlini sınar.
    degisiklikler_acik = False

    def calistir(self, argv, an=datetime(2026, 10, 4, 15, 30, 12, tzinfo=timezone.utc)):
        if not self.degisiklikler_acik:
            argv = list(argv) + ["--degisiklikler-kapali"]
        cikti, hata = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(cikti), contextlib.redirect_stderr(hata):
            kod = ayna.main(
                argv,
                listeler=self.listeler,
                uyku=self.uyku,
                saat=lambda: an,
                adres_denetimi=self.yerel_adres_mi,
                gecmis_koku=self.taban + "/changes/history/",
                delta_koku=self.taban + "/changes/",
            )
        return kod, cikti.getvalue(), hata.getvalue()

    def github_ciktilari(self) -> dict:
        with open(self.github_ciktisi, encoding="utf-8") as dosya:
            return dict(satir.strip().split("=", 1) for satir in dosya if satir.strip())

    def test_ilk_calistirma_ve_degismeyen_ikinci_calistirma(self):
        cikti_dizini = os.path.join(self.dizin, "cikti")
        notlar = os.path.join(self.dizin, "notlar.md")
        kod, cikti, hata = self.calistir(["--cikti", cikti_dizini, "--notlar", notlar])
        self.assertEqual(kod, 0, hata)
        self.assertEqual(len(cikti.strip().splitlines()), 1)
        self.assertIn("ayna-202610041530 degisti", cikti)
        self.assertEqual(sorted(os.listdir(cikti_dizini)), ["consolidated.xml", "manifest.json", "sdn.xml"])
        with open(os.path.join(cikti_dizini, "sdn.xml"), "rb") as dosya:
            self.assertEqual(dosya.read(), self.sdn)
        with open(os.path.join(cikti_dizini, "consolidated.xml"), "rb") as dosya:
            self.assertEqual(dosya.read(), self.cons)
        m = ayna.manifest_oku(os.path.join(cikti_dizini, "manifest.json"))
        ayna.manifest_dogrula(m, ["ofac:sdn", "ofac:non_sdn"])
        self.assertEqual(m["etiket"], "ayna-202610041530")
        self.assertEqual(m["dosyalar"]["ofac:sdn"]["sha256"], hashlib.sha256(self.sdn).hexdigest())
        self.assertEqual(m["dosyalar"]["ofac:sdn"]["boyut"], len(self.sdn))
        self.assertEqual(m["dosyalar"]["ofac:sdn"]["record_count"], 12)
        self.assertEqual(m["dosyalar"]["ofac:non_sdn"]["publish_date"], "09/14/2026")
        self.assertEqual(m["dosyalar"]["ofac:non_sdn"]["ad"], "consolidated.xml")
        self.assertEqual(
            m["dosyalar"]["ofac:non_sdn"]["kaynak_url"],
            "https://sanctionslistservice.ofac.treas.gov/api/download/consolidated.xml",
        )
        self.assertEqual(
            self.github_ciktilari(),
            {"etiket": "ayna-202610041530", "degisti": "true", "nabiz": "false"},
        )
        with open(notlar, encoding="utf-8") as dosya:
            self.assertIn(hashlib.sha256(self.cons).hexdigest(), dosya.read())

        onceki_dizini = os.path.join(self.dizin, "onceki")
        os.makedirs(onceki_dizini)
        os.replace(os.path.join(cikti_dizini, "manifest.json"), os.path.join(onceki_dizini, "manifest.json"))
        open(self.github_ciktisi, "w").close()
        kod, cikti, hata = self.calistir(
            ["--cikti", cikti_dizini, "--onceki", os.path.join(onceki_dizini, "manifest.json")],
            an=datetime(2026, 10, 4, 18, 17, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(kod, 0, hata)
        # Resmî özetler değişmedi: nabız (yeni etiket yok, dosya indirilmez).
        self.assertIn("ayna-202610041530 ayni", cikti)
        self.assertEqual(
            self.github_ciktilari(),
            {"etiket": "ayna-202610041530", "degisti": "false", "nabiz": "true"},
        )

    def test_dogrulama_hatasi_2_ve_cikti_yok(self):
        self.cons = xml_uret(kayit=4, record_count=5, ad_alani=OFAC_NS)
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, cikti, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("ofac:non_sdn", hata)
        self.assertEqual(cikti, "")
        self.assertEqual(os.listdir(cikti_dizini), [])
        self.assertEqual(self.github_ciktilari(), {})

    def test_gerileme_hatasi_2(self):
        onceki = manifest(sdn=girdi(publish_date="10/03/2026", record_count=12))
        onceki_yolu = self.yaz("onceki.json", ayna.manifest_baytlari(onceki))
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini, "--onceki", onceki_yolu])
        self.assertEqual(kod, 2)
        self.assertIn("geriledi", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])

    def test_gerileme_denetimi_elle_atlanir(self):
        onceki = manifest(sdn=girdi(publish_date="10/03/2026", record_count=12))
        onceki_yolu = self.yaz("onceki.json", ayna.manifest_baytlari(onceki))
        cikti_dizini = os.path.join(self.dizin, "cikti")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            kod, cikti, hata = self.calistir(
                ["--cikti", cikti_dizini, "--onceki", onceki_yolu, "--gerileme-denetimini-atla"]
            )
        self.assertEqual(kod, 0, hata)
        self.assertIn("gerileme denetimi elle atlandı", hata)
        self.assertIn("::warning title=Yaptırım aynası::", cikti)
        self.assertIn("ayna-202610041530 degisti", cikti)
        self.assertEqual(self.github_ciktilari()["degisti"], "true")
        self.assertEqual(
            sorted(os.listdir(cikti_dizini)), ["consolidated.xml", "manifest.json", "sdn.xml"]
        )

    def test_ileri_tarihli_dosya_2(self):
        self.sdn = xml_uret(kayit=12, ad_alani=OFAC_NS, publish_date="09/14/2062")
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("ofac:sdn", hata)
        self.assertIn("ileri tarihli", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])

    def test_yanlis_yonlendirilen_dosya_2(self):
        # consolidated adresi SDN dosyasını veriyor.
        self.cons = self.sdn
        listeler = [dict(liste) for liste in self.listeler]
        listeler[1]["azami_kayit"] = None  # yalnız sha256 denetimini sına
        self.listeler = tuple(listeler)
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("aynı dosya", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])
        # Azami eşik de tek başına yakalar.
        listeler[1]["azami_kayit"] = 11
        self.listeler = tuple(listeler)
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("azami", hata)

    def test_izinsiz_yonlendirme_2(self):
        self.sunucu.yollar["/api/download/consolidated.xml"] = (
            302,
            {"Location": "http://localhost:9/s3/cons"},
            b"",
        )
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 2)
        self.assertIn("ofac:non_sdn", hata)
        self.assertIn("yönlendirme", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])

    def test_indirme_hatasi_3(self):
        self.sunucu.yollar["/s3/cons"] = (503, {}, b"")
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 3)
        self.assertIn("indirilemedi", hata)
        self.assertEqual(os.listdir(cikti_dizini), [])

    def test_github_actions_hata_bildirimi(self):
        self.sdn = b"<CONSOLIDATED_LIST/>"
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            kod, cikti, _ = self.calistir(["--cikti", os.path.join(self.dizin, "cikti")])
        self.assertEqual(kod, 2)
        self.assertTrue(re.match(r"^::error title=Yaptırım aynası doğrulama hatası::", cikti))


# --------------------------------------------------------------------------- değişiklik verisi

DELTA_NS = "https://www.treasury.gov/ofac/DeltaFile/1.0"


def delta_uret(tarih="2026-10-02T00:00:00-04:00", varliklar=(), ad_alani=DELTA_NS, tur="Standard Action"):
    """DeltaFile/1.0 biçiminde küçük bir delta XML'i. varliklar: [(id, action ya da None)]."""
    parcalar = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n',
        f'<sanctionsData xmlns="{ad_alani}">\n',
        f"  <publicationInfo><datePublished>{tarih}</datePublished>"
        f"<publicationType>{tur}</publicationType></publicationInfo>\n",
        "  <referenceValues /><featureTypes />\n  <entities>\n",
    ]
    for kimlik, eylem in varliklar:
        ozn = f' action="{eylem}"' if eylem else ""
        parcalar.append(
            f'    <entity id="{kimlik}"{ozn}><names><name id="9{kimlik}" action="add">'
            "<isPrimary>true</isPrimary></name></names>"
            '<relationships><relationship id="7"><relatedEntity entityId="5">X</relatedEntity>'
            "</relationship></relationships></entity>\n"
        )
    parcalar.append("  </entities>\n</sanctionsData>\n")
    return "".join(parcalar).encode("utf-8")


def isaretci(son=1107, sayi=3, sha="c" * 64, boyut=100) -> dict:
    return {
        "ad": "degisiklikler.json",
        "sha256": sha,
        "boyut": boyut,
        "son_yayin_id": son,
        "yayin_sayisi": sayi,
    }


class DegisiklikManifestTestleri(unittest.TestCase):
    def test_eski_ve_yeni_manifest_gecerli(self):
        eski = manifest()
        ayna.manifest_dogrula(eski, ["ofac:sdn", "ofac:non_sdn"])
        yeni = dict(manifest(), degisiklikler=isaretci())
        ayna.manifest_dogrula(yeni, ["ofac:sdn", "ofac:non_sdn"])
        ayna.manifest_dogrula(dict(manifest(), degisiklikler=isaretci(son=None, sayi=0)))
        self.assertEqual(yeni["surum"], 1)
        self.assertLess(len(ayna.manifest_baytlari(yeni)), 16 * 1024)

    def test_bozuk_isaretci_reddedilir(self):
        bozuklar = [
            "degisiklikler.json",
            dict(isaretci(), fazla=1),
            {k: v for k, v in isaretci().items() if k != "boyut"},
            isaretci(sha="XYZ"),
            isaretci(boyut=0),
            isaretci(boyut=ayna.DEGISIKLIK_OZETI_AZAMI_BAYT + 1),
            isaretci(son=None, sayi=3),
            isaretci(son=1107, sayi=0),
            isaretci(son=True),
            isaretci(son=-1),
            dict(isaretci(), ad="sdn.xml"),
            dict(isaretci(), ad="../degisiklikler.json"),
        ]
        for bozuk in bozuklar:
            with self.assertRaises(ayna.DogrulamaHatasi, msg=repr(bozuk)):
                ayna.manifest_dogrula(dict(manifest(), degisiklikler=bozuk))
        # Bilinmeyen başka üst düzey alanlar hâlâ reddedilir.
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.manifest_dogrula(dict(manifest(), degisiklikler=isaretci(), fazla=1))

    def test_degisti_mi_yayin_verisi(self):
        eski = manifest()
        self.assertFalse(ayna.degisti_mi(manifest(), eski))
        # Yayın verisi ilk kez eklenir.
        self.assertTrue(ayna.degisti_mi(dict(manifest(), degisiklikler=isaretci()), eski))
        onceki = dict(manifest(), degisiklikler=isaretci(son=1105))
        self.assertTrue(ayna.degisti_mi(dict(manifest(), degisiklikler=isaretci(son=1107)), onceki))
        self.assertFalse(ayna.degisti_mi(dict(manifest(), degisiklikler=isaretci(son=1105)), onceki))
        # Yayın verisi alınamayan çalıştırma, aynı dosyalarla öncekinin yerine geçmez.
        self.assertFalse(
            ayna.degisti_mi(dict(manifest(), degisiklikler=isaretci(son=None, sayi=0)), onceki)
        )
        self.assertFalse(ayna.degisti_mi(manifest(), onceki))


class DeltaDogrulamaTestleri(GeciciDizinli):
    def test_gecerli_delta(self):
        yol = self.yaz(
            "d.xml",
            delta_uret(varliklar=[(30, "add"), (10, "remove"), (20, None), (5, "add")]),
        )
        self.assertEqual(
            ayna.delta_dogrula(yol, "2026-10-02T11:32:59.89"),
            {
                "delta_tarihi": "2026-10-02T00:00:00-04:00",
                "yayin_turu": "Standard Action",
                "eklenen": [5, 30],
                "cikarilan": [10],
                "degisen": [20],
            },
        )
        bos = self.yaz("bos.xml", delta_uret())
        self.assertEqual(ayna.delta_dogrula(bos, "2026-10-02T09:00:00")["eklenen"], [])

    def test_bozuk_deltalar(self):
        durumlar = [
            (delta_uret(ad_alani="https://www.treasury.gov/ofac/DeltaFile/2.0"), "kök"),
            (xml_uret(), "kök"),
            (delta_uret(varliklar=[(1, "update")]), "action"),
            (delta_uret(varliklar=[(1, "add"), (1, None)]), "tekrar"),
            (delta_uret(varliklar=[("01", "add")]), "id"),
            (delta_uret(varliklar=[("x", "add")]), "id"),
            (delta_uret(tarih="2026-10-03T00:00:00-04:00"), "gününe"),
            (delta_uret(tarih="dün"), "datePublished"),
            (delta_uret(tur=""), "publicationType"),
            (delta_uret()[:-30], "iyi biçimli"),
            (delta_uret().replace(b"<entities>", b"<entities></entities><entities>"), "entities"),
        ]
        for veri, beklenen in durumlar:
            yol = self.yaz("d.xml", veri)
            with self.assertRaisesRegex(ayna.DogrulamaHatasi, beklenen, msg=veri[:200]):
                ayna.delta_dogrula(yol, "2026-10-02T11:00:00")


class ListeFarkiTestleri(GeciciDizinli):
    def test_eklenen_cikarilan_degisen(self):
        eski = self.yaz("eski.xml", xml_uret(uidler=[1, 2, 3, 4]))
        yeni = self.yaz("yeni.xml", xml_uret(uidler=[2, 3, 4, 5, 6], degisen={3: "YENI AD"}))
        self.assertEqual(
            ayna.liste_farki_hesapla(ayna.kayit_ozetleri(eski), ayna.kayit_ozetleri(yeni)),
            {"eklenen": [5, 6], "cikarilan": [1], "degisen": [3]},
        )

    def test_girinti_ve_ad_alani_degisiklik_sayilmaz(self):
        duz = xml_uret(kayit=3)
        ns = xml_uret(kayit=3, ad_alani=OFAC_NS)
        girintili = duz.replace(b"<lastName>", b"\n      <lastName>")
        ozet = ayna.kayit_ozetleri(self.yaz("a.xml", duz))
        self.assertEqual(ozet, ayna.kayit_ozetleri(self.yaz("b.xml", ns)))
        self.assertEqual(ozet, ayna.kayit_ozetleri(self.yaz("c.xml", girintili)))
        self.assertEqual(sorted(ozet), [1, 2, 3])  # akaList içindeki uid'ler kayıt sayılmaz

    def test_kardes_sirasi_degisiklik_sayilmaz(self):
        # OFAC dosyayı her yayında yeniden üretirken liste öğelerinin sırasını değiştirir.
        def idli(*idler):
            liste = "".join(
                f"<id><uid>{no}</uid><idType>{tur}</idType><idNumber>{deger}</idNumber></id>"
                for no, tur, deger in idler
            )
            return xml_uret(kayit=2).replace(
                b"<uid>1</uid><lastName>", f"<uid>1</uid><idList>{liste}</idList><lastName>".encode()
            )

        a, b, c = (91, "Passport", "A1"), (92, "Tax ID No.", "B2"), (92, "Tax ID No.", "B3")
        ozet = ayna.kayit_ozetleri(self.yaz("a.xml", idli(a, b)))
        self.assertEqual(ozet, ayna.kayit_ozetleri(self.yaz("b.xml", idli(b, a))))
        farkli = ayna.kayit_ozetleri(self.yaz("c.xml", idli(c, a)))  # içerik değişikliği
        self.assertEqual(
            ayna.liste_farki_hesapla(ozet, farkli), {"eklenen": [], "cikarilan": [], "degisen": [1]}
        )
        # Aynı kardeşin tekrarı kaybolmaz: iki kez geçen id bir kez geçenden farklıdır.
        self.assertNotEqual(ozet, ayna.kayit_ozetleri(self.yaz("d.xml", idli(a, b, b))))
        # Değer bir kardeşten ötekine taşınırsa kayıt değişmiştir.
        tasinan = ayna.kayit_ozetleri(
            self.yaz("e.xml", idli((91, "Passport", "B2"), (92, "Tax ID No.", "A1")))
        )
        self.assertNotEqual(ozet, tasinan)

    def test_bozuk_uid(self):
        for veri in (
            xml_uret(uidler=[1, 1]),
            xml_uret(kayit=1).replace(b"<uid>1</uid>", b"<uid>bir</uid>"),
            xml_uret(kayit=1).replace(b"<uid>1</uid>", b""),
        ):
            with self.assertRaises(ayna.DogrulamaHatasi):
                ayna.kayit_ozetleri(self.yaz("a.xml", veri))


class DegisiklikOzetiTestleri(unittest.TestCase):
    def ozet(self) -> dict:
        return {
            "surum": 1,
            "etiket": "ayna-202610041530",
            "olusturuldu_at": "2026-10-04T15:30:12Z",
            "ofac_yayinlari": {
                "gecmis_url": ayna.DEGISIKLIK_GECMIS_KOKU,
                "yayinlar": [
                    {
                        "yayin_id": 1107,
                        "yayin_zamani": "2026-10-02T11:32:59.899866",
                        "delta_tarihi": "2026-10-02T00:00:00-04:00",
                        "yayin_turu": "Standard Action",
                        "ad": "ofac-delta-1107.xml",
                        "kaynak_url": ayna.DEGISIKLIK_DELTA_KOKU + "1107",
                        "sha256": "d" * 64,
                        "boyut": 10,
                        "eklenen": [1, 2],
                        "cikarilan": [],
                        "degisen": [3],
                    }
                ],
            },
            "liste_farki": {
                "ofac:sdn": {
                    "onceki_etiket": "ayna-202610031530",
                    "onceki_sha256": "a" * 64,
                    "onceki_publish_date": "10/01/2026",
                    "sha256": "b" * 64,
                    "publish_date": "10/02/2026",
                    "eklenen": [1],
                    "cikarilan": [],
                    "degisen": [],
                }
            },
            "uyarilar": [],
        }

    def test_gecerli(self):
        ayna.degisiklik_ozeti_dogrula(self.ozet(), "ayna-202610041530")
        ozet = dict(self.ozet(), ofac_yayinlari=None)
        ayna.degisiklik_ozeti_dogrula(ozet)

    def test_bozuk(self):
        def yayin(m):
            return m["ofac_yayinlari"]["yayinlar"][0]

        def fark(m):
            return m["liste_farki"]["ofac:sdn"]

        bozucular = [
            lambda m: m.update(surum=2),
            lambda m: m.update(fazla=1),
            lambda m: m.update(uyarilar=[1]),
            lambda m: m["ofac_yayinlari"].update(gecmis_url="https://ornek/"),
            lambda m: m["ofac_yayinlari"].update(yayinlar=[]),
            lambda m: yayin(m).update(ad="../sdn.xml"),
            lambda m: yayin(m).update(ad="ofac-delta-1.xml"),
            lambda m: yayin(m).update(kaynak_url="https://ornek/1107"),
            lambda m: yayin(m).update(eklenen=[2, 1]),
            lambda m: yayin(m).update(eklenen=[0]),
            lambda m: yayin(m).update(yayin_turu="<b>"),
            lambda m: m["ofac_yayinlari"]["yayinlar"].append(dict(yayin(m))),
            lambda m: fark(m).update(onceki_publish_date="2026-10-01"),
            lambda m: fark(m).update(onceki_sha256="b" * 64),
            lambda m: fark(m).pop("degisen"),
        ]
        for bozucu in bozucular:
            m = copy.deepcopy(self.ozet())
            bozucu(m)
            with self.assertRaises(ayna.DogrulamaHatasi, msg=repr(m)):
                ayna.degisiklik_ozeti_dogrula(m)
        with self.assertRaises(ayna.DogrulamaHatasi):
            ayna.degisiklik_ozeti_dogrula(self.ozet(), "ayna-202610041531")


class DegisiklikUctanUcaTestleri(UctanUcaTestler):
    """UctanUcaTestler'in bütün testleri değişiklik verisi açıkken de çalışır (miras); aşağıdaki
    testler değişiklik verisine özgüdür."""

    degisiklikler_acik = True

    def setUp(self):
        super().setUp()
        self.gecmis = {
            2026: [
                {"publicationID": 1107, "datePublished": "2026-10-02T11:32:59.899866"},
                {"publicationID": 1105, "datePublished": "2026-09-14T14:36:11.1"},
            ],
            2025: [{"publicationID": 1001, "datePublished": "2025-12-30T10:00:00"}],
        }
        self.deltalar = {
            1107: delta_uret("2026-10-02T00:00:00-04:00", [(12, "add"), (3, None)]),
            1105: delta_uret("2026-09-14T00:00:00-04:00", [(99, "remove")]),
            1001: delta_uret("2025-12-30T00:00:00-05:00", [(7, None)]),
        }
        self.gecmis_bozuk = {}
        for yil in (2025, 2026):
            self.sunucu.yollar[f"/changes/history/{yil}"] = lambda yil=yil: self.gecmis_bozuk.get(
                yil, (200, {}, json.dumps(self.gecmis[yil]).encode("utf-8"))
            )
        for kimlik in (1107, 1105, 1001):
            self.sunucu.yollar[f"/changes/{kimlik}"] = (
                302,
                {"Location": f"/s3/DeltaArchive/{kimlik}_delta.xml?X-Amz-Signature=gizli"},
                b"",
            )
            self.sunucu.yollar[f"/s3/DeltaArchive/{kimlik}_delta.xml?X-Amz-Signature=gizli"] = (
                lambda kimlik=kimlik: (200, {}, self.deltalar[kimlik])
            )

    # Miras alınan iki test çıktı dizinini tam olarak sayar; değişiklik verisiyle dosya listesi
    # genişler. Burada aynı senaryolar yeni dosyalarla sınanır.
    def test_ilk_calistirma_ve_degismeyen_ikinci_calistirma(self):
        cikti_dizini = os.path.join(self.dizin, "cikti")
        notlar = os.path.join(self.dizin, "notlar.md")
        kod, cikti, hata = self.calistir(["--cikti", cikti_dizini, "--notlar", notlar])
        self.assertEqual(kod, 0, hata)
        self.assertIn("degisiklikler son_yayin_id=1107 yayin_sayisi=3", cikti)
        self.assertEqual(
            sorted(os.listdir(cikti_dizini)),
            [
                "consolidated.xml",
                "degisiklikler.json",
                "manifest.json",
                "ofac-delta-1001.xml",
                "ofac-delta-1105.xml",
                "ofac-delta-1107.xml",
                "sdn.xml",
            ],
        )
        m = ayna.manifest_oku(os.path.join(cikti_dizini, "manifest.json"))
        self.assertEqual(m["surum"], 1)
        self.assertEqual(m["degisiklikler"]["son_yayin_id"], 1107)
        self.assertEqual(m["degisiklikler"]["yayin_sayisi"], 3)
        ozet = self.ozet_oku(cikti_dizini)
        self.assertEqual(ozet["etiket"], m["etiket"])
        yayinlar = ozet["ofac_yayinlari"]["yayinlar"]
        self.assertEqual([y["yayin_id"] for y in yayinlar], [1107, 1105, 1001])
        self.assertEqual(
            yayinlar[0],
            {
                "yayin_id": 1107,
                "yayin_zamani": "2026-10-02T11:32:59.899866",
                "delta_tarihi": "2026-10-02T00:00:00-04:00",
                "yayin_turu": "Standard Action",
                "ad": "ofac-delta-1107.xml",
                "kaynak_url": "https://sanctionslistservice.ofac.treas.gov/changes/1107",
                "sha256": hashlib.sha256(self.deltalar[1107]).hexdigest(),
                "boyut": len(self.deltalar[1107]),
                "eklenen": [12],
                "cikarilan": [],
                "degisen": [3],
            },
        )
        self.assertEqual(yayinlar[1]["cikarilan"], [99])
        with open(os.path.join(cikti_dizini, "ofac-delta-1105.xml"), "rb") as dosya:
            self.assertEqual(dosya.read(), self.deltalar[1105])
        self.assertEqual(ozet["liste_farki"], {})
        self.assertEqual(len(ozet["uyarilar"]), 2)  # iki liste için "önceki sürüm yok"
        self.assertNotIn("X-Amz-Signature", json.dumps(ozet))
        with open(notlar, encoding="utf-8") as dosya:
            self.assertIn("| 1107 | 2026-10-02T11:32 | Standard Action | 1 | 0 | 1 |", dosya.read())
        self.assertEqual(
            self.yayin_dosyalari(cikti_dizini),
            [
                "sdn.xml",
                "consolidated.xml",
                "degisiklikler.json",
                "ofac-delta-1107.xml",
                "ofac-delta-1105.xml",
                "ofac-delta-1001.xml",
                "manifest.json",
            ],
        )

        onceki = self.onceki_hazirla(cikti_dizini)
        open(self.github_ciktisi, "w").close()
        ikinci = os.path.join(self.dizin, "ikinci")
        kod, cikti, hata = self.calistir(
            ["--cikti", ikinci, "--onceki", onceki, "--onceki-dizin", os.path.dirname(onceki)]
        )
        self.assertEqual(kod, 0, hata)
        self.assertIn("nabız", cikti)
        self.assertEqual(os.listdir(ikinci), ["manifest.json"])
        self.assertEqual(
            ayna.manifest_oku(os.path.join(ikinci, "manifest.json"))["degisiklikler"],
            m["degisiklikler"],
        )

    def test_nabiz_dosya_indirmeden_son_kontrol_at_yeniler(self):
        onceki = self.ilk_surum()
        self.sunucu.istekler.clear()
        dizin, cikti, _ = self.ikinci(onceki)
        self.assertIn("nabız", cikti)
        self.assertEqual(os.listdir(dizin), ["manifest.json"])
        # Nabızda yalnız resmî özetler ve değişiklik geçmişi okunur; dosya ya da delta indirilmez.
        self.assertEqual(
            sorted({yol for yol, _ in self.sunucu.istekler}),
            [
                "/api/PublicationPreview/ConsolidatedList",
                "/api/PublicationPreview/SdnList",
                "/changes/history/2025",  # 2026'da 30'dan az yayın var
                "/changes/history/2026",
            ],
        )
        m = ayna.manifest_oku(os.path.join(dizin, "manifest.json"))
        self.assertEqual(m["son_kontrol_at"], "2026-10-04T18:17:03Z")
        self.assertEqual(m["etiket"], "ayna-202610041530")

    def test_gerileme_denetimi_elle_atlanir(self):
        onceki = manifest(sdn=girdi(publish_date="10/03/2026", record_count=12))
        onceki_yolu = self.yaz("onceki.json", ayna.manifest_baytlari(onceki))
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, cikti, hata = self.calistir(
            ["--cikti", cikti_dizini, "--onceki", onceki_yolu, "--gerileme-denetimini-atla"]
        )
        self.assertEqual(kod, 0, hata)
        self.assertIn("gerileme denetimi elle atlandı", hata)
        self.assertEqual(self.github_ciktilari()["degisti"], "true")
        # Önceki dosyalar (onceki-dizin) yok: liste farkı uyarıyla atlanır.
        ozet = self.ozet_oku(cikti_dizini)
        self.assertEqual(ozet["liste_farki"], {})
        self.assertEqual(len(ozet["uyarilar"]), 2)

    def ozet_oku(self, dizin: str) -> dict:
        with open(os.path.join(dizin, "degisiklikler.json"), encoding="utf-8") as dosya:
            return json.load(dosya)

    def yayin_dosyalari(self, dizin: str) -> list:
        cikti, hata = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(cikti), contextlib.redirect_stderr(hata):
            kod = ayna.main(["--yayin-dosyalari", dizin])
        self.assertEqual(kod, 0, hata.getvalue())
        return cikti.getvalue().split()

    def onceki_hazirla(self, cikti_dizini: str) -> str:
        """Çıktıyı iş akışının indirdiği gibi onceki/ dizinine taşır; manifest yolunu döner."""
        onceki_dizini = os.path.join(self.dizin, "onceki")
        if os.path.isdir(onceki_dizini):
            for ad in os.listdir(onceki_dizini):
                os.remove(os.path.join(onceki_dizini, ad))
        os.makedirs(onceki_dizini, exist_ok=True)
        for ad in ("manifest.json", "sdn.xml", "consolidated.xml"):
            os.replace(os.path.join(cikti_dizini, ad), os.path.join(onceki_dizini, ad))
        return os.path.join(onceki_dizini, "manifest.json")

    def ilk_surum(self) -> str:
        cikti_dizini = os.path.join(self.dizin, "ilk")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 0, hata)
        open(self.github_ciktisi, "w").close()
        return self.onceki_hazirla(cikti_dizini)

    def ikinci(self, onceki: str, *ek):
        cikti_dizini = os.path.join(self.dizin, "ikinci")
        kod, cikti, hata = self.calistir(
            ["--cikti", cikti_dizini, "--onceki", onceki, "--onceki-dizin", os.path.dirname(onceki), *ek],
            an=datetime(2026, 10, 4, 18, 17, 3, tzinfo=timezone.utc),
        )
        self.assertEqual(kod, 0, hata)
        return cikti_dizini, cikti, hata

    def test_yalniz_yeni_ofac_yayini_yeni_surum_acar(self):
        onceki = self.ilk_surum()
        self.gecmis[2026].insert(0, {"publicationID": 1110, "datePublished": "2026-10-04T12:00:00"})
        self.deltalar[1110] = delta_uret("2026-10-04T00:00:00-04:00", [(4, None)])
        self.sunucu.yollar["/changes/1110"] = (200, {}, self.deltalar[1110])
        dizin, cikti, _ = self.ikinci(onceki)
        self.assertIn("ayna-202610041817 degisti", cikti)
        self.assertEqual(self.github_ciktilari()["degisti"], "true")
        ozet = self.ozet_oku(dizin)
        self.assertEqual(ozet["ofac_yayinlari"]["yayinlar"][0]["yayin_id"], 1110)
        for fark in ozet["liste_farki"].values():
            self.assertEqual(fark["onceki_etiket"], "ayna-202610041530")
            self.assertEqual((fark["eklenen"], fark["cikarilan"], fark["degisen"]), ([], [], []))
        self.assertEqual(ozet["uyarilar"], [])

    def test_liste_farki_onceki_surume_gore(self):
        onceki = self.ilk_surum()
        self.sdn = xml_uret(
            uidler=[2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14],
            ad_alani=OFAC_NS,
            publish_date="10/03/2026",
            degisen={5: "YENI AD"},
        )
        notlar = os.path.join(self.dizin, "notlar.md")
        dizin, cikti, _ = self.ikinci(onceki, "--notlar", notlar)
        self.assertIn("degisti", cikti)
        with open(notlar, encoding="utf-8") as dosya:
            self.assertIn(
                "- `ofac:sdn` (`ayna-202610041530` → bu sürüm): 2 eklenen, 1 çıkarılan, 1 değişen",
                dosya.read(),
            )
        fark = self.ozet_oku(dizin)["liste_farki"]
        self.assertEqual(
            fark["ofac:sdn"],
            {
                "onceki_etiket": "ayna-202610041530",
                "onceki_sha256": ayna.manifest_oku(onceki)["dosyalar"]["ofac:sdn"]["sha256"],
                "onceki_publish_date": "10/02/2026",
                "sha256": hashlib.sha256(self.sdn).hexdigest(),
                "publish_date": "10/03/2026",
                "eklenen": [13, 14],
                "cikarilan": [1],
                "degisen": [5],
            },
        )
        self.assertEqual(fark["ofac:non_sdn"]["eklenen"], [])

    def test_onceki_dosya_ozetine_uymazsa_liste_farki_atlanir(self):
        onceki = self.ilk_surum()
        with open(os.path.join(os.path.dirname(onceki), "sdn.xml"), "ab") as dosya:
            dosya.write(b"\n")
        self.sdn = xml_uret(kayit=13, ad_alani=OFAC_NS, publish_date="10/03/2026")
        dizin, _, hata = self.ikinci(onceki)
        ozet = self.ozet_oku(dizin)
        self.assertNotIn("ofac:sdn", ozet["liste_farki"])
        self.assertIn("ofac:non_sdn", ozet["liste_farki"])
        self.assertTrue(any("özetine uymuyor" in u for u in ozet["uyarilar"]))
        self.assertIn("UYARI", hata)

    def test_eski_bicimli_onceki_manifestten_gecis(self):
        # Özellikten önceki biçimde (degisiklikler alanı yok) bir önceki sürüm.
        self.degisiklikler_acik = False
        onceki = self.ilk_surum()
        self.assertNotIn("degisiklikler", ayna.manifest_oku(onceki))
        self.degisiklikler_acik = True
        dizin, cikti, _ = self.ikinci(onceki)
        self.assertIn("degisti", cikti)
        m = ayna.manifest_oku(os.path.join(dizin, "manifest.json"))
        self.assertEqual(m["degisiklikler"]["son_yayin_id"], 1107)
        self.assertEqual(set(self.ozet_oku(dizin)["liste_farki"]), {"ofac:sdn", "ofac:non_sdn"})

    def test_kapali_iken_yeni_bicimli_onceki_ile_nabiz(self):
        onceki = self.ilk_surum()
        self.degisiklikler_acik = False
        dizin, cikti, _ = self.ikinci(onceki)
        self.assertIn("nabız", cikti)
        self.assertIn("degisiklikler", ayna.manifest_oku(os.path.join(dizin, "manifest.json")))
        # Dosyalar değişince kapalı çalıştırma eski biçimli manifest yazar.
        self.sdn = xml_uret(kayit=13, ad_alani=OFAC_NS, publish_date="10/03/2026")
        dizin, cikti, _ = self.ikinci(onceki)
        self.assertIn("degisti", cikti)
        self.assertEqual(
            sorted(os.listdir(dizin)), ["consolidated.xml", "manifest.json", "sdn.xml"]
        )
        self.assertNotIn("degisiklikler", ayna.manifest_oku(os.path.join(dizin, "manifest.json")))

    def test_gecmis_alinamazsa_ayna_yine_yayimlar(self):
        self.gecmis_bozuk[2026] = (503, {}, b"")
        cikti_dizini = os.path.join(self.dizin, "cikti")
        notlar = os.path.join(self.dizin, "notlar.md")
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            kod, cikti, hata = self.calistir(["--cikti", cikti_dizini, "--notlar", notlar])
        self.assertEqual(kod, 0, hata)
        with open(notlar, encoding="utf-8") as dosya:
            self.assertIn("OFAC değişiklik akışı bu sürümde yok", dosya.read())
        self.assertIn("::warning title=Yaptırım aynası::OFAC değişiklik geçmişi alınamadı", cikti)
        m = ayna.manifest_oku(os.path.join(cikti_dizini, "manifest.json"))
        self.assertEqual(m["degisiklikler"]["son_yayin_id"], None)
        ozet = self.ozet_oku(cikti_dizini)
        self.assertIsNone(ozet["ofac_yayinlari"])
        self.assertTrue(ozet["uyarilar"][0].startswith("OFAC değişiklik geçmişi alınamadı"))
        self.assertEqual(self.yayin_dosyalari(cikti_dizini)[-2:], ["degisiklikler.json", "manifest.json"])

    def test_bicimi_bozuk_gecmis_uyari(self):
        for bozuk in (
            b"<html/>",
            b"{}",
            json.dumps([{"publicationID": "1107", "datePublished": "2026-10-02T11:00:00"}]).encode(),
            json.dumps([{"publicationID": 1107, "datePublished": "dün"}]).encode(),
            json.dumps([{"publicationID": 1107, "datePublished": "2026-10-02T11:00:00"}] * 2).encode(),
        ):
            self.gecmis_bozuk[2026] = (200, {}, bozuk)
            self.uykular.clear()
            with self.assertRaises(ayna.DogrulamaHatasi, msg=bozuk):
                ayna.yayin_gecmisi_al(
                    datetime(2026, 10, 4, tzinfo=timezone.utc),
                    gecmis_koku=self.taban + "/changes/history/",
                    uyku=self.uyku,
                    adres_denetimi=self.yerel_adres_mi,
                )
            # Yalnız JSON olmayan gövde yeniden denenir.
            self.assertEqual(
                len(self.uykular), ayna.YENIDEN_DENEME if bozuk.startswith(b"<") else 0, bozuk
            )

    def test_gecmis_yil_siniri_ve_sayi(self):
        al = lambda sayi: ayna.yayin_gecmisi_al(  # noqa: E731
            datetime(2026, 10, 4, tzinfo=timezone.utc),
            gecmis_koku=self.taban + "/changes/history/",
            sayi=sayi,
            uyku=self.uyku,
            adres_denetimi=self.yerel_adres_mi,
        )
        self.assertEqual([y["yayin_id"] for y in al(2)], [1107, 1105])
        self.assertNotIn("/changes/history/2025", [yol for yol, _ in self.sunucu.istekler])
        self.assertEqual([y["yayin_id"] for y in al(5)], [1107, 1105, 1001])

    def test_izinsiz_delta_yonlendirmesi_uyari_ve_delta_yok(self):
        port = self.sunucu.server_address[1]
        self.sunucu.yollar["/changes/1105"] = (
            302,
            {"Location": f"http://localhost:{port}/s3/x?X-Amz-Signature=gizli"},
            b"",
        )
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 0, hata)
        self.assertIn("OFAC delta dosyaları alınamadı: yayın 1105", hata)
        self.assertNotIn("X-Amz-Signature", hata)
        self.assertFalse([ad for ad in os.listdir(cikti_dizini) if ad.startswith(("ofac-delta", "."))])
        self.assertIsNone(self.ozet_oku(cikti_dizini)["ofac_yayinlari"])

    def test_yanlis_gunun_deltasi_reddedilir(self):
        self.deltalar[1105] = self.deltalar[1107]
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 0, hata)
        self.assertIn("gününe", hata)
        self.assertIsNone(self.ozet_oku(cikti_dizini)["ofac_yayinlari"])

    def test_delta_alinamazsa_onceki_surum_korunur_nabiz(self):
        onceki = self.ilk_surum()
        self.gecmis[2026].insert(0, {"publicationID": 1110, "datePublished": "2026-10-04T12:00:00"})
        self.sunucu.yollar["/changes/1110"] = (404, {}, b"")
        dizin, cikti, hata = self.ikinci(onceki)
        self.assertIn("nabız", cikti)
        self.assertIn("OFAC delta dosyaları alınamadı", hata)
        self.assertEqual(os.listdir(dizin), ["manifest.json"])
        self.assertEqual(
            ayna.manifest_oku(os.path.join(dizin, "manifest.json"))["degisiklikler"]["son_yayin_id"],
            1107,
        )

    def test_yayin_dosyalari_degistirilmis_dosyayi_yakalar(self):
        cikti_dizini = os.path.join(self.dizin, "cikti")
        kod, _, hata = self.calistir(["--cikti", cikti_dizini])
        self.assertEqual(kod, 0, hata)
        for ad in ("ofac-delta-1105.xml", "degisiklikler.json", "sdn.xml"):
            yol = os.path.join(cikti_dizini, ad)
            with open(yol, "rb") as dosya:
                asil = dosya.read()
            with open(yol, "ab") as dosya:
                dosya.write(b" ")
            hata = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(hata):
                self.assertEqual(ayna.main(["--yayin-dosyalari", cikti_dizini]), 2, ad)
            self.assertIn(ad, hata.getvalue())
            with open(yol, "wb") as dosya:
                dosya.write(asil)
        self.assertEqual(len(self.yayin_dosyalari(cikti_dizini)), 7)

    def test_delta_adresleri_resmi_izin_listesinde(self):
        self.assertTrue(ayna.resmi_adres_mi(ayna.DEGISIKLIK_GECMIS_KOKU + "2026"))
        self.assertTrue(ayna.resmi_adres_mi(ayna.DEGISIKLIK_DELTA_KOKU + "1107"))
        self.assertTrue(
            ayna.resmi_adres_mi(
                "https://wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com/"
                "DeltaArchive/2026-11-13_delta.xml?X-Amz-Signature=x"
            )
        )


if __name__ == "__main__":
    unittest.main()

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
) -> bytes:
    """OFAC SDN biçiminde küçük, sentetik bir XML üretir."""
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
    for i in range(1, kayit + 1):
        parcalar.append(
            f"  <sdnEntry><uid>{i}</uid><lastName>DENEME {i}</lastName><sdnType>Entity</sdnType>"
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
            kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
            self.assertEqual(kod, 2, hata)
            self.assertIn("resmî özet yanıtı beklenen biçimde değil", hata)

    def test_resmi_ozet_alinamazsa_3(self):
        self.resmi_bozuk["SDN.XML"] = (503, {}, b"")
        kod, _, hata = self.calistir(["--cikti", os.path.join(self.dizin, "c")])
        self.assertEqual(kod, 3, hata)
        self.assertEqual(len(self.uykular), ayna.YENIDEN_DENEME)

    def calistir(self, argv, an=datetime(2026, 10, 4, 15, 30, 12, tzinfo=timezone.utc)):
        cikti, hata = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(cikti), contextlib.redirect_stderr(hata):
            kod = ayna.main(
                argv,
                listeler=self.listeler,
                uyku=self.uyku,
                saat=lambda: an,
                adres_denetimi=self.yerel_adres_mi,
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


if __name__ == "__main__":
    unittest.main()

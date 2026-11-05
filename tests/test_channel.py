"""Offline controls only. All HTTP responses, products, prices and screenshots are fixtures."""
import io
import sys
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from PIL import Image, ImageDraw

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"skills/d11-research/scripts"))
import capture
import channel

NAME="样例牌绿泥控油洁面膏"
NORMAL="已选规格：绿泥 120g 单支\n2026年双十一\n活动标价：49元\n店铺券：满60减10\n加赠：旅行装一件"
PIC="https://fixture.example.org/main.png"
TM="https://detail.tmall.com/item.htm?id=100000000000"
DY="https://haohuo.jinritemai.com/views/product/item2?id=100000000001"

class TestChannel(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve(); self.run_dir=self.root/"batch"
        channel.write(self.run_dir/"state.json",{"year":2026,"candidates":{"fixture":{"brand":"样例牌","title":NAME,"sku":"120g 单支"}},"selections":["fixture"]})
        im=Image.new("RGB",(700,700),"white"); d=ImageDraw.Draw(im)
        d.rounded_rectangle((265,120,435,575),radius=25,fill=(92,116,104));d.rectangle((280,330,420,385),fill=(210,230,190))
        buf=io.BytesIO();im.save(buf,format="PNG");self.picture=buf.getvalue()

    def collect(self,url=TM,lines=NORMAL,*,page=None,expected_image=True):
        self.calls=[]
        html=page or ('<html><head><meta charset="utf-8"><meta property="og:type" content="product"><title>'+NAME+
            '</title></head><body><h1>'+NAME+'</h1><div class="product-gallery"><img src="'+PIC+'"></div>'+
            ''.join("<div>"+x+"</div>" for x in lines.splitlines())+'<div>立即购买</div></body></html>')
        def fake(url_,kind,**kw):
            self.calls.append((kind,url_))
            self.assertIn(url_,[url,PIC] if expected_image else [url])
            h=Message(); h['Content-Type']="text/html; charset=utf-8" if kind=="page" else "image/png"
            return url_,NS(status=200,headers=h,body=html.encode() if kind=="page" else self.picture),[{"url":url_}]
        with patch.object(capture,"fetch",side_effect=fake):
            cap,path=channel.collect(NS(run=self.run_dir,candidate="fixture",variant="绿泥",url=url,out=self.root/"attempts"))
        return cap,path

    def compare(self,c,path,**overrides):
        r={"source":"rendered_page","page_url":c["url"],"page_title":NAME,"at":c["at"],
           "selected_sku":"120克 一支","activity_price":"49.00","campaign":"2026年双十一",
           "benefits":["店铺券：满60减10","加赠：旅行装一件"],"screenshot":"screen.png","main_image_box":[10,10,360,360],**overrides}
        ref=self.root/"reference"/"view.json"; channel.write(ref,r)
        canvas=Image.new("RGB",(700,580),(238,238,238))
        with Image.open(io.BytesIO(self.picture)) as photo:canvas.paste(photo.resize((350,350),Image.Resampling.LANCZOS),(10,10))
        if getattr(self,"change_label",False): ImageDraw.Draw(canvas).rectangle((150,173,218,203),fill=(34,18,20))
        canvas.save(ref.parent/"screen.png")
        # Test clock is fixed to the declared fixture campaign. Never assign it to real evidence.
        c["at"]="2026-11-06T06:00:00+08:00";channel.write(path,c)
        if "at" not in overrides:r["at"]=c["at"];channel.write(ref,r)
        return channel.compare(NS(capture=path,reference=ref,out=self.root/"comparison.json"))[0]

    def test_both_detail_formats_roundtrip(self):
        for url in (TM,DY):
            with self.subTest(url=url):
                c,path=self.collect(url)
                self.assertEqual(c["status"],"awaiting_independent_page",c["missing"])
                self.assertEqual([kind for kind,url_ in self.calls],["page","image"])
                out=self.compare(c,path)
                self.assertEqual(out["checks"],{k:True for k in ("规格","图片","活动价","优惠")});self.assertTrue(out["passed"])

    def test_wrong_pack_or_variant_cannot_lend_price_or_media(self):
        for variant in ("120g×2", "2x120g 单支", "120g/180g 单支","氨基酸 120g 单支"):
            with self.subTest(variant=variant):
                c,_=self.collect(lines=NORMAL.replace("绿泥 120g 单支",variant),expected_image=False)
                self.assertEqual(c["status"],"incomplete");self.assertIsNone(c["fields"]["price"]);self.assertFalse(c["main_image"])

    def test_marketing_prices_and_recommendations(self):
        for offer in ("活动价：39–59元\n活动标价：49元","券后到手价：49元","活动价：49元起","活动价：49元\n活动价：50元"):
            with self.subTest(offer=offer):
                c,_=self.collect(lines=NORMAL.replace("活动标价：49元",offer)+"\n猜你喜欢\n活动价：100元\n平台券：满5减3")
                self.assertIsNone(c["fields"]["price"]);self.assertNotIn("满5减3",str(c["fields"]["benefits"]))

    def test_unknown_promotion_or_other_year_is_not_a_match(self):
        for block in ("", "2025年双十一", "2025、2026年双十一", "2026双十一已结束"):
            with self.subTest(block=block):
                c,p=self.collect(lines=NORMAL.replace("2026年双十一",block))
                out=self.compare(c,p);self.assertTrue(out["checks"]["图片"]);self.assertIsNone(out["checks"]["活动价"]);self.assertFalse(out["passed"])
        c,p=self.collect(lines=NORMAL.replace("\n店铺券：满60减10\n加赠：旅行装一件",""))
        self.assertIsNone(self.compare(c,p)["checks"]["优惠"])

    def test_independent_price_benefit_and_small_label_differences(self):
        c,p=self.collect()
        self.change_label=True
        out=self.compare(c,p,activity_price="48",benefits=["店铺券：满80减10"])
        self.assertEqual(out["checks"],{"规格":True,"图片":False,"活动价":False,"优惠":False})
        self.assertFalse(out["passed"])
        out=self.compare(c,p,selected_sku="氨基酸 120克 一支")
        self.assertEqual(out["checks"],{"规格":False,"图片":None,"活动价":None,"优惠":None})

    def test_wrong_reference_time_and_item(self):
        c,p=self.collect()
        for changed in ({"page_url":DY},{"at":"2026-11-07T06:00:00+08:00"},{"page_title":"另一款洁面"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):self.compare(c,p,**changed)

    def test_login_and_wrong_site(self):
        c,p=self.collect(page="<html><title>用户登录</title><body>请先登录</body></html>",expected_image=False)
        self.assertEqual(c["status"],"incomplete");self.assertEqual(c["fields"],{})
        for bad in ("https://www.douyin.com/video/100000000000","https://www.example.org/product/100000000000"):
            with self.subTest(url=bad), self.assertRaises(ValueError):self.collect(bad)
            self.assertEqual(self.calls,[])

    def test_redirect_target_checked_before_transport(self):
        short="https://v.douyin.com/offlinefixture/"
        def deny_after_first(url_,kind,deadline):
            self.assertEqual(url_,short);h=Message();h['Location']=TM
            return NS(status=302,headers=h,body=b"",route="offline-fixture")
        with self.assertRaises(capture.CaptureError) as ex:
            capture.fetch(short,"page",request=deny_after_first,before_hop=channel.request_guard(None,short,{channel.ident(TM)}))
        self.assertEqual(ex.exception.code,"access_not_permitted")
        with patch.object(capture,"get_hop",side_effect=AssertionError("transport must not be called")), self.assertRaises(capture.CaptureError):
            capture.fetch(TM,"page",before_hop=channel.request_guard(channel.ident(TM),None,{channel.ident(TM)}))

if __name__=="__main__":unittest.main(verbosity=2)

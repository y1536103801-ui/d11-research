"""Collect four channel fields from a permitted public detail; compare to a separate on-screen reading."""
import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import unicodedata
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from PIL import Image, ImageChops, ImageStat, ImageOps
from capture import capture_page, capture_image, public_url, fail, CaptureError

def clean(s):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(s))).strip()

def packed(s):
    return re.sub(r"\s+", "", clean(s)).lower()

def ident(url):
    u = urlsplit(public_url(url)); h = u.hostname or ""; q = parse_qs(u.query)
    def host(d): return h == d or h.endswith("." + d)
    def value(k):
        vs=q.get(k, [])
        return vs[0] if len(vs)==1 and re.fullmatch(r"[0-9]{6,24}", vs[0]) else None
    if host("tmall.com") and re.fullmatch(r"/(?:item\.htm|item\.html|item/detail\.htm)", u.path) and value("id"):
        return ("天猫",value("id"))
    if any(host(d) for d in ("douyin.com","jinritemai.com","douyinec.com")):
        direct=re.fullmatch(r"/(?:mall/)?(?:goods|product)/(?:detail/)?(\d{6,24})/?",u.path)
        if direct:return ("抖音",direct[1])
        if re.fullmatch(r"/(?:ecommerce/(?:trade|product)/detail(?:/index\.html)?|views/product/item2?|product/detail|mall/product/detail)/?",u.path):
            keys=[k for k in ("product_id","goods_id","id") if k in q]
            vals={value(k) for k in keys}
            if len(vals)==1 and None not in vals:return ("抖音",vals.pop())
    return None

def request_guard(initial,short_url,stops):
    def check(url):
        p=urlsplit(url);h=p.hostname or "";target=ident(url)
        if target in stops:fail("access_not_permitted","此商品入口已记录宿主限制，未发送请求。")
        if short_url and url==short_url:return
        if target and ((initial and target==initial) or (short_url and target[0]=="抖音")):return
        if h in {"login.taobao.com","login.tmall.com","passport.douyin.com"}:
            fail("login_redirect","公开商品页转向平台登录；未读取账号页。")
        fail("not_same_detail","跳转目标不是同一获准渠道商品详情，未发送请求。")
    return check

def sku(s):
    v=packed(s)
    sizes=re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(毫升|ml|克|g)(?![a-z])",v)
    if len(sizes)!=1 or re.search(r"[+/~–—]|(?:\d)\s*-\s*(?:\d)|套装|礼盒|任选|或|赠|组合|毫克|买.{0,4}送",v):return None
    # Remove the size first: in "2x120g" the 120 is never the item count.
    stripped=re.sub(r"(?<![\d.])\d+(?:\.\d+)?(?:毫升|ml|克|g)(?![a-z])","@",v)
    counts={int(x) for x in re.findall(r"[*×x](\d+)",stripped)}
    counts.update(int(x) for x in re.findall(r"(\d+)[*×x]@",stripped))
    counts.update(int(x) for x in re.findall(r"(\d+)(?:支|瓶|管|件)",stripped))
    if re.search(r"单(?:支|瓶|管|件)|一(?:支|瓶|管|件)",stripped): counts.add(1)
    if re.search(r"双支|两(?:支|瓶|管|件)",stripped): counts.add(2)
    if len(counts)!=1 or min(counts)<1: return None
    n,unit=sizes[0]
    return (Decimal(n), "ml" if unit in ("ml","毫升") else "g", counts.pop())

def same_variant(selection,title):
    residue=re.sub(r"\d+(?:\.\d+)?(?:毫升|ml|克|g)|(?:\d+|[单一两双])(?:瓶|支|管|件)(?:装)?|[*×x]\d+|\d+[*×x]|正装|净含量|规格|标准装"," ",packed(selection))
    return all(token in packed(title) for token in re.findall(r"[\u3400-\u9fffA-Za-z0-9]+",residue))

SELECT=re.compile(r"^(?:已选(?:择|规格|款式)?|选中规格|当前规格|唯一规格)\s*[:：]?\s*(.*)$")
PRICE=re.compile(r"^(?:双(?:十一|11)(?:活动)?(?:标)?价|活动(?:标)?价|大促(?:活动)?价)\s*[:：]?\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)(?:\s*(?:元|人民币))?$")
PRICE_HEAD=re.compile(r"^(?:双(?:十一|11)(?:活动)?(?:标)?价|活动(?:标)?价|大促(?:活动)?价)\s*[:：]?$")
PRICE_PREFIX=re.compile(r"^(?:双(?:十一|11)(?:活动)?(?:标)?价|活动(?:标)?价|大促(?:活动)?价)")
BENEFIT=re.compile(r"^(?:优惠(?:明细|信息|说明)?[:：]|店铺券|平台券|购物券|会员(?:专享|权益|券)|88VIP|满\s*\d|每满\s*\d|直降|立减|加赠|赠品|满赠|购物金|平台补贴|直播(?:间)?专享|暂无优惠|无可用优惠)",re.I)
PRIVATE=re.compile(r"收件人|收货地址|支付宝账户|手机号码|订单编号|验证码|1[3-9]\d{9}")
CAMPAIGN=re.compile(r"双\s*(?:11|十一)|11[.·]11")
def fields(body, expected, year, candidate_title):
    body=body.split("[公开 Product 结构化数据]",1)[0] # SEO offer ranges are not a selected offer.
    lines=[]
    for line in body.splitlines():
        s=clean(line)
        if re.fullmatch(r"猜你喜欢|其他商品推荐|为你推荐|为您推荐|看了又看|店铺推荐|相关推荐|用户评价|商品评价",s):break
        if s and len(s)<=240 and not PRIVATE.search(s):lines.append(s)
    choices=[]; prices=[]; bad_prices=[]; benefits=[]; labels=[]
    for i,s in enumerate(lines):
        nxt=lines[i+1] if i+1<len(lines) else ""
        m=SELECT.fullmatch(s)
        if m:choices.append(m[1] or nxt)
        m=PRICE.fullmatch(s) or (PRICE.fullmatch(s.rstrip("：:")+" "+nxt) if PRICE_HEAD.fullmatch(s) else None)
        quote=s+" "+nxt if PRICE_HEAD.fullmatch(s) else s
        if m and Decimal(m[1])>0:prices.append((str(Decimal(m[1]).quantize(Decimal("0.01"))),quote))
        elif PRICE_PREFIX.match(s):bad_prices.append(quote)
        if (BENEFIT.match(s) and re.search(r"\d|赠.{2,}|暂无优惠|无可用优惠|包邮|免邮|优先发货|提前购|试用装|积分翻倍",s)
                and not re.search(r"以实物为准|以实际为准|已失效|已过期|展开查看",s) and s not in benefits):benefits.append(s)
        if is_campaign(s,year):labels.append(s)
    variants=[sku(x) for x in choices]; expected=sku(expected)
    bound=bool(expected and variants and all(v==expected for v in variants) and
               all(same_variant(x,candidate_title) for x in choices))
    amounts={x[0] for x in prices}
    if len(benefits)>1 and any(x in ("暂无优惠","无可用优惠") for x in benefits):benefits=[]
    return {"selected":choices[0] if bound else None, "selected_quotes":list(dict.fromkeys(choices)),
            "price":next(iter(amounts)) if bound and len(amounts)==1 and not bad_prices else None,"price_quotes":list(dict.fromkeys([x[1] for x in prices]+bad_prices)),
            "campaign":labels[:4], "benefits":benefits if bound else []}

def is_campaign(s,year):
    return bool(CAMPAIGN.search(s) and set(re.findall(r"(?<!\d)(20\d{2})(?!\d)",s))=={str(year)} and
                not re.search(r"去年|上年|往年|历史|已结束|已失效",s))

def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def read(path):return json.loads(Path(path).read_text(encoding="utf-8"))
def write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
def instant(s):
    d=dt.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    if d.utcoffset() is None:raise ValueError("取证时间必须含时区")
    return d

def collect(a):
    state=read(Path(a.run)/"state.json")
    c=state["candidates"].get(a.candidate)
    if not c or a.candidate not in state["selections"]:raise ValueError("请先在当前批次认款")
    variant=packed(a.variant)
    if len(variant)<2 or variant not in packed(c["title"]) or variant in {"洁面","洁面膏","男士","控油","洗面奶","单支",packed(c["brand"])}:
        raise ValueError("款名须取自已认款标题，且能区分同品牌的其他洁面")
    u=public_url(a.url); initial=ident(u); bits=urlsplit(u)
    short=bits.hostname=="v.douyin.com" and re.fullmatch(r"/[A-Za-z0-9_-]+/?",bits.path)
    if not initial and not short:raise ValueError("请提供天猫/抖音具体商品详情或抖音公开商品分享链接，搜索页、视频和品牌站不采为渠道样本")
    # Only for an explicitly permitted public product; never reuse an account session.
    dest=Path(a.out).absolute();dest.mkdir(parents=True,exist_ok=True)
    ts=dt.datetime.now(dt.timezone.utc);out=dest/("attempt-"+ts.strftime("%Y%m%dT%H%M%S%fZ"));out.mkdir()
    stopped=Path(a.run)/"受限商品.json"
    stops={tuple(x) for x in read(stopped)["items"]} if stopped.is_file() else set()
    cap=capture_page(u,str(out),before_hop=request_guard(initial,u if short else None,stops))
    actual=ident(cap["url"]) if cap.get("url") else None
    result={"candidate_id":a.candidate,"expected_sku":c["sku"],"candidate_title":c["title"],"brand":c["brand"],"variant":a.variant,"year":state["year"],
            "at":cap.get("retrieved_at") or ts.isoformat(),"source":"public_detail_get","target":actual,"url":cap.get("url") if actual else None,
            "page_status":cap.get("status"),"receipt":Path(cap["receipt_path"]).name if cap.get("receipt_path") else None,"fields":{},"main_image":None,"missing":[]}
    pending=result["missing"]
    if cap.get("status")!="ok" or cap.get("evidence_ok") is not True or not actual or (initial and actual!=initial):
        pending.append("未取得可读的同一渠道商品详情；不解析价格或图片")
    elif not all(packed(x) in packed(str(cap.get("product_name") or cap.get("title") or "")) for x in (c["brand"],a.variant)):
        pending.append("渠道商品标题未同时匹配认款品牌与款名")
    else:
        t=Path(cap["text_path"])
        if sha(t)!=cap["text_sha256"]:raise ValueError("页面正文校验值不一致")
        body=t.read_text(encoding="utf-8").partition("\n\n")[2]; f=fields(body,c["sku"],state["year"],c["title"]);result["fields"]=f;result["title"]=cap.get("product_name") or cap["title"]
        for k,label in (("selected","所选规格/件数"),("price","单一活动标价"),("campaign","有年份的本期活动标识"),("benefits","明确优惠或明确无优惠")):
            if not f[k]:pending.append(label)
        # The media is still a candidate until compared to the visible SKU hero.
        images=[it for it in cap.get("image_candidates",[]) if it.get("url") in cap.get("images",[]) and
                ("gallery" in it.get("contexts",[]) or it.get("source","").startswith(("og:image","jsonld:image","link:image_src")))]
        if f["selected"] and images:
            def rank(it):
                source=it.get("source","")
                return 0 if "gallery" in it.get("contexts",[]) else 1 if source.startswith("jsonld:image") else 2
            it=sorted(images,key=rank)[0]; im=capture_image(it["url"],str(out),from_page=cap["receipt_path"])
            if im.get("status")=="ok" and min(im["width"],im["height"])>=600:
                result["main_image"]={"file":Path(im["path"]).name,"sha256":im["sha256"],"width":im["width"],"height":im["height"]}
        if not result["main_image"]:pending.append("同一商品页的高清主图（短边至少 600 像素）")
    result["status"]="incomplete" if pending else "awaiting_independent_page"
    write(out/"capture.json",result);return result,out/"capture.json"

def signature(im):
    # Ignore the screenshot's alpha only after compositing on white; preserve orientation.
    im=ImageOps.exif_transpose(im).convert("RGBA")
    bg=Image.new("RGBA",im.size,"white");bg.alpha_composite(im)
    return ImageOps.contain(bg.convert("RGB"),(160,160),Image.Resampling.LANCZOS)

def image_match(oi,si,box):
    if not(isinstance(box,list) and len(box)==4 and all(type(v) is int for v in box) and
           0<=box[0]<box[2]<=si.width and 0<=box[1]<box[3]<=si.height and
           min(box[2]-box[0],box[3]-box[1])>=100 and
           (box[2]-box[0])*(box[3]-box[1])<si.width*si.height*.95):
        raise ValueError("须标出完整页面截图内、清晰可辨的主图区域")
    ai,bi=signature(oi),signature(si.crop(tuple(box)))
    if abs(ai.width/ai.height-bi.width/bi.height)>.035:return False
    bi=bi.resize(ai.size,Image.Resampling.LANCZOS)
    diff=ImageChops.difference(ai,bi)
    if max(ImageStat.Stat(diff).mean)>=7 or min(ImageStat.Stat(z.convert("L")).stddev[0] for z in (ai,bi))<=12:return False
    # A large white background must not hide a different bottle or label.
    def foreground(z):
        w,h=z.size; corners=[z.getpixel(xy) for xy in ((0,0),(0,h-1),(w-1,0),(w-1,h-1))]
        back=tuple(sorted(p[j] for p in corners)[1] for j in range(3))
        delta=ImageChops.difference(z,Image.new("RGB",z.size,back))
        rr,gg,bb=delta.split()
        return ImageChops.lighter(rr,ImageChops.lighter(gg,bb)).point(lambda v:255 if v>24 else 0)
    mask=ImageChops.lighter(foreground(ai),foreground(bi))
    if mask.histogram()[255]<ai.width*ai.height*.035:return False
    if max(ImageStat.Stat(diff,mask).mean)>=8:return False
    # A small region can contain the only difference (e.g. label, size).
    rr,gg,bb=diff.split()
    hist=ImageChops.lighter(rr,ImageChops.lighter(gg,bb)).histogram(mask)
    return sum(hist[45:])/sum(hist)<0.025

def compare(a):
    cp=Path(a.capture).resolve();ref_path=Path(a.reference).resolve(); c=read(cp); r=read(ref_path)
    if c.get("source")!="public_detail_get" or c.get("page_status")!="ok" or not c.get("fields"):
        raise ValueError("没有完成同款商品详情采集，不能核对")
    if not c.get("target") or tuple(c["target"])!=ident(c["url"]) or tuple(c["target"])!=ident(r["page_url"]):
        raise ValueError("对照页不是同平台同商品编号")
    ts=instant(r["at"]); ct=instant(c["at"])
    if abs((ts-ct).total_seconds())>600:raise ValueError("采集与独立页面记录相差超过 10 分钟")
    if r.get("source")!="rendered_page" or not all(packed(x) in packed(r.get("page_title","")) for x in (c["brand"],c["variant"])):
        raise ValueError("对照材料须来自独立可见页面，标题须明确为同品牌同款")
    def file_for(directory,name):
        if not isinstance(name,str) or Path(name).is_absolute():raise ValueError("附件必须为相对路径")
        path=(directory/name).resolve(strict=True)
        if not path.is_relative_to(directory):raise ValueError("附件路径越界")
        return path
    screen=file_for(ref_path.parent,r["screenshot"]); pixels=None
    with Image.open(screen) as si:
        si.load()
        if c.get("main_image") and r.get("main_image_box"):
            orig=file_for(cp.parent,c["main_image"]["file"])
            if sha(orig)!=c["main_image"]["sha256"] or sha(screen)==sha(orig):raise ValueError("需要独立页面截图，不能把原图作为页面截图")
            with Image.open(orig) as oi:pixels=image_match(oi,si,r["main_image_box"])
    f=c["fields"]
    refquotes=r.get("benefits")
    if refquotes is not None and (not isinstance(refquotes,list) or not all(isinstance(s,str) and s.strip() for s in refquotes)):
        raise ValueError("优惠对照须逐条录入可见文字")
    # The reference is transcribed from a separate screenshot, not synthesized from the receipt.
    act=r.get("campaign") or ""; rp=r.get("activity_price"); spec=r.get("selected_sku")
    if rp is not None and (not isinstance(rp,str) or not re.fullmatch(r"\d+(?:\.\d{1,2})?",rp)):raise ValueError("活动标价必须是对应页面直接显示的单个金额")
    spec_ok=(same_variant(spec,c["candidate_title"]) and sku(spec)==sku(f["selected"])==sku(c["expected_sku"])) if spec and f.get("selected") and sku(spec) else None
    same_campaign=(is_campaign(act,c["year"]) and bool(f["campaign"]) and ts.astimezone(dt.timezone(dt.timedelta(hours=8))).year==c["year"])
    checks={"规格":spec_ok,"图片":pixels if spec_ok else None,
            "活动价":Decimal(rp)==Decimal(f["price"]) if spec_ok and same_campaign and rp and f.get("price") else None,
            "优惠":({packed(x) for x in refquotes}=={packed(x) for x in f["benefits"]}) if spec_ok and same_campaign and refquotes and f.get("benefits") else None}
    result={"target":c["target"],"page":c["url"],"capture_at":c["at"],"reference_at":r["at"],"checks":checks,
            "passed":c.get("status")=="awaiting_independent_page" and all(v is True for v in checks.values()),
            "basis":"同商品编号、10分钟内独立页面截图和可见文字；主图为截图内指定区域与原图的像素比较",
            "capture_sha256":sha(cp),"reference_sha256":sha(ref_path),"screenshot_sha256":sha(screen)}
    out=Path(a.out).absolute();write(out,result);return result,out

def main():
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("collect")
    for k in ("run","candidate","url","variant","out"):p.add_argument("--"+k,required=True)
    p=sub.add_parser("compare")
    for k in ("capture","reference","out"):p.add_argument("--"+k,required=True)
    a=ap.parse_args();result,out=(collect if a.cmd=="collect" else compare)(a)
    print(json.dumps({"status":result.get("status"),"passed":result.get("passed"),"page_status":result.get("page_status"),"missing":result.get("missing"),"checks":result.get("checks"),"path":str(out)},ensure_ascii=False))
    return 0 if result.get("status")=="awaiting_independent_page" or result.get("passed") else 2
if __name__=="__main__":
    try:sys.exit(main())
    except (ValueError,KeyError,OSError,CaptureError) as ex:sys.exit(str(ex))

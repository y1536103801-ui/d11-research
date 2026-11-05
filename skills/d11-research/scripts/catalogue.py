"""Discover candidates in visible official catalogue cards. A card is a lead, never an offer."""
import argparse, datetime as dt, hashlib, json, re, sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin,urlsplit,urlencode
import capture
from channel import packed, ident, read, sku, write
from promo import document, h, link

def official(url):
    u=urlsplit(capture.public_url(url))
    if u.scheme!="https" or u.hostname!="www.jd.com" or not re.fullmatch(r"/(?:brand|chanpin|jiage|xinghao|hprm|xinkuan)/[\w-]+\.html",u.path) or u.query:
        capture.fail("not_catalogue","仅接受京东公开的品牌或商品目录页")

class Cards(HTMLParser):
    def __init__(self,base,platform="京东",stops=()):
        super().__init__(convert_charrefs=True);self.base=base;self.platform=platform;self.stops=set(stops);self.anchor=None;self.items=[]
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="a":
            self.anchor=None;link=urljoin(self.base,a.get("href") or "")
            try:target=ident(link)
            except (ValueError,capture.CaptureError):return
            if target and target[0]==self.platform and target not in self.stops and not (
                target[0] in ("淘宝","天猫") and any((ch,target[1]) in self.stops for ch in ("淘宝","天猫"))
            ):self.anchor=(link,(a.get("title") or "").strip())
        if tag=="img" and self.anchor:
            link,anchor_title=self.anchor;alt=(a.get("alt") or a.get("title") or "").strip()
            raw=(a.get("data-lazy-img") or a.get("data-src") or a.get("src") or "").strip()
            try:img=capture.public_url(urljoin(self.base,raw))
            except capture.CaptureError:return
            h=urlsplit(img).hostname or ""
            endings=(".360buyimg.com",".jdimg.com") if self.platform=="京东" else (".alicdn.com",".tbcdn.cn")
            if not h.endswith(endings) or "blank.gif" in img:return
            self.items.append((link,anchor_title or alt,alt,img))
    def handle_endtag(self,tag):
        if tag=="a":self.anchor=None

def select_cards(items,c,variant):
    if len(packed(variant))<2 or packed(variant) not in packed(c["title"]):raise ValueError("款型须来自已认候选")
    expected=sku(c["sku"])
    if not expected:raise ValueError("候选规格缺少容量或件数")
    out={}; rejected=set()
    for url,title,alt,img in items:
        if not all(packed(x) in packed(title) for x in (c["brand"],variant)):continue
        # The final part of the heading is the offered variant on many JD cards.
        tail=title.split()[-1];chosen=tail if re.search(r"\d+\s*(?:g|ml|克|毫升)",tail,re.I) and any(v in tail for v in ("泥","控油","洁面")) else title
        dims=re.findall(r"(\d+(?:\.\d+)?)(毫升|克|ml|g)",packed(chosen))
        itemid=ident(url)[1]
        quantities=re.findall(r"(\d+)(?:支|瓶|管|件)(?:装)?",packed(title))
        # A common title mentions all flavours. Do not match green when the selected suffix is red.
        if (packed(variant) not in packed(chosen) or len(dims)!=1 or
            re.search(r"[+＋]|另赠|买.{0,3}赠|组合装|双支|两支|两瓶|礼盒|任选",packed(title)) or
            any(int(q)!=expected[2] for q in quantities) or
            any(sku(f"{n}{u} 单支")[:2]!=expected[:2] for n,u in dims) or
            (bool(re.search(r"[*×x]\d+|\d+[*×x]|\d+(?:支|瓶|管|件)|单[支瓶管件]",packed(chosen))) and sku(chosen)!=expected)):
            rejected.add(itemid);continue
        if itemid not in out:out[itemid]={"item_id":itemid,"url":url,"title":title,"image_alt":alt,"image_url":img,
            "package_count_stated":sku(chosen)==expected,"image_role":"目录缩略图，仅用于找款"}
    return sorted(out.values(),key=lambda x:not x["package_count_stated"]),len(rejected-set(out))

def show_cards(receipt, dest):
    cards=receipt["candidates"]
    cn=dt.datetime.fromisoformat(receipt["at"]).astimezone(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M 北京时间")
    contents=[f'<p>{h(receipt["source"])} · {h(cn)} · {link(receipt["url"],"打开目录来源")}</p>',
        '<p>商品卡可辅助选款。详情页尚未核实的高清主图、双十一价格及优惠不列在这里。</p>']
    for c in cards:
        preview=c.get("preview"); img=""
        if preview:
            img=f'<p><img src="{h(preview["file"])}" alt="商品目录缩略图"> · {preview["width"]}×{preview["height"]} 目录图</p>'
        label="目录标明容量和件数" if c["package_count_stated"] else "目录只标明容量，件数待核"
        contents.append(f'<article><b>{h(c["title"])}</b>{img}<p>{label} · {link(c["url"],"打开商品详情")} · {h(c["item_id"])}</p></article>')
    if not cards:contents.append("<p>没有从这个公开入口取得匹配的商品卡。</p>")
    (dest/"候选商品卡.html").write_bytes(document("渠道候选商品卡","".join(contents)))

def main():
    p=argparse.ArgumentParser(description=__doc__)
    for k in ("run","candidate","variant","out"):p.add_argument("--"+k,required=True)
    p.add_argument("--platform",choices=("天猫","淘宝","京东"),default="京东")
    p.add_argument("--url",help="京东官方品牌/品类目录。淘宝、天猫使用候选名称构造各自的公开搜索入口")
    p.add_argument("--preview",type=int,choices=(0,1,2),default=1);a=p.parse_args()
    batch=read(Path(a.run)/"state.json");c=batch["candidates"].get(a.candidate)
    if a.platform not in batch["channels"] or not c or a.candidate not in batch["selections"]:raise ValueError("需使用已认款且包含目标渠道的批次")
    if a.platform=="京东":
        if not a.url:raise ValueError("京东需要一个已找到的官方目录网址")
        url=a.url;guard=official
    else:
        if a.url:raise ValueError("淘系按本批候选名称发起公开搜索，不接受任意中转网址")
        url="https://s.taobao.com/search?"+urlencode({"q":c["title"],**({"tab":"mall"} if a.platform=="天猫" else {})})
        def guard(u):
            if u!=url:
                h=urlsplit(u).hostname
                capture.fail("login_redirect" if h in {"login.taobao.com","login.tmall.com"} else "unexpected_redirect","停止非搜索页跳转")
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc)
    dest=out/("catalogue-"+now.strftime("%Y%m%dT%H%M%S%fZ"));dest.mkdir()
    r={"source":a.platform+"公开商品目录","page_type":"目录非商品详情","url":url,"at":now.isoformat(),"candidate":a.candidate,
       "candidates":[],"accepted_offer_fields":0,"comment":"目录线索不能当作所选规格、活动价、福利或渠道高清主图。"}
    guard(url);stop_file=Path(a.run)/"受限商品.json";stops=[tuple(x) for x in read(stop_file)["items"]] if stop_file.is_file() else []
    try:
        final,hop,_=capture.fetch(url,"page",before_hop=guard)
        r.update(http_status=hop.status,html_sha256=hashlib.sha256(hop.body).hexdigest(),url=final)
        if hop.headers.get_content_type()!="text/html":raise ValueError("目录未返回公开 HTML")
        meta,_=capture.analyze_page(final,hop)
        if meta["status"] not in {"ok","no_product_content"}:
            capture.fail(meta["status"],"未取得可用目录")
        html=capture.decode_html(hop.body,hop.headers); parser=Cards(final,a.platform,stops);parser.feed(html)
        items,rejected=select_cards(parser.items,c,a.variant)
        r.update(status="candidates" if items else "no_matching_cards",candidates=items,excluded_variants=rejected,page_title=meta.get("title"),linked_cards=len(parser.items))
        for item in items[:a.preview]:
            src=dest/("catalogue-image-link-"+item["item_id"]+".json")
            write(src,dict(kind="image_link",http_status=200,status="link_observed",evidence_ok=False,
                           image_url=item["image_url"],url=final,product_url=item["url"],source_excerpt=item["title"]))
            im=capture.capture_image(item["image_url"],str(dest),from_page=str(src))
            item["image_status"]=im["status"]
            if im["status"]=="ok":item["preview"]={"file":Path(im["path"]).name,"sha256":im["sha256"],"width":im["width"],"height":im["height"]}
    except (ValueError,capture.CaptureError) as exc:r.update(status=getattr(exc,"code","unreadable"))
    write(dest/"目录线索.json",r)
    show_cards(r,dest)
    print(json.dumps({"status":r["status"],"candidates":len(r["candidates"]),"receipt":str(dest/"目录线索.json"),"cards":str(dest/"候选商品卡.html")},ensure_ascii=False))
    return 0 if r["candidates"] else 2
if __name__=="__main__":
    try:sys.exit(main())
    except (ValueError,OSError,KeyError,capture.CaptureError) as ex:sys.exit(str(ex))

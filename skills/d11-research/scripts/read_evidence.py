"""Discover official channel entry points or read price/benefit wording from a local capture."""
import argparse, json, re, sys, tempfile, subprocess
from pathlib import Path
from urllib.parse import urlencode, quote
ROOT = Path(__file__).resolve().parent
PLATFORMS = {
    "天猫": lambda q: "https://s.taobao.com/search?" + urlencode({"q":q, "tab":"mall"}),
    "淘宝": lambda q: "https://s.taobao.com/search?" + urlencode({"q":q}),
    "京东": lambda q: "https://search.jd.com/Search?" + urlencode({"keyword":q}),
    "抖音": lambda q: "https://www.douyin.com/search/" + quote(q, safe=""),
}
CAMPAIGN = re.compile(r"双\s*(?:十\s*一|11)|(?<!\d)11[.·．]11(?!\d)")
PROMO = re.compile(r"双\s*(?:十一|11)|11[.·．]11|[券实到]后价|到手价|预计到手|付款金额|实付款|活动价|日常价|券|会员|满\s*\d|立减|直减|补贴|购物金|红包|加赠|满赠|赠品|直播专享|[¥￥]\s*\d|\d+(?:\.\d+)?\s*元")
RISK = re.compile(r"收货|配送至|联系人|手机号|收件人|支付账号|支付宝账号|验证码|订单号|订单编号|运单|收货地址|1[3-9]\d{9}|\S+@\S+\.\S+")
def read_ocr(path):
    with tempfile.TemporaryDirectory(prefix="d11-ocr-") as tmp:
        out=Path(tmp)/"text.json"
        p=subprocess.run(["/usr/bin/osascript","-l","JavaScript",str(ROOT/"ocr_batch.js"),str(out),str(path)],capture_output=True,timeout=45)
        if p.returncode or not out.is_file(): raise ValueError("本机 OCR 不可用，请用宿主读图")
        return json.loads(out.read_text())[0]["text"]
def parse(text):
    chunks=[p.strip() for p in re.split(r"[\r\n。；;|]+", text) if p.strip()]
    evidence=[]
    for p in chunks:
        if RISK.search(p):continue
        for m in PROMO.finditer(p):
            left=max(0,m.start()-24);right=min(len(p),m.end()+32)
            clip=p[left:right].strip()
            if clip and not any(clip in x for x in evidence):evidence.append(clip)
    return {"图片或正文含活动标识":bool(CAMPAIGN.search(text)), "可核对的优惠原文":evidence[:36],
            "说明":"这些是原文线索。价格归属、商品规格、活动时间和会员条件需结合来源核对。"}
def main():
    a=argparse.ArgumentParser()
    subs=a.add_subparsers(required=True,dest="cmd")
    q=subs.add_parser("find");q.add_argument("query");q.add_argument("--channels",nargs="+",choices=PLATFORMS,default=list(PLATFORMS))
    z=subs.add_parser("read");z.add_argument("file",type=Path);z.add_argument("--include-ocr",action="store_true")
    args=a.parse_args()
    if args.cmd=="find":o=[{"平台":p,"搜索词":args.query,"入口":PLATFORMS[p](args.query)} for p in args.channels]
    else:
        f=args.file.resolve(strict=True);is_image=f.suffix.lower() in {".png",".jpg",".jpeg",".webp",".heic",".tif",".tiff"}
        if is_image:t=read_ocr(f)
        elif f.suffix.lower()==".json":
            d=json.loads(f.read_text(encoding="utf-8"));t="\n".join(map(str,d.get("lines",[]))) if isinstance(d,dict) and "lines" in d else d.get("text","")
            if not isinstance(t,str) or not t:raise ValueError("JSON 没有可读取的页面正文")
        else:t=f.read_text(encoding="utf-8")
        o=parse(t)
        if args.include_ocr and is_image:o["OCR原文"]=t
    print(json.dumps(o,ensure_ascii=False,indent=2))
if __name__=="__main__":
    try:main()
    except (OSError,ValueError,subprocess.SubprocessError) as exc:sys.exit(str(exc))

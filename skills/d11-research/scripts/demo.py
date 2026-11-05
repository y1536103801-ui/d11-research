"""Offline acceptance sample. No website is contacted and no example is a merchant offer."""
import argparse
import datetime
import json
from pathlib import Path
import subprocess
import sys
from PIL import Image, ImageDraw

def main():
    p=argparse.ArgumentParser(description="用虚构候选图演示如何生成 Word；不访问平台")
    p.add_argument("--out", type=Path, help="新的或空的批次目录")
    a=p.parse_args()
    stamp=datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder=(a.out or (Path.cwd()/"工作区"/("离线演示-"+stamp))).expanduser().absolute()
    if folder.exists() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError("演示需要新的或空的目录，请另选 --out；不会改动已有批次。")
    def flow(*args):
        run=subprocess.run([sys.executable,"-B",str(Path(__file__).with_name("promo.py")),
                            "--run",str(folder),*map(str,args)],stdout=subprocess.PIPE,
                           encoding="utf-8",check=True)
        return json.loads(run.stdout)
    flow("start","--pilot")
    intake=folder/"演示输入"; intake.mkdir()
    image=Image.new("RGB",(720,720),"#F3F4EB");draw=ImageDraw.Draw(image)
    draw.rounded_rectangle((235,110,485,554),radius=25,fill="#416659")
    draw.rectangle((235,495,485,554),fill="#1C3C38")
    draw.rounded_rectangle((256,190,464,405),radius=15,fill="#E7F1DB")
    draw.text((283,235),"DEMO / CLEANSER",fill="#233C31",font_size=19)
    draw.text((305,302),"120 g",fill="#233C31",font_size=34)
    draw.text((168,629),"OFFLINE SAMPLE - NOT A REAL PRODUCT",fill="#424B49",font_size=20)
    photo=intake/"演示图.png";image.save(photo)
    candidate={"product":"控油洁面","role":"own","brand":"演示品牌","title":"离线演示用控油洁面（虚构）",
               "sku":"120g 单支","platform":"品牌官网","shop":"虚构示例","url":"https://example.org/d11-demo",
               "basis":"离线演示，没有访问商品页面，不代表任何在售产品或价格。",
               "image":photo.relative_to(folder).as_posix()}
    manifest=intake/"候选.json"
    manifest.write_text(json.dumps([candidate],ensure_ascii=False),encoding="utf-8")
    cid=flow("candidates",manifest)["ids"][0]
    flow("select",cid,"--trial");flow("gap","此批只有虚构演示图，没有任何经核实的渠道价格或福利；请另建批次开始实际调研。")
    result=flow("export")
    print(json.dumps({"说明":"离线演示，无真实平台数据。",**result},ensure_ascii=False,indent=2))

if __name__=="__main__":
    try: main()
    except (OSError,ValueError,subprocess.CalledProcessError) as exc: sys.exit(str(exc))

"""Smoke test installed copies on a Mac; never uses the user's ecommerce accounts."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
from xml.etree import ElementTree as ET

ROOT=Path(__file__).resolve().parents[1]

def cli(skill,*args):
    return subprocess.check_output(["/bin/bash",str(skill/"scripts"/"run"),*map(str,args)],text=True)

def check(condition, why):
    if not condition: raise RuntimeError(why)

def main():
    opts=argparse.ArgumentParser()
    opts.add_argument("--installed-user",action="store_true",help="test the default user installations, e.g. on disposable CI")
    args=opts.parse_args()
    with tempfile.TemporaryDirectory(prefix="d11-mac-smoke-") as td:
        tmp=Path(td).resolve()
        if args.installed_user:
            root=Path.home();dest=[root/".agents",root/".workbuddy"]
        else:
            root=tmp/"带 空格的项目";dest=[root/".agents",root/".codebuddy"]
            install=["/bin/bash",str(ROOT/"install-mac.command"),"--project",str(root)]
            subprocess.run(install,check=True)
            again=subprocess.check_output(install,text=True)
            check(again.count("已是相同版本")==2,"同版本安装应该跳过两份文件")
            check(not list(root.glob("*/d11-skill-backups/*")),"同版本不应产生旧版备份")
        hosts=[p/"skills"/"d11-research" for p in dest]
        for folder in hosts:
            check((folder/"SKILL.md").is_file() and "运行环境可用" in cli(folder,"doctor"), "客户端入口无法找到技能或运行依赖")
        # Two separately installed copies must be able to continue the same real empty batch.
        batch=tmp/"自己的批次";cli(hosts[0],"flow","--run",batch,"start","--pilot")
        status=json.loads(cli(hosts[1],"flow","--run",batch,"status"))
        check(status["mode"]=="pilot" and status["candidates"]==0,"另一客户端读到的不是同一空批次")
        demo=tmp/"离线演示";out=json.loads(cli(hosts[1],"demo","--out",demo))
        check(out["mode"]=="pilot" and out["formal_images"]==0 and out["report_entries"]==0,"虚构数据进入了正式证据")
        dest=Path(out["export_dir"])
        check(all((dest/p).exists() for p in ("候选清单.html","证据表.html","待补.txt","双十一","产品图（更新）")), "缺少预期交付文件")
        with zipfile.ZipFile(out["report"]) as doc:
            doc_text="".join(ET.fromstring(doc.read("word/document.xml")).itertext())
            check("演示" in doc_text and "试跑" in doc_text and any(n.startswith("word/media/") for n in doc.namelist()),"Word 缺少试跑说明或演示配图")
        refuse=subprocess.run(["/bin/bash",str(hosts[0]/"scripts"/"run"),"demo","--out",str(demo)],capture_output=True)
        check(refuse.returncode!=0,"不应覆盖已有工作批次")
        print("通过：两套 Mac 安装入口、跨客户端续跑、带图 Word、证据表和空正式图片目录；无平台请求。")
if __name__=="__main__":main()

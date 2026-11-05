"""Install this skill on a Mac without replacing unrelated tools or job data."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parent
SKILL = ROOT / "skills" / "d11-research"
DATA = Path(os.environ.get("D11_DATA_DIR", Path.home() / "Library" / "Application Support" / "d11-research")).expanduser().absolute()
CHECK = ('import PIL,docx; assert (10,4) <= tuple(map(int,PIL.__version__.split(".")[:2])) < (14,); '
         'assert (1,1,2) <= tuple(map(int,docx.__version__.split(".")[:3])) < (2,)')

def ready(p):
    return p.is_file() and subprocess.run([str(p), "-I", "-B", "-c", CHECK],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

def runtime():
    DATA.mkdir(parents=True, exist_ok=True)
    env = DATA / "venv"
    py = Path(sys.executable).absolute()
    if not ready(py):
        py = env / "bin" / "python3"
        if not py.is_file():
            subprocess.run([sys.executable, "-m", "venv", str(env)], check=True)
        if not ready(py):
            print("首次安装图片、Word 依赖…", flush=True)
            subprocess.run([str(py), "-m", "pip", "--disable-pip-version-check", "install",
                            "--only-binary=:all:", "-r", str(SKILL/"requirements.txt")], check=True)
    if not ready(py):
        raise RuntimeError("Python 环境仍缺少 Pillow 或 python-docx。")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=DATA, delete=False) as f:
        f.write(str(py) + "\n")
    os.replace(f.name, DATA / "python-path")
    return py

def contents(folder):
    result = {}
    for path in folder.rglob("*"):
        if any(x in ("__pycache__", ".DS_Store") for x in path.parts) or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            return None
        if path.is_file():
            result[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result

def deploy(dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or (dst.exists() and not dst.is_dir()):
        raise ValueError(f"安装位置已被链接或普通文件占用，请用 --project 安装到新文件夹：{dst}")
    if dst.is_dir() and contents(dst) == contents(SKILL) and os.access(dst/"scripts"/"run", os.X_OK):
        print(f"已是相同版本：{dst}")
        return
    with tempfile.TemporaryDirectory(prefix=".d11-stage-", dir=dst.parent) as tmp:
        staged = Path(tmp)/SKILL.name
        shutil.copytree(SKILL, staged, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        # A ZIP extractor need not preserve executable bits.
        (staged/"scripts"/"run").chmod(0o755)
        backup = None
        if dst.exists():
            old = dst.parent.parent / "d11-skill-backups"
            old.mkdir(exist_ok=True)
            backup = old / ("d11-research-" + uuid.uuid4().hex[:10])
            dst.rename(backup)
        try:
            staged.rename(dst)
        except BaseException:
            if backup is not None: backup.rename(dst)
            raise
        if backup: print(f"旧版已备份：{backup}")
    print(f"已安装：{dst}")

def main():
    p = argparse.ArgumentParser(description="为 Mac 上的 Codex / WorkBuddy 安装男护大促调研助手")
    p.add_argument("--host", choices=("both", "codex", "workbuddy"), default="both")
    p.add_argument("--project", type=Path, help="仅安装到指定项目；默认当前 macOS 用户的技能目录")
    a=p.parse_args()
    if sys.platform != "darwin" or sys.version_info < (3,10):
        raise ValueError("需要 macOS 与 Python 3.10+，请用 bash install-mac.command 安装。")
    py=runtime()
    base=a.project.expanduser().resolve() if a.project else Path.home()
    targets={"codex":base/".agents"/"skills"/SKILL.name,
             "workbuddy":base/(".codebuddy" if a.project else ".workbuddy")/"skills"/SKILL.name}
    for host,dst in targets.items():
        if a.host not in ("both",host): continue
        deploy(dst)
        subprocess.run(["/bin/bash", str(dst/"scripts"/"run"), "doctor"],
                       env={**os.environ,"D11_PYTHON":str(py)}, check=True)
    print("安装完成。打开新对话，发：用 d11-research 从零调研男士控油洁面，先给我看候选商品和图片。")
if __name__=="__main__":
    try: main()
    except (ValueError,RuntimeError,OSError,subprocess.CalledProcessError) as e: sys.exit(str(e))

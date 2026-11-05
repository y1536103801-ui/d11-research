#!/usr/bin/env python3
"""双十一证据批次 CLI。JSON 输入/输出均为 UTF-8；python promo.py schema 查看契约。"""
import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import html
import importlib.util
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit
import warnings

PRODUCTS = ["冰川保湿抗皱", "冰川露", "喷喷乳", "控油洁面"]
CHANNELS = ["抖音", "天猫", "淘宝", "京东"]
BRANDS = ["欧莱雅", "海洋至尊", "碧欧泉", "百雀羚", "朗仕", "韩束", "理肤泉"]
DOMAIN = {"抖音": ("douyin.com", "jinritemai.com", "douyinec.com"),
          "天猫": ("tmall.com", "tmall.hk"), "淘宝": ("taobao.com",),
          "京东": ("jd.com", "jd.hk", "jdhealth.com")}
OFFICIAL = "品牌官网"
PLATFORM_ALIASES = {"官网": OFFICIAL, "官方网站": OFFICIAL, "品牌官方网站": OFFICIAL,
                    "抖音商城": "抖音", "京东商城": "京东"}
KINDS = ("campaign", "current", "offer", "rule")
SCOPES = ("product_page", "brand_page", "rule_page", "checkout", "live")
LABELS = {"campaign": "双十一", "current": "产品图（更新）", "offer": "价格与福利", "rule": "平台规则"}
CAND_KEYS = "product role brand title sku url platform shop basis".split()
OBS_KEYS = "candidate_id platform shop kind scope observed_at url excerpt assets fields original".split()
ARRAY_ROLES = ("original_assets", "screenshot_assets")
CN = dt.timezone(dt.timedelta(hours=8))
STATE = "state.json"
BLOBS = "留证"
MAX_JSON = 16 * 1024 * 1024
MAX_ASSET = 32 * 1024 * 1024
MAX_PIXELS = 40_000_000
MIN_SIDE = 600
FORMAT = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "AVIF": ".avif", "BMP": ".bmp"}
IMG_EXT = set(FORMAT.values()) | {".jpeg", ".jfif", ".tif", ".tiff", ".heic"}
TAG = re.compile(r"双\s*(?:十[一壹]|11)|(?:11\s*[.·．-]\s*11)|双壹拾壹", re.I)
HASHFILE = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|gif|webp|avif|bmp|pdf|txt|bin)\Z")
YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
PRICES = {"日常价": ("日常价", "日常价格", "日常标价"),
          "活动标价": ("活动标价", "双十一标价", "双十一活动标价"),
          "宣传到手价": ("宣传到手价", "宣传券后价", "宣传会员价", "宣传叠券价", "宣传预计到手价"),
          "结算实见价": ("结算实见价", "结算页实见价", "结算页实付价")}
PRICE_ALIAS = {alias: key for key, aliases in PRICES.items() for alias in aliases}
AMBIG_PRICE = re.compile(r"价|到手|实付|支付金额|结算金额|券后金额|成交金额|折后金额")
DESCRIPTIVE = {"价格型福利", "非价格型福利", "价格依据", "日常价依据", "比价规格", "保价规则", "价格口径说明"}
UNKNOWN = {"", "无", "否", "不明", "未知", "待核", "待补", "未核实", "不详", "无依据", "不确定", "-", "n/a", "none", "null"}
STATE_VERSION = 1


class Invalid(ValueError):
    pass


def require(condition, msg):
    if not condition:
        raise Invalid(msg)


def dump(obj, pretty=False):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      indent=2 if pretty else None, separators=None if pretty else (",", ":"))


def unique_object(pairs):
    obj = {}
    for k, v in pairs:
        require(k not in obj, f"JSON 键重复：{k}")
        obj[k] = v
    return obj


def read_json(path, limit=MAX_JSON):
    require(path.is_file(), f"缺少普通 JSON 文件：{path}")
    with path.open("rb") as src:
        require(stat.S_ISREG(os.fstat(src.fileno()).st_mode), "JSON 必须是普通文件")
        b = src.read(limit + 1)
    require(len(b) <= limit, "JSON 文件超过大小限制")
    return json.loads(b.decode("utf-8-sig"), object_pairs_hook=unique_object,
                      parse_constant=lambda _: (_ for _ in ()).throw(Invalid("无效 JSON 数值")))


def identity(prefix, obj):
    return prefix + "-" + hashlib.sha256(dump(obj).encode()).hexdigest()[:24]


def now():
    return dt.datetime.now(CN).isoformat(timespec="seconds")


def string(x, key, *, limit=12000):
    require(isinstance(x, str), f"{key} 必须是字符串")
    x = unicodedata.normalize("NFC", x.strip())
    require(0 < len(x) <= limit and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", x), f"{key} 为空、过长或含控制字符")
    require(not re.search(r"[\ud800-\udfff]", x), f"{key} 含无效 Unicode 字符")
    return x


def stamp(x):
    x = string(x, "observed_at", limit=80)
    require(re.match(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}", x), "observed_at 需要日期、时间及明确时区")
    try:
        t = dt.datetime.fromisoformat(x.replace("Z", "+00:00").replace("z", "+00:00"))
        require(t.utcoffset() is not None, "observed_at 缺少时区")
        require(t <= dt.datetime.now(CN) + dt.timedelta(minutes=5), "观察时间不可在未来")
        return t.astimezone(CN).isoformat()
    except Invalid:
        raise
    except (ValueError, OverflowError):
        raise Invalid("observed_at 需要 ISO 8601 日期时间及明确时区，如 2026-11-05T15:30:00+08:00") from None


def enum(x, allowed, key):
    require(x in allowed, f"{key} 必须是 {' / '.join(allowed)}")
    return x


def website(x):
    x = string(x, "url", limit=4096)
    require(not re.search(r"[\s\\]", x), "URL 含空白或反斜杠")
    u = urlsplit(x)
    host = (u.hostname or "").lower()
    require(u.scheme in ("http", "https") and u.username is None and u.password is None and
            u.port in (None, 80, 443) and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
            and "." in host and ".." not in host, "必须使用无凭据的公开 HTTP(S) 来源 URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise Invalid("来源请使用公开站点域名")
    require(not any(re.search(r"token|cookie|password|signature|session|credential|^sign$|auth[_-]?key|^code$", k, re.I)
                    for k, _ in parse_qsl(u.query, keep_blank_values=True)), "来源 URL 含授权参数，请提供公开链接")
    port = "" if u.port in (None, {"http": 80, "https": 443}[u.scheme]) else f":{u.port}"
    return urlunsplit((u.scheme, host + port, u.path or "/", u.query, "")), host


def platform_url(row, s):
    name = string(row["platform"], "platform", limit=64)
    name = PLATFORM_ALIASES.get(name, name)
    enum(name, [*s["channels"], OFFICIAL], "platform")
    url, host = website(row["url"])
    actual = next((p for p, suffixes in DOMAIN.items() if any(host == d or host.endswith("." + d) for d in suffixes)), None)
    require((actual or OFFICIAL) == name, "platform 与来源域名不符；官网只作线索，请另附对应渠道页面")
    return name, url


def abs_path(value):
    p = Path(value).expanduser()
    require(".." not in p.parts, "路径不允许包含 ..")
    p = p.absolute()
    # macOS 自带 /tmp → /private/tmp 和 /var → /private/var；不跟随用户目录中的符号链接。
    if sys.platform == "darwin" and len(p.parts) > 1 and p.parts[1] in ("tmp", "var"):
        a = Path("/") / p.parts[1]
        if a.is_symlink() and a.resolve() == Path("/private") / p.parts[1]:
            p = a.resolve().joinpath(*p.parts[2:])
    return p


def no_links(p):
    for d in (p, *p.parents):
        require(not d.is_symlink(), f"不接受符号链接：{d}")
    return p


def atomic(p, b):
    no_links(p)
    fd, name = tempfile.mkstemp(prefix=".promo-write-", dir=p.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(b)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, p)
    finally:
        Path(name).unlink(missing_ok=True)


def save_json(p, obj):
    atomic(p, (dump(obj, True) + "\n").encode())


def scope_config(pilot):
    return (["控油洁面"], ["天猫", "抖音"], ["欧莱雅"]) if pilot else (PRODUCTS[:], CHANNELS[:], BRANDS[:])


@contextlib.contextmanager
def locked(root, create=False):
    no_links(root)
    require(root != Path(root.anchor), "不能使用文件系统根目录作为批次")
    require(not any((p / STATE).exists() and (p / ".promo.lock").exists() for p in root.parents),
            "不同批次请使用并列目录，不可嵌套")
    if create:
        root.mkdir(parents=True, exist_ok=True)
    require(root.is_dir(), "批次目录不存在；请先 start")
    st_path = no_links(root / STATE)
    require(create or st_path.is_file(), "批次未初始化；请先 start")
    lock = no_links(root / ".promo.lock")
    if not lock.exists():
        require(all(p.name == lock.name for p in root.iterdir()), "首次 start 需要新目录或空目录")
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    with os.fdopen(os.open(lock, flags, 0o600), "r+b") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        require(stat.S_ISREG(os.fstat(f.fileno()).st_mode), "批次锁无效")
        if st_path.exists():
            s = read_json(st_path, 256 * 1024 * 1024)
            require(isinstance(s, dict) and s.get("schema_version") == STATE_VERSION and
                    s.get("mode") in ("pilot", "formal"), "不支持的批次状态文件")
            cfg = scope_config(s["mode"] == "pilot")
            require(tuple(s.get(k) for k in ("products", "channels", "competitors")) == cfg, "批次范围与冻结接口不一致")
        else:
            require(create and all(p.name == lock.name for p in root.iterdir()), "首次 start 需要空目录")
            s = None
        yield s


def rows(file):
    data = read_json(Path(file).expanduser())
    require(isinstance(data, list) and all(isinstance(v, dict) for v in data), "输入必须是 JSON 对象列表")
    return data


def check_keys(row, mandatory, extra=()):
    missing = set(mandatory) - row.keys()
    require(not missing, "缺少字段：" + "、".join(sorted(missing)))
    bad = row.keys() - set(mandatory) - set(extra)
    require(not bad, "未知字段：" + "、".join(sorted(bad)))


def relative_input(root, val):
    p = Path(string(val, "附件路径", limit=4096))
    require(".." not in p.parts, "附件路径不能包含 ..")
    p = abs_path(p) if p.is_absolute() else root / p
    require(p.is_relative_to(root) and p != root, "附件必须位于当前批次目录内")
    no_links(p)
    rel = p.relative_to(root)
    require(rel.parts[0] not in {STATE, BLOBS, "exports", "候选清单.html", "最近导出.json"} and
            not any(x.startswith(".") for x in rel.parts), "请使用批次内独立的输入路径，例如 inputs/页.png")
    require(p.is_file(), f"附件不是本批次普通文件：{rel}")
    return p


def inspect_asset(b, suffix):
    looks_image = (b.startswith((b"\x89PNG", b"\xff\xd8", b"GIF8", b"BM")) or b[:4] == b"RIFF" and b[8:12] == b"WEBP"
                   or b[4:8] == b"ftyp" and b[8:12] in (b"avif", b"avis"))
    if looks_image or suffix in IMG_EXT:
        try:
            from PIL import Image
        except ImportError:
            raise Invalid("读取图片需要 Pillow；请使用已装 Pillow 的 Python") from None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(b)) as im:
                    fmt, (w, h) = im.format, im.size
                    require(fmt in FORMAT and 0 < w * h <= MAX_PIXELS, "图片格式或像素数不支持")
                    im.verify()
                with Image.open(io.BytesIO(b)) as im:
                    im.load()
            return FORMAT[fmt], {"type": "image", "width": w, "height": h}
        except Invalid:
            raise
        except Exception:
            raise Invalid("图片损坏或格式不支持") from None
    if b.startswith(b"%PDF-"):
        return ".pdf", {"type": "document"}
    if suffix in {".txt", ".md", ".html", ".htm", ".json", ".csv", ".log"}:
        try:
            b.decode("utf-8-sig")
        except UnicodeError:
            raise Invalid("文字附件不是 UTF-8") from None
        return ".txt", {"type": "text"}
    return ".bin", {"type": "file"}


class Intake:
    """先暂存整份清单，通过后才发布新附件及状态；相同内容只留一份。"""
    def __init__(self, root, s, stage):
        self.root, self.s, self.stage, self.cache = root, s, Path(stage), {}

    def asset(self, value, image_only=False):
        p = relative_input(self.root, value)
        if p not in self.cache:
            with p.open("rb") as f:
                require(stat.S_ISREG(os.fstat(f.fileno()).st_mode), "附件必须是普通文件")
                b = f.read(MAX_ASSET + 1)
            require(0 < len(b) <= MAX_ASSET, "附件为空或超过 32 MiB")
            suffix, meta = inspect_asset(b, p.suffix.lower())
            key = hashlib.sha256(b).hexdigest() + suffix
            self.s["blobs"][key] = dict(meta, bytes=len(b))
            target = no_links(self.root / BLOBS / key)
            if target.exists():
                verify_blob(self.root, key, len(b))
            elif not (self.stage / key).exists():
                (self.stage / key).write_bytes(b)
            self.cache[p] = key
        key = self.cache[p]
        require(not image_only or self.s["blobs"][key]["type"] == "image", "此处需要可解码的本地图片")
        return key

    def publish(self):
        target = no_links(self.root / BLOBS)
        target.mkdir(exist_ok=True)
        for p in self.stage.iterdir():
            os.replace(p, no_links(target / p.name))


def verify_blob(root, key, size):
    require(isinstance(key, str) and HASHFILE.fullmatch(key), "无效的批次附件标识")
    file = no_links(root / BLOBS / key)
    require(file.is_file() and file.stat().st_size == size <= MAX_ASSET and
            hashlib.sha256(file.read_bytes()).hexdigest() == key.split(".")[0],
            f"批次附件缺失或内容已变更：{key}")
    return file


def verified_assets(root, s):
    keys = {c["image"] for c in s["candidates"].values() if c["image"]}
    keys.update(k for o in s["observations"].values() for k in o["assets"])
    return {k: verify_blob(root, k, s["blobs"][k]["bytes"]) for k in keys}


def candidate(row, s, intake):
    check_keys(row, CAND_KEYS, ("image",))
    c = {k: string(row[k], k) for k in CAND_KEYS}
    enum(c["product"], s["products"], "product")
    enum(c["role"], ["own", "competitor"], "role")
    if c["role"] == "competitor":
        if c["brand"] in ("欧莱雅男士", "巴黎欧莱雅", "巴黎欧莱雅男士"):
            c["brand"] = "欧莱雅"
        enum(c["brand"], s["competitors"], "竞品 brand")
    c["platform"], c["url"] = platform_url(row, s)
    cid = identity("C", [c[k] for k in ("product", "role", "brand", "sku", "platform", "shop", "url")])
    old = s["candidates"].get(cid, {})
    c["image"] = intake.asset(row["image"], True) if row.get("image") not in (None, "") else old.get("image")
    s["candidates"][cid] = c
    return cid


def field_map(value):
    require(isinstance(value, dict), "fields 必须为中文键到字符串的 JSON 对象")
    fields = {}
    for rawkey, val in value.items():
        k = string(rawkey, "fields 键", limit=80)
        require(re.search(r"[\u3400-\u9fff]", k), f"fields 键请使用中文：{k}")
        require(k not in ("认款状态", "证据范围", "来源范围", "归档状态", "价格口径"), f"{k} 由 CLI 写入，请勿作为输入")
        if k in PRICE_ALIAS:
            k = PRICE_ALIAS[k]
        elif k not in DESCRIPTIVE and AMBIG_PRICE.search(k):
            raise Invalid(f"价格字段「{k}」口径不明确；请用 {' / '.join(PRICES)}，门槛和福利另列")
        require(k not in fields, f"重复的 fields 键/价格口径：{k}")
        fields[k] = string(val, k)
    return fields


def listed(x, name):
    require(isinstance(x, list) and len(x) <= 128 and all(isinstance(k, str) for k in x),
            f"{name} 必须是本批次本地文件路径列表（至多 128 项）")
    return x


def observation(row, s, intake):
    check_keys(row, OBS_KEYS, ARRAY_ROLES)
    cid = string(row["candidate_id"], "candidate_id")
    require(cid in s["candidates"], "candidate_id 不存在于本批次")
    platform, url = platform_url(row, s)
    kind = enum(string(row["kind"], "kind"), KINDS, "kind")
    scope = enum(string(row["scope"], "scope"), SCOPES, "scope")
    require(type(row["original"]) is bool, "original 必须是 JSON 布尔值")
    fields = field_map(row["fields"])
    price = [k for k in PRICES if k in fields]
    require(len(price) <= 1, "一条证据只记录一种价格口径；同一页面可拆成多条")
    if kind == "offer":
        require(platform in s["channels"] and scope in ("product_page", "checkout", "live"),
                "优惠需要本批次渠道的商品、直播或结算页；官网不能作为渠道实价")
    if kind == "rule":
        require(platform in s["channels"] and scope in ("rule_page", "product_page", "live"),
                "平台规则需要本渠道规则、商品说明或直播页")
    if price:
        key = price[0]
        require(kind == "offer", "价格只允许在 offer 中记录")
        if key == "日常价":
            require(scope == "product_page", "日常价必须来自同规格日常商品页")
        elif key == "结算实见价":
            require(scope == "checkout", "结算实见价只能来自 checkout")
        else:
            require(scope in ("product_page", "live"), "宣传价与活动标价不能作为结算页实见价")
    assets = sorted({intake.asset(v) for v in listed(row["assets"], "assets")})
    o = dict(candidate_id=cid, platform=platform, url=url, shop=string(row["shop"], "shop"),
             kind=kind, scope=scope, observed_at=stamp(row["observed_at"]), excerpt=string(row["excerpt"], "excerpt"),
             fields=fields, original=row["original"], assets=assets)
    for k in ARRAY_ROLES:
        o[k] = sorted({intake.asset(v, True) for v in listed(row.get(k, []), k)})
        require(set(o[k]) <= set(assets), f"{k} 必须是 assets 中的图片")
    require(not (set(o["original_assets"]) & set(o["screenshot_assets"])), "原图与页面截图必须是不同内容")
    require(not o["original_assets"] or o["original"] and kind in ("current", "campaign"),
            "original_assets 只用于 original=true 的 campaign/current 原图证据")
    eid = identity("E", o)
    s["observations"][eid] = o
    return eid


def known(x):
    return bool(x and x.strip().lower() not in UNKNOWN and not re.fullmatch(r"(?:待|尚未|未)(?:核对|核实|确认|核验|补充|知)(?:规格|版本|来源)?", x.strip()))


def visible_marker(v):
    negative = r"(?:无|没有|不含|不带|并非|非|未(?:见|写|标注|注明|显示)).{0,8}(?:双\s*(?:十[一壹]|11)|11\s*[.·．-]\s*11)"
    return bool(TAG.search(v) and not re.search(negative, v))


def version_basis(v):
    if not known(v) or v in {"当前", "新版", "当前版本", "是", "有"}:
        return False
    # 无活动字样与当前版本是两项独立事实，后者须有正面的在售/更新依据。
    weak = r"推测|猜测|猜想|推断|仅凭|可能是|无法确认|未能确认|待核|不能确认|不确定|因(?:为)?.{0,12}(?:没有|无|不含|未写).{0,8}(?:双十|双11)"
    positive = r"(?:在售|现售|当前(?:包装|主图|展示|版本|商品页)|更新(?:至|为|于|日期|时间)|上架|替换|改版|新包装|版本号|主图第|第.{1,4}张主图|详情页.{0,12}(?:主图|首图|展示|版本)|[vV]\d)"
    return not re.search(weak, v) and bool(re.search(positive, v)) and not (
        TAG.search(v) and re.search(r"(?:没有|不含|未写|无).{0,6}(?:双十|双11)", v) and
        not re.search(r"在售|现售|详情页|版本号|更新于|上架", v))


def daily_basis(v):
    return (known(v) and re.search(r"日常|常规|平日|非活动|非促销|非大促|日销|非双十一", v)
            and not re.search(r"划线价|吊牌价|建议零售价|宣传价|券后价|标称原价", v))


def this_campaign(fields, s):
    v = fields.get("活动期", "")
    return set(YEAR.findall(v)) == {str(s["year"])} and visible_marker(v)


def evidence(s):
    """只根据操作者标注的来源事实分类；不从 URL、图片文件名或价差推断活动/价格。"""
    results = {}
    used = {}
    screens = {k for eid, o in s["observations"].items() if eid not in s["withdrawn"] for k in o["screenshot_assets"]}
    for eid, o in s["observations"].items():
        if eid in s["withdrawn"]:
            results[eid] = ["已撤销：" + s["withdrawn"][eid]["reason"]]
            continue
        c = s["candidates"][o["candidate_id"]]
        sel = s["selections"].get(o["candidate_id"])
        f, kind, scope = o["fields"], o["kind"], o["scope"]
        issues = []
        def need(ok, why):
            if not ok:
                issues.append(why)
        need(sel and sel["status"] == ("trial" if s["mode"] == "pilot" else "confirmed"),
             "未试跑认款" if s["mode"] == "pilot" else "未经用户认款")
        need(o["platform"] in s["channels"], "官网仅作认款线索")
        need(int(o["observed_at"][:4]) == s["year"], "观察年份与批次不符")
        u = urlsplit(o["url"])
        barrier = (re.search(r"(?:^|[./_-])(?:login|passport|captcha|verify|search|redirect)(?:[./_-]|$)",
                             (u.hostname + u.path).lower()) or u.hostname in {"v.douyin.com", "s.taobao.com", "s.m.taobao.com",
                             "s.click.taobao.com", "u.jd.com", "3.cn", "m.tb.cn"} or u.path in ("", "/") and not u.query)
        need(not barrier, "需要展开后的实际来源页，不能用搜索、站点首页、中转或登录页")
        need(known(o["excerpt"]), "缺页面可读摘录")
        if kind != "rule":
            need(known(c["sku"]) and f.get("页面规格") == c["sku"], "页面规格须与所选候选的 sku 一致")
        if kind in ("campaign", "current"):
            need(c["role"] == "own", "竞品图片仅留证，不进本品原图目录")
            need(scope == "product_page", "缺本渠道商品详情页证据")
            need(o["original"] and o["original_assets"], "缺显式标注的原图 original_assets")
            need(o["screenshot_assets"], "缺独立商品页截图 screenshot_assets")
            oversized = [k for k in o["original_assets"] if min(s["blobs"][k]["width"], s["blobs"][k]["height"]) < MIN_SIDE]
            need(not oversized, f"原图短边不足 {MIN_SIDE}px")
            need(not (set(o["original_assets"]) & screens), "同一文件也被标为截图，不能当原图")
            if kind == "campaign":
                need(this_campaign(f, s), f"活动期须指明 {s['year']} 年双十一")
                mark = f.get("图中文字", "")
                need(visible_marker(mark) and f.get("双十一字样", "有") != "无",
                     "需逐字记录原图上的双十一标识（图中文字）")
            else:
                need(f.get("双十一字样") == "无" and not TAG.search(f.get("图中文字", "")), "需核对原图无双十一字样")
                version = f.get("版本依据", "")
                need(version_basis(version), "需可定位的在售包装/版本或更新依据，仅未写双十一或推测不足")
            if not issues:
                for key in o["original_assets"]:
                    used.setdefault(key, {}).setdefault(kind, []).append(eid)
        elif kind == "offer":
            need(o["screenshot_assets"], "优惠/价格缺对应页面截图 screenshot_assets")
            for price in PRICES.keys() & f.keys():
                need(known(f[price]) and re.search(r"\d|[零一二三四五六七八九十百千万两]+(?:元|角|分)", f[price]),
                     "所选价格口径须有明确的页面金额")
            if "日常价" in f:
                need(daily_basis(f.get("日常价依据")), "缺同规格非活动日常价记录；划线价、吊牌价不能代替")
            else:
                need(this_campaign(f, s), f"活动期须指明 {s['year']} 年双十一")
            if "结算实见价" in f:
                need(known(f.get("结算条件")), "缺结算条件（规格、件数、账户/券适用情况）")
            need(any(k in PRICES or k not in {"页面规格", "活动期", "日常价依据", "结算条件", "双十一字样", "版本依据", "图中文字"}
                     for k in f), "缺价格或逐条福利字段")
        else:
            need(scope in {"rule_page", "product_page", "live"}, "缺本平台规则来源")
            need(o["screenshot_assets"] or any(s["blobs"][k]["type"] in {"document", "text"} for k in o["assets"]),
                 "规则缺原文附件或标注的截图")
            need(this_campaign(f, s), f"活动期须指明 {s['year']} 年双十一")
        results[eid] = issues
    for kinds in used.values():
        if len(kinds) > 1:
            for ids in kinds.values():
                for eid in ids:
                    reason = "同一原图同时标为活动图与当前图，须核对后再归档"
                    if reason not in results[eid]:
                        results[eid].append(reason)
    return results


def gap_list(s, issues):
    gaps = list(s["gaps"])
    obs = s["observations"]
    good = {(s["candidates"][o["candidate_id"]]["product"], s["candidates"][o["candidate_id"]]["role"],
             s["candidates"][o["candidate_id"]]["brand"], o["platform"], o["kind"])
            for eid, o in obs.items() if not issues[eid]}
    prices = {}
    for eid, o in obs.items():
        c = s["candidates"][o["candidate_id"]]
        if not issues[eid] and o["kind"] == "offer" and c["role"] == "own":
            prices.setdefault((c["product"], o["platform"]), set()).update(PRICES.keys() & o["fields"].keys())
    selected = [s["candidates"][k] for k in s["selections"]]
    for product in s["products"]:
        own = [c for c in selected if c["role"] == "own" and c["product"] == product]
        if not own:
            gaps.append(f"{product}：尚未选定本品规格")
        else:
            for ch in s["channels"]:
                missing = [LABELS[k] for k in ("campaign", "current", "offer")
                           if (product, "own", own[0]["brand"], ch, k) not in good]
                if missing:
                    gaps.append(f"{product} / {ch}：待补" + "、".join(missing))
                if (product, ch) in prices:
                    absent = [k for k in PRICES if k not in prices[product, ch]]
                    if absent:
                        gaps.append(f"{product} / {ch}：以下价格尚无单独依据：" + "、".join(absent))
        for ch in s["channels"]:
            missing = [brand for brand in s["competitors"] if (product, "competitor", brand, ch, "offer") not in good]
            if missing:
                gaps.append(f"{product} / {ch}：竞品同规格福利待核：" + "、".join(missing))
    for ch in s["channels"]:
        if not any(k[3:] == (ch, "rule") for k in good):
            gaps.append(f"{ch}：缺本期平台规则依据")
    gaps.extend(f"{eid}：{'；'.join(why)}" for eid, why in issues.items() if why and eid not in s["withdrawn"])
    return list(dict.fromkeys(gaps))


def status(s, issues=None):
    issues = evidence(s) if issues is None else issues
    accepted = [o for eid, o in s["observations"].items() if not issues[eid]]
    pics = {(s["candidates"][o["candidate_id"]]["product"], o["kind"], k) for o in accepted
            if o["kind"] in ("campaign", "current") for k in o["original_assets"]}
    pilot = s["mode"] == "pilot"
    return {k: s[k] for k in ("mode", "year", "campaign", "products", "channels", "competitors")} | {
        "candidates": len(s["candidates"]), "selections": s["selections"], "observations": len(issues),
        "report_entries": len(accepted), "formal_images": 0 if pilot else len(pics),
        "trial_preview_images": len(pics) if pilot else 0, "withdrawn": len(s["withdrawn"]),
        "pending_observations": sum(bool(x) for eid, x in issues.items() if eid not in s["withdrawn"]),
        "gaps": gap_list(s, issues)}


def selection(s, ids, name, replace):
    require(name == ("trial" if s["mode"] == "pilot" else "confirmed"), "试跑仅可 --trial；正式批次仅可 --confirmed。请另开并列目录切换模式")
    chosen = list(dict.fromkeys(ids))
    old = s["selections"]
    seen = {}
    for cid in chosen:
        require(cid in s["candidates"], f"未知候选：{cid}")
        c = s["candidates"][cid]
        require(known(c["sku"]), f"{cid} 尚未识别规格，不能选定")
        if c["role"] == "own":
            require(c["product"] not in seen, f"{c['product']} 本品同一行只可选一款")
            seen[c["product"]] = cid
    removed = []
    for cid in list(old):
        c = s["candidates"][cid]
        if c["role"] == "own" and seen.get(c["product"], cid) != cid:
            require(replace, f"{c['product']} 已选 {cid}；确需改款时用 --replace 明确替换")
            removed.append(cid)
    for cid in removed:
        s["selection_history"].append({"candidate_id": cid, **old.pop(cid), "ended_at": now()})
    for cid in chosen:
        if cid not in old:
            old[cid] = {"status": name, "at": now()}


def entry(s, eid):
    o = s["observations"][eid]
    c = s["candidates"][o["candidate_id"]]
    pilot = s["mode"] == "pilot"
    f = dict(o["fields"], 认款状态="试跑认款（非用户确认）" if pilot else "用户确认",
             来源范围=o["scope"])
    if o["kind"] == "offer":
        f["价格口径"] = next((k for k in PRICES if k in f), "仅列所见福利")
        f.setdefault("叠加条件", "未核实，不推算到手价")
    if o["kind"] == "rule":
        f["证据范围"] = "平台/店铺规则原文；未单独证明此 SKU 或账户符合资格"
    return {k: c[k] for k in ("product", "role", "brand", "sku")} | {
        k: o[k] for k in ("platform", "shop", "observed_at", "url", "kind", "excerpt")} | {
        "evidence_id": eid, "fields": f, "sample": pilot}


def h(x):
    return html.escape(str(x), quote=True)


def link(url, label):
    require(urlsplit(url).scheme in ("", "http", "https") and not url.startswith("//"), "无效链接类型")
    return f'<a href="{h(url)}">{h(label)}</a>'


def document(title, content):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src \'self\' file:; style-src \'unsafe-inline\'">'
            f'<title>{h(title)}</title><style>body{{font:16px/1.55 system-ui;margin:2rem;color:#213244}}'
            'article{border:1px solid #cbd5db;border-radius:8px;padding:1rem;margin-bottom:1rem}'
            'img{max-width:240px;max-height:180px}td,th{border:1px solid #bcc8ce;padding:.6rem;text-align:left;vertical-align:top}'
            'table{border-collapse:collapse}small{color:#465865}td,p{overflow-wrap:anywhere}pre{white-space:pre-wrap}</style>'
            f'<body><h1>{h(title)}</h1>{content}</body></html>').encode()


def mode_label(s):
    return "试跑预览（非用户确认）" if s["mode"] == "pilot" else "正式批次"


def cards(s, folder):
    parts = [f'<p>{h(s["campaign"])} · {mode_label(s)} · 本品每款只选一个 SKU。官网图片仅供认款。</p>']
    for cid, c in s["candidates"].items():
        sel = s["selections"].get(cid)
        name = {"trial": "试跑认款", "confirmed": "用户确认"}.get(sel["status"] if sel else "", "未认款")
        require(not c["image"] or HASHFILE.fullmatch(c["image"]), "无效的候选图标识")
        img = (f'<img loading="lazy" src="{quote(folder + "/" + c["image"])}" alt="本地候选图">' if c["image"] else "<p>候选图待补</p>")
        detail = " / ".join(c[k] for k in ("product", "brand", "sku", "platform", "shop"))
        parts.append(f'<article><b>{h(c["title"])}</b> · {"本品" if c["role"] == "own" else "竞品"} · {name}<p>{img}</p>'
                     f'<p>{h(detail)}</p><p>{h(c["basis"])}</p><code>{h(cid)}</code> · {link(c["url"], "候选来源")}</article>')
    if not s["candidates"]:
        parts.append("<p>暂无候选。</p>")
    return document("候选清单", "".join(parts))


def proof_table(records, s):
    header = ("证据 ID", "用途", "商品 / 身份 / 规格", "渠道 / 店铺 / 范围", "时间 / 来源", "摘录 / 字段", "本地附件")
    parts = [f'<p>{h(s["campaign"])} · {mode_label(s)}。待补行不进入报告事实。</p><table><tr>' +
             "".join(f"<th>{k}</th>" for k in header) + "</tr>"]
    for r in records:
        files = "<br>".join(link(quote(a["path"]), a["role"] + " " + Path(a["path"]).name[:12]) for a in r["files"])
        fields = "<br>".join(h(k) + "：" + h(v) for k, v in r["fields"].items())
        cols = (h(r["evidence_id"]), h(r["disposition"]) + "<br>" + h("；".join(r["reasons"])),
                h(" / ".join(r[k] for k in ("product", "role", "brand", "sku"))),
                h(" / ".join(r[k] for k in ("platform", "shop", "kind", "scope"))),
                h(r["observed_at"]) + "<br>" + link(r["url"], "查看来源"), h(r["excerpt"]) + "<hr>" + fields, files)
        parts.append("<tr>" + "".join("<td>" + x + "</td>" for x in cols) + "</tr>")
    return document("证据表", "".join(parts) + "</table>")


def report_writer():
    path = Path(__file__).resolve().with_name("report.py")
    require(path.is_file(), "缺少同目录 report.py（write_report(path, data)）；请集成后复跑 export，批次已保留")
    source = path.read_bytes()
    spec = importlib.util.spec_from_file_location("_promo_batch_report", path)
    module = importlib.util.module_from_spec(spec)
    # dataclass 等工具会根据 __module__ 查找模块。
    sys.modules[spec.name] = module
    exec(compile(source, str(path), "exec"), module.__dict__)
    fn = getattr(module, "write_report", None)
    require(callable(fn), "report.py 应提供 write_report(path, data)")
    return fn, hashlib.sha256(source).hexdigest()


def artifact_files(folder):
    d = {}
    for p in folder.rglob("*"):
        require(not p.is_symlink(), "导出目录含符号链接")
        if p.is_file() and p != folder / "导出回执.json":
            digest = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
            d[p.relative_to(folder).as_posix()] = digest.hexdigest()
    return d


def export_run(root, s):
    import zipfile
    writer, writer_sha = report_writer()
    issues = evidence(s)
    summary = status(s, issues)
    token = identity("V", [s, str(root), writer_sha, hashlib.sha256(Path(__file__).read_bytes()).hexdigest()])
    base = no_links(root / "exports")
    base.mkdir(exist_ok=True)
    dest = no_links(base / (("试跑-" if s["mode"] == "pilot" else "正式-") + token))
    originals = verified_assets(root, s)
    word = "双十一调研（试跑）.docx" if s["mode"] == "pilot" else "双十一调研.docx"
    out = {"export_dir": str(dest), "candidate_list": str(dest / "候选清单.html"), "evidence": str(dest / "证据表.html"),
           "gaps": str(dest / "待补.txt"), "report": str(dest / word), "mode": s["mode"],
           **{k: summary[k] for k in ("formal_images", "trial_preview_images", "report_entries", "pending_observations")}}
    def sub(kind, product):
        enum(product, s["products"], "归档目录对应的产品")
        return Path(LABELS[kind]) / (product if kind == "campaign" else product + "（更新）")
    if dest.exists():
        receipt = read_json(no_links(dest / "导出回执.json"))
        require(receipt.get("version") == token and receipt.get("files") == artifact_files(dest) and
                all((dest / sub(kind, p)).is_dir() for p in s["products"] for kind in ("campaign", "current")),
                "已有导出内容被改动，请移走该份导出后重试")
        save_json(root / "最近导出.json", out)
        return dict(out, reused=True)
    with tempfile.TemporaryDirectory(prefix=".writing-", dir=base) as tmp:
        stage = Path(tmp)
        attachments = stage / "附件"
        attachments.mkdir()
        for key, p in originals.items():
            shutil.copyfile(p, attachments / key)
        for product in s["products"]:
            for kind in ("current", "campaign"):
                (stage / sub(kind, product)).mkdir(parents=True, exist_ok=True)
        records, entries = [], []
        for eid, o in s["observations"].items():
            c = s["candidates"][o["candidate_id"]]
            why = issues[eid]
            files = [{"path": "附件/" + k, "role": "页面截图" if k in o["screenshot_assets"] else
                      "标注原图" if k in o["original_assets"] else "附件"} for k in o["assets"]]
            paths = []
            if not why:
                e = entry(s, eid)
                first = [*o["original_assets"], *o["screenshot_assets"], *o["assets"]]
                e["assets"] = [str(originals[k]) for k in dict.fromkeys(first)]
                entries.append(e)
                if o["kind"] in ("current", "campaign"):
                    for k in o["original_assets"]:
                        rel = sub(o["kind"], c["product"]) / k
                        if not (stage / rel).exists():
                            shutil.copyfile(attachments / k, stage / rel)
                        paths.append(str(rel))
            records.append({k: c[k] for k in ("product", "role", "brand", "sku")} |
                           {k: o[k] for k in OBS_KEYS if k != "assets"} |
                           {"evidence_id": eid, "selection": s["selections"].get(o["candidate_id"]),
                            "reasons": why, "disposition": "已撤销" if eid in s["withdrawn"] else "待补" if why else mode_label(s),
                            "files": files, "archived": paths})
        (stage / "候选清单.html").write_bytes(cards(s, "附件"))
        (stage / "证据表.html").write_bytes(proof_table(records, s))
        save_json(stage / "证据表.json", records)
        (stage / "待补.txt").write_text(mode_label(s) + "\n" + ("\n".join("• " + x for x in summary["gaps"]) or "无已知缺口") + "\n", encoding="utf-8")
        data = {"title": s["campaign"] + (" 男护渠道试跑" if s["mode"] == "pilot" else " 男护产品与渠道调研"), "campaign": s["campaign"],
                **{k: s[k] for k in ("products", "channels", "competitors")}, "entries": entries, "gaps": summary["gaps"],
                "pilot_batch": s["mode"] == "pilot", "manual_gaps": list(s["gaps"]),
                "candidate_refs": [{"title": c["title"], "role": c["role"], "sku": c["sku"], "url": c["url"],
                                    "platform": c["platform"], "asset": str(originals[c["image"]]) if c["image"] else ""}
                                   | {"identity": ("试跑样款" if s["mode"] == "pilot" else "已认本品") if cid in s["selections"] and c["role"] == "own" else ("竞品" if c["role"] == "competitor" else "本品备选")}
                                   for cid, c in sorted(s["candidates"].items(), key=lambda v: (v[0] not in s["selections"], v[1]["role"] != "own", v[1]["title"]))],
                "suggestions": ["按待补项回访对应渠道，保留与规格一致的页面和截图。"] if summary["gaps"] else [],
                "sample": s["mode"] == "pilot"}
        # Keep the exported receipt portable; the Word builder itself uses verified local paths.
        portable = dict(data, candidate_refs=[dict(c, asset=("附件/" + Path(c["asset"]).name) if c["asset"] else "")
                                               for c in data["candidate_refs"]],
                        entries=[dict(e, assets=["附件/" + Path(p).name for p in e["assets"]]) for e in entries])
        save_json(stage / "报告输入.json", portable)
        writer(stage / word, data)
        require((stage / word).is_file() and zipfile.is_zipfile(stage / word), "报告模块未产生有效 DOCX；本次导出未发布")
        with zipfile.ZipFile(stage / word) as z:
            require({"[Content_Types].xml", "word/document.xml"} <= set(z.namelist()) and z.testzip() is None,
                    "报告模块产生的 DOCX 不完整")
        save_json(stage / "导出回执.json", {"version": token, "mode": s["mode"], "files": artifact_files(stage)})
        stage.rename(dest)
    save_json(root / "最近导出.json", out)
    return dict(out, reused=False)


def guide():
    return {"run": {"start": "新建或续用同一模式的空/已初始化目录；--year 默认上海当前年，续跑沿用建档年",
                    "formal": dict(zip(("products", "channels", "competitors"), scope_config(False))),
                    "pilot": dict(zip(("products", "channels", "competitors"), scope_config(True)))},
            "candidates": {"type": "对象列表", "required_strings": CAND_KEYS, "role": ["own", "competitor"],
                           "platform": "本批次渠道或品牌官网（官网亦可）；平台必须与来源域名相符",
                           "optional": {"image": "批次内本地图片路径；直接进入候选清单，不算正式原图"}},
            "select": "select ID [...] --trial（仅试跑）或 --confirmed（仅正式）；每款本品最多一个 ID。另一次明确 --replace 可换款并保存历史",
            "observe": {"type": "对象列表", "required": OBS_KEYS, "kind": list(KINDS), "scope": list(SCOPES),
                        "observed_at": "ISO 8601 日期时间，必须含时区",
                        "assets": "本批次本地文件路径列表；可为空，缺证据时仅留线索",
                        "fields": "JSON 对象；中文键、非空字符串值。条件、门槛、赠品等照录原文",
                        "original": "JSON 布尔值；true 自身不证明原图",
                        "optional": {k: "assets 的图片子集；按字节区分原图与页面截图" for k in ARRAY_ROLES},
                        "image_gate": {"both": "已认本品、本渠道商品页、观察年=批次年、页面规格=候选 sku、original=true、原图短边≥600px、独立截图",
                                       "campaign": {"活动期": "来源明确的年份与双十一（如 2026年双十一）", "图中文字": "逐字摘录原图上的双十一或 11.11 等标识"},
                                       "current": {"双十一字样": "无", "版本依据": "例如：详情页标注当前在售包装 V2；无双十一字样或推测本身不足"}},
                        "offer_gate": {"all": "所选候选、本渠道商品/直播/结算页、页面规格=候选 sku、独立标注的截图",
                                       "prices": list(PRICES), "daily": "日常价另需单独的同规格非促销日常价记录，限 product_page；划线价/吊牌价不算",
                                       "campaign": "活动价/福利另需活动期；宣传价和活动标价限 product_page/live",
                                       "checkout": "结算实见价只允许 checkout，另需结算条件",
                                       "stacking": "一条只记一种价格；不推算叠券、跨店满减、会员到手价"},
                        "rule_gate": "已选候选，本渠道规则/商品说明/直播、活动期，附本地文字/PDF原文或明确标注的截图；不推断 SKU 可用"},
            "paths": "清单 FILE 相对命令工作目录；image/assets 及两类附件角色相对 --run 或用批次内绝对路径；禁跨批次、符号链接、..",
            "export": "JSON 回执含 export_dir/report；exports 下分试跑/正式快照，原图二级目录、候选清单.html、证据表.html/.json、待补.txt、报告输入.json、Word。相同状态/程序复用经校验快照",
            "report_api": "同目录 report.py: write_report(path: pathlib.Path, data: dict)；entries.assets 为绝对本地路径",
            "source_boundary": "离线；域名、字段、关联、文件格式/像素可校验，原图内容、SKU 对应、页面真伪和授权由导入者根据截图核实"}


def schema():
    st = {"type": "string", "minLength": 1, "maxLength": 12000}
    url = {"type": "string", "format": "uri", "pattern": "^https?://", "maxLength": 4096}
    paths = {"type": "array", "maxItems": 128, "items": {"type": "string", "minLength": 1, "maxLength": 4096}}
    platform = {"enum": [*CHANNELS, OFFICIAL, *PLATFORM_ALIASES]}
    c = {k: st for k in CAND_KEYS} | {
        "product": {"enum": PRODUCTS}, "role": {"enum": ["own", "competitor"]}, "url": url, "platform": platform,
        "image": {"type": ["string", "null"], "maxLength": 4096}}
    o = {k: st for k in OBS_KEYS} | {
        "platform": platform, "url": url, "kind": {"enum": list(KINDS)}, "scope": {"enum": list(SCOPES)},
        "observed_at": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([Zz]|[+-]\d{2}:?\d{2})$"},
        "fields": {"type": "object", "propertyNames": {"type": "string", "pattern": "[\u3400-\u9fff]", "maxLength": 80},
                   "additionalProperties": st},
        "original": {"type": "boolean"}, **{k: paths for k in ("assets", *ARRAY_ROLES)}}
    def obj(properties, required):
        return {"type": "object", "required": required, "properties": properties, "additionalProperties": False}
    def ref(name):
        return {"$ref": "#/$defs/" + name}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "双十一调研 CLI 输入清单",
            "anyOf": [ref("candidates"), ref("observations")],
            "$defs": {"candidate": obj(c, CAND_KEYS), "observation": obj(o, OBS_KEYS),
                      "candidates": {"type": "array", "items": ref("candidate")},
                      "observations": {"type": "array", "items": ref("observation")}}, "x-cli": guide()}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", metavar="DIR", help="当前批次专用目录")
    cmds = p.add_subparsers(dest="cmd", required=True)
    a = cmds.add_parser("start", help="初始化或续跑（模式不可更改）")
    a.add_argument("--pilot", action="store_true")
    a.add_argument("--year", type=int)
    for name in ("candidates", "observe"):
        cmds.add_parser(name, help="导入 UTF-8 JSON 对象列表").add_argument("file", metavar="FILE")
    a = cmds.add_parser("select", help="明确试跑认款或用户确认")
    a.add_argument("ids", nargs="+", metavar="ID")
    mode = a.add_mutually_exclusive_group(required=True)
    mode.add_argument("--trial", action="store_true")
    mode.add_argument("--confirmed", action="store_true")
    a.add_argument("--replace", action="store_true", help="明确替换同款本品的旧选择，历史留存")
    a = cmds.add_parser("gap", help="记录或消除一项人工缺口")
    a.add_argument("reason", metavar="原因")
    a.add_argument("--resolve", action="store_true")
    a = cmds.add_parser("withdraw", help="撤销错误证据，保留原始行和原因")
    a.add_argument("ids", nargs="+", metavar="E-ID")
    a.add_argument("--reason", required=True)
    for name, help_ in (("status", "查看选择、缺口和有效数量"), ("export", "生成可追溯的批次交付物"), ("schema", "打印 JSON 契约")):
        cmds.add_parser(name, help=help_)
    return p


def run(a):
    if a.cmd == "schema":
        return schema()
    require(a.run, "需要 --run DIR")
    root = abs_path(a.run)
    with locked(root, a.cmd == "start") as s:
        if a.cmd == "start":
            mode = "pilot" if a.pilot else "formal"
            require(a.year is None or 2000 <= a.year <= 2099, "--year 应在 2000 至 2099")
            if s is not None:
                require(s["mode"] == mode and (a.year is None or s["year"] == a.year), "同一目录不能切换试跑/正式或活动年，请新建并列批次目录")
                verified_assets(root, s)
            else:
                year = a.year or dt.datetime.now(CN).year
                products, channels, brands = scope_config(a.pilot)
                s = dict(schema_version=STATE_VERSION, mode=mode, year=year, campaign=f"{year}年双十一", created_at=now(),
                         products=products, channels=channels, competitors=brands, candidates={}, selections={},
                         selection_history=[], observations={}, withdrawn={}, blobs={}, gaps=[])
                save_json(root / STATE, s)
            return dict(run=str(root), **status(s))
        if a.cmd == "status":
            verified_assets(root, s)
            return dict(run=str(root), candidate_ids=list(s["candidates"]), **status(s))
        if a.cmd == "export":
            return export_run(root, s)
        if a.cmd in {"candidates", "observe"}:
            items = rows(a.file)
            ids = []
            cards_path = no_links(root / "候选清单.html")
            with tempfile.TemporaryDirectory(prefix=".promo-intake-", dir=root) as tmp:
                intake = Intake(root, s, tmp)
                for i, row in enumerate(items, 1):
                    try:
                        ids.append((candidate if a.cmd == "candidates" else observation)(row, s, intake))
                    except (ValueError, OSError) as e:
                        raise Invalid(f"第 {i} 行：{e}") from None
                intake.publish()
            save_json(root / STATE, s)
            if a.cmd == "candidates":
                atomic(cards_path, cards(s, BLOBS))
                return {"ids": ids, "total": len(s["candidates"]), "candidate_list": str(cards_path)}
            result = evidence(s)
            return {"ids": ids, "total": len(s["observations"]), "pending": {i: result[i] for i in ids if result[i]}}
        if a.cmd == "select":
            cards_path = no_links(root / "候选清单.html")
            selection(s, a.ids, "trial" if a.trial else "confirmed", a.replace)
            save_json(root / STATE, s)
            atomic(cards_path, cards(s, BLOBS))
            return {"mode": s["mode"], "selections": s["selections"], "candidate_list": str(cards_path)}
        if a.cmd == "withdraw":
            reason = string(a.reason, "reason")
            for eid in a.ids:
                require(eid in s["observations"], f"无此证据：{eid}")
                s["withdrawn"].setdefault(eid, {"reason": reason, "at": now()})
            save_json(root / STATE, s)
            return {"withdrawn": s["withdrawn"]}
        if a.cmd == "gap":
            reason = string(a.reason, "原因")
            if a.resolve and reason in s["gaps"]:
                s["gaps"].remove(reason)
            elif not a.resolve and reason not in s["gaps"]:
                s["gaps"].append(reason)
            save_json(root / STATE, s)
            return {"manual_gaps": s["gaps"]}


def main():
    try:
        print(dump(run(parser().parse_args()), True))
        return 0
    except (OSError, ValueError, ImportError, RuntimeError, KeyError, TypeError) as e:
        print("promo: " + str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

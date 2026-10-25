#!/usr/bin/env python3
"""DECK LOG deck ID -> Tabletop Simulator Steam Cloud save converter.

Uses only the Python standard library. Public deck API:
POST https://decklog.bushiroad.com/system/app/api/view/{deck_id}
Card image path is taken from each API item's `img` field.
"""
from __future__ import annotations
import argparse, json, os, re, shlex, shutil, struct, subprocess, sys, urllib.error, urllib.request, zlib
from pathlib import Path
from typing import Any

API_URL = "https://decklog.bushiroad.com/system/app/api/view/{deck_id}"
VIEW_URL = "https://decklog.bushiroad.com/view/{deck_id}"
IMAGE_ROOT = "https://cf-vanguard.com/wordpress/wp-content/images/cardlist/"
PLACEHOLDER_ASSET_BASE = "https://steamcloud.invalid/{deck_id}/assets/"
DEFAULT_CLOUD_PATH = "decklog2tts/assets"
CACHE_ROOT = Path("cache") / "assets"
CARD_BACK_NAME = "card_back.png"
STATIC_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
STATIC_CARD_BACK = STATIC_ASSETS_DIR / CARD_BACK_NAME
DEFAULT_CARD_WIDTH = 350
DEFAULT_CARD_HEIGHT = 510
DEFAULT_CARD_ASPECT = DEFAULT_CARD_HEIGHT / DEFAULT_CARD_WIDTH
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def load_local_env() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    for env_path in candidates:
        if env_path.is_file():
            try:
                for raw in env_path.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value
            except OSError:
                pass
            break


load_local_env()
ENV_CLOUD_PATH = os.environ.get("DECKLOG2TTS_CLOUD_PATH")
ENV_CLOUD_CLI = os.environ.get("DECKLOG2TTS_CLOUD_CLI")


def _coerce_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def normalize_deck_id(value: str) -> str:
    value = value.strip().rstrip("/")
    m = re.search(r"/view/([A-Za-z0-9]+)$", value)
    deck_id = m.group(1) if m else value
    if not re.fullmatch(r"[A-Za-z0-9]+", deck_id):
        raise ValueError("Deck IDまたは /view/{DeckID} URLを指定してください。")
    return deck_id.upper()


def extract_recipe_sources(url: str) -> tuple[list[str], list[str]]:
    raw, ctype = request_bytes(url, referer=url)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="ignore")

    codes: list[str] = []
    recipe_ids: list[str] = []

    def add(code: str) -> None:
        try:
            normalized = normalize_deck_id(code)
        except ValueError:
            return
        if normalized not in codes:
            codes.append(normalized)

    for match in re.finditer(r"https?://decklog\\.bushiroad\\.com/view/([A-Za-z0-9]+)", text, re.IGNORECASE):
        add(match.group(1))

    if not codes:
        for match in re.finditer(r"DECK\\s*LOG(?:コード|code)[:：\\s]*([A-Za-z0-9]+)", text, re.IGNORECASE):
            add(match.group(1))

    for match in re.finditer(r"data-recipe-id=\"(\d+)\"", text, re.IGNORECASE):
        recipe_id = match.group(1)
        if recipe_id not in recipe_ids:
            recipe_ids.append(recipe_id)

    return codes, recipe_ids


def fetch_recipe_deck(recipe_id: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    url = f"https://cf-vanguard.com/manage/deckrecipe/detail?id={recipe_id}"
    raw, ctype = request_bytes(url, referer=url)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"公式デッキレシピAPIをJSONとして解析できません: Content-Type={ctype!r}") from e
    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError("公式デッキレシピAPIが失敗を返しました。")
    deck = data.get("deck")
    details = data.get("details")
    if not isinstance(deck, dict) or not isinstance(details, list):
        raise RuntimeError("公式デッキレシピAPIの形式が想定外です。")

    main: list[dict[str, Any]] = []
    ride: list[dict[str, Any]] = []

    for entry in details:
        if not isinstance(entry, dict):
            continue
        img = str(entry.get("picture", "")).strip()
        if not img:
            continue
        item = {
            "name": str(entry.get("card_name", "")),
            "card_number": str(entry.get("card_number", "")),
            "img": img,
            "num": _coerce_int(entry.get("num")),
        }
        target = ride if _coerce_int(entry.get("is_ride")) else main
        target.append(item)

    if not main and not ride:
        raise RuntimeError("公式デッキレシピのカード詳細が取得できませんでした。")

    return deck, main, ride


def build_recipe_deck_structure(recipe_id: str, deck_meta: dict[str, Any], main: list[dict[str, Any]], ride: list[dict[str, Any]]) -> dict[str, Any]:
    deck_id = f"RECIPE{recipe_id}"
    title = str(deck_meta.get("name") or deck_meta.get("deck_name") or deck_id)
    return {
        "deck_id": deck_id,
        "title": title,
        "list": main,
        "p_list": ride,
    }


def request_bytes(url: str, *, method: str = "GET", referer: str | None = None) -> tuple[bytes, str]:
    headers = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, data=b"" if method == "POST" else None, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(), r.headers.get("Content-Type", "")


def fetch_deck(deck_id: str) -> dict[str, Any]:
    raw, ctype = request_bytes(API_URL.format(deck_id=deck_id), method="POST", referer=VIEW_URL.format(deck_id=deck_id))
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"API応答をJSONとして解析できません: Content-Type={ctype!r}") from e
    if not isinstance(data, dict) or "list" not in data:
        raise RuntimeError("API応答に list がありません。公開デッキか確認してください。")
    return data


def safe_asset_name(img_path: str) -> str:
    p = img_path.replace("\\", "/").strip("/")
    if ".." in p.split("/"):
        raise ValueError(f"不正な画像パス: {img_path}")
    return p.replace("/", "__")


def ensure_png(path: Path, raw: bytes, ctype: str, url: str) -> None:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise RuntimeError(f"PNGではない応答です: {url} (Content-Type={ctype!r}, {len(raw)} bytes)")
    path.write_bytes(raw)


def download_images(
    items: list[dict[str, Any]], cache_root: Path, refresh: bool = False
) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
    cache_root.mkdir(parents=True, exist_ok=True)
    meta: dict[str, tuple[int, int, bool]] = {}
    for item in items:
        img = str(item.get("img", ""))
        if not img or img in meta:
            continue
        width = _coerce_int(item.get("width"))
        height = _coerce_int(item.get("height"))
        direction = bool(_coerce_int(item.get("direction")))
        meta[img] = (width, height, direction)
    mapping: dict[str, str] = {}
    dimensions: dict[str, tuple[int, int]] = {}
    seen: set[str] = set()
    for item in items:
        img = str(item.get("img", ""))
        if not img or img in seen:
            continue
        seen.add(img)
        name = safe_asset_name(img)
        dst = cache_root / name
        mapping[img] = name
        if not dst.exists() or refresh:
            url = IMAGE_ROOT + img
            raw, ctype = request_bytes(url, referer="https://cf-vanguard.com/cardlist/")
            ensure_png(dst, raw, ctype, url)
            print(f"downloaded: {img}")
        info = meta.get(img)
        width = info[0] if info else 0
        height = info[1] if info else 0
        sideways = bool(info[2]) if info else False
        maybe_rotate_landscape(dst, width, height, sideways)
        dims = png_dimensions(dst)
        if dims and dims[0] > 0 and dims[1] > 0:
            raw_w, raw_h = dims
        else:
            raw_w = width if width > 0 else DEFAULT_CARD_WIDTH
            raw_h = height if height > 0 else DEFAULT_CARD_HEIGHT
        dimensions[img] = (raw_w, raw_h)
    return mapping, dimensions


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def png_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        with path.open("rb") as f:
            header = f.read(24)
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", header[16:24])


def _png_unfilter_scanlines(data: bytes, width: int, height: int, bpp: int) -> list[bytearray] | None:
    stride = width * bpp
    expected = (stride + 1) * height
    if len(data) != expected:
        return None
    rows: list[bytearray] = []
    prev = bytearray(stride)
    idx = 0
    for _ in range(height):
        ftype = data[idx]
        idx += 1
        raw = bytearray(data[idx:idx + stride])
        idx += stride
        if ftype == 0:
            recon = raw
        elif ftype == 1:
            recon = bytearray(stride)
            for i in range(stride):
                left = recon[i - bpp] if i >= bpp else 0
                recon[i] = (raw[i] + left) & 0xFF
        elif ftype == 2:
            recon = bytearray(stride)
            for i in range(stride):
                recon[i] = (raw[i] + prev[i]) & 0xFF
        elif ftype == 3:
            recon = bytearray(stride)
            for i in range(stride):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev[i]
                recon[i] = (raw[i] + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:
            recon = bytearray(stride)
            for i in range(stride):
                left = recon[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                pa = left + up - up_left
                pa_left = abs(pa - left)
                pa_up = abs(pa - up)
                pa_ul = abs(pa - up_left)
                if pa_left <= pa_up and pa_left <= pa_ul:
                    pr = left
                elif pa_up <= pa_ul:
                    pr = up
                else:
                    pr = up_left
                recon[i] = (raw[i] + pr) & 0xFF
        else:
            return None
        rows.append(recon)
        prev = recon
    return rows


def rotate_png_ccw(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    width = height = bit_depth = color_type = compression = filter_method = interlace = None
    ancillary: list[tuple[bytes, bytes]] = []
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif ctype == b"IDAT":
            idat.extend(chunk_data)
        elif ctype == b"IEND":
            break
        else:
            ancillary.append((ctype, chunk_data))
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width <= 0 or height <= 0:
        return False
    if bit_depth != 8 or compression != 0 or filter_method != 0 or interlace != 0:
        return False
    if color_type == 6:
        bpp = 4
    elif color_type == 2:
        bpp = 3
    elif color_type == 3:
        bpp = 1
    else:
        return False
    try:
        decompressed = zlib.decompress(bytes(idat))
    except zlib.error:
        return False
    rows = _png_unfilter_scanlines(decompressed, width, height, bpp)
    if rows is None:
        return False
    new_width = height
    new_height = width
    rotated = [bytearray(new_width * bpp) for _ in range(new_height)]
    for old_y in range(height):
        row = rows[old_y]
        for old_x in range(width):
            pixel = row[old_x * bpp:(old_x + 1) * bpp]
            new_y = width - 1 - old_x
            new_x = old_y
            rotated[new_y][new_x * bpp:(new_x + 1) * bpp] = pixel
    filtered = bytearray()
    for row in rotated:
        filtered.append(0)
        filtered.extend(row)
    new_chunks = [png_chunk(b"IHDR", struct.pack(">IIBBBBB", new_width, new_height, bit_depth, color_type, 0, 0, 0))]
    for ctype, chunk_data in ancillary:
        if ctype not in (b"IDAT", b"IEND"):
            new_chunks.append(png_chunk(ctype, chunk_data))
    new_chunks.append(png_chunk(b"IDAT", zlib.compress(bytes(filtered), 9)))
    new_chunks.append(png_chunk(b"IEND", b""))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(new_chunks))
    return True


def rotate_png_cw(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    pos = 8
    width = height = bit_depth = color_type = compression = filter_method = interlace = None
    ancillary: list[tuple[bytes, bytes]] = []
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        chunk_data = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(">IIBBBBB", chunk_data)
        elif ctype == b"IDAT":
            idat.extend(chunk_data)
        elif ctype == b"IEND":
            break
        else:
            ancillary.append((ctype, chunk_data))
    if not isinstance(width, int) or not isinstance(height, int):
        return False
    if width <= 0 or height <= 0:
        return False
    if bit_depth != 8 or compression != 0 or filter_method != 0 or interlace != 0:
        return False
    if color_type == 6:
        bpp = 4
    elif color_type == 2:
        bpp = 3
    elif color_type == 3:
        bpp = 1
    else:
        return False
    try:
        decompressed = zlib.decompress(bytes(idat))
    except zlib.error:
        return False
    rows = _png_unfilter_scanlines(decompressed, width, height, bpp)
    if rows is None:
        return False
    new_width = height
    new_height = width
    rotated = [bytearray(new_width * bpp) for _ in range(new_height)]
    for old_y in range(height):
        row = rows[old_y]
        for old_x in range(width):
            pixel = row[old_x * bpp:(old_x + 1) * bpp]
            new_y = old_x
            new_x = new_width - 1 - old_y
            rotated[new_y][new_x * bpp:(new_x + 1) * bpp] = pixel
    filtered = bytearray()
    for row in rotated:
        filtered.append(0)
        filtered.extend(row)
    new_chunks = [png_chunk(b"IHDR", struct.pack(">IIBBBBB", new_width, new_height, bit_depth, color_type, 0, 0, 0))]
    for ctype, chunk_data in ancillary:
        if ctype not in (b"IDAT", b"IEND"):
            new_chunks.append(png_chunk(ctype, chunk_data))
    new_chunks.append(png_chunk(b"IDAT", zlib.compress(bytes(filtered), 9)))
    new_chunks.append(png_chunk(b"IEND", b""))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"".join(new_chunks))
    return True


def maybe_rotate_landscape(path: Path, width: int, height: int, sideways: bool) -> None:
    if not sideways:
        return
    if width > 0 and height > 0 and width <= height:
        return
    dims = png_dimensions(path)
    if not dims:
        return
    actual_w, actual_h = dims
    if actual_w <= actual_h:
        return
    if rotate_png_cw(path):
        print(f"rotated: {path.name}")


def create_back_png(path: Path, width: int = DEFAULT_CARD_WIDTH, height: int = DEFAULT_CARD_HEIGHT) -> None:
    # Solid navy card back with a light border. No external asset dependency.
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            border = x < 14 or y < 14 or x >= width - 14 or y >= height - 14
            inner = x < 21 or y < 21 or x >= width - 21 or y >= height - 21
            rgb = (220, 225, 235) if border else ((35, 52, 92) if inner else (14, 25, 52))
            row.extend((*rgb, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)
    png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(raw, 9)) + png_chunk(b"IEND", b"")
    path.write_bytes(png)


def transform(x: float, y: float, z: float) -> dict[str, float]:
    return {
        "posX": x,
        "posY": y,
        "posZ": z,
        "rotX": 0,
        "rotY": 180,
        "rotZ": 180,
        "scaleX": 1,
        "scaleY": 1,
        "scaleZ": 1,
    }


def make_deck(
    name: str,
    items: list[dict[str, Any]],
    pos: tuple[float, float, float],
    asset_base: str,
    mapping: dict[str, str],
) -> dict[str, Any]:
    asset_base = asset_base.rstrip("/") + "/"
    back_url = asset_base + "card_back.png"
    custom: dict[str, Any] = {}
    deck_ids: list[int] = []
    contained: list[dict[str, Any]] = []
    for idx, item in enumerate(items, start=1):
        key = str(idx)
        image_key = str(item["img"])
        image_name = mapping[image_key]
        custom[key] = {"FaceURL": asset_base + image_name, "BackURL": back_url, "NumWidth": 1, "NumHeight": 1, "BackIsHidden": True, "UniqueBack": False, "Type": 0}
        card_id = idx * 100
        sideways = bool(item.get("direction", 0))
        for _ in range(int(item.get("num", 1))):
            deck_ids.append(card_id)
            entry = {
                "Name": "CardCustom",
                "Transform": transform(0, 0, 0),
                "Nickname": str(item.get("name", "")),
                "Description": str(item.get("card_number", "")),
                "CardID": card_id,
                "SidewaysCard": sideways,
                "CustomDeck": {key: custom[key]},
                "LuaScript": "",
                "LuaScriptState": "",
                "XmlUI": "",
            }
            contained.append(entry)
    return {
        "Name": "DeckCustom", "Transform": transform(*pos), "Nickname": name,
        "Description": "Generated by DeckLog2TTS", "DeckIDs": deck_ids,
        "CustomDeck": custom, "ContainedObjects": contained,
        "LuaScript": "", "LuaScriptState": "", "XmlUI": ""
    }


def ensure_card_back(cache_root: Path, refresh: bool = False) -> Path:
    cache_root.mkdir(parents=True, exist_ok=True)
    back_path = cache_root / CARD_BACK_NAME
    if STATIC_CARD_BACK.is_file():
        if refresh and back_path.exists():
            back_path.unlink()
        if not back_path.exists():
            shutil.copy2(STATIC_CARD_BACK, back_path)
        return back_path
    if refresh or not back_path.exists():
        create_back_png(back_path)
    return back_path


def sync_deck_assets(required_names: set[str], cache_root: Path, assets_dir: Path, refresh: bool = False) -> None:
    assets_dir.mkdir(parents=True, exist_ok=True)
    existing = {p.name for p in assets_dir.glob("*") if p.is_file()}
    for name in required_names:
        src = cache_root / name
        if not src.exists():
            raise RuntimeError(f"キャッシュに存在しないアセットです: {name}")
        dst = assets_dir / name
        if dst.exists():
            if refresh:
                dst.unlink()
            else:
                continue
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
    for redundant in existing - required_names:
        try:
            (assets_dir / redundant).unlink()
        except FileNotFoundError:
            pass


def generate(deck: dict[str, Any], out_root: Path, asset_base: str, refresh: bool = False) -> tuple[Path, Path, dict[str, str]]:
    deck_id = str(deck["deck_id"])
    bundle = out_root / deck_id
    assets = bundle / "assets"
    cache_root = CACHE_ROOT
    main = list(deck.get("list") or [])
    ride = list(deck.get("p_list") or [])
    all_items = main + ride
    mapping, dimensions = download_images(all_items, cache_root, refresh)
    for item in all_items:
        if "direction" in item:
            continue
        img = str(item.get("img", ""))
        dims = dimensions.get(img)
        if dims and dims[0] > 0 and dims[1] > 0:
            item["direction"] = 1 if dims[0] > dims[1] else 0
        else:
            item["direction"] = 0
    for item in all_items:
        img = str(item.get("img", ""))
        if not img:
            continue
        name = mapping.get(img)
        if not name:
            continue
        sideways = bool(item.get("direction"))
        if not sideways:
            continue
        dims = dimensions.get(img)
        width = dims[0] if dims else 0
        height = dims[1] if dims else 0
        maybe_rotate_landscape(cache_root / name, width, height, sideways)
    ensure_card_back(cache_root, refresh)
    required_names = set(mapping.values()) | {CARD_BACK_NAME}
    sync_deck_assets(required_names, cache_root, assets, refresh)
    save = {
        "SaveName": f"{deck.get('title', deck_id)} - {deck_id}", "GameMode": "", "Date": "", "Table": "", "Sky": "",
        "Note": f"DECK LOG {deck_id}; main={sum(int(x.get('num',1)) for x in main)}, ride/other={sum(int(x.get('num',1)) for x in ride)}",
        "Rules": "", "PlayerTurn": "", "ObjectStates": [
            make_deck("メインデッキ", main, (-2, 1, 0), asset_base, mapping),
            make_deck("ライドデッキ等", ride, (2, 1, 0), asset_base, mapping)
        ]
    }
    save_path = bundle / f"{deck_id}.json"
    save_path.write_text(json.dumps(save, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "deck_id": deck_id,
        "title": deck.get("title"),
        "asset_base": asset_base,
        "main": main,
        "p_list": ride,
        "assets": sorted(required_names),
    }
    manifest_path = bundle / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return save_path, bundle, mapping


def install_save(save_path: Path) -> Path:
    home = Path.home()
    dst_dir = home / "Documents" / "My Games" / "Tabletop Simulator" / "Saves" / "Saved Objects"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / save_path.name
    shutil.copy2(save_path, dst)
    return dst


def resolve_cloud_cli(command: str) -> list[str]:
    raw = command.strip()
    if not raw:
        raise RuntimeError("--cloud-cli で指定したコマンドが空です。")
    parts = shlex.split(raw, posix=False)
    if not parts:
        raise RuntimeError("--cloud-cli で指定したコマンドが空です。")

    def ensure_cmd_wrapper(script_path: Path) -> list[str]:
        return ["cmd", "/c", str(script_path)]

    token_path = Path(parts[0]).expanduser()
    if len(parts) == 1:
        if token_path.is_dir():
            script = token_path / "sync-with-cloud.bat"
            if script.exists():
                return ensure_cmd_wrapper(script)
            exe = token_path / "sync-with-cloud.exe"
            if exe.exists():
                return [str(exe)]
        elif token_path.suffix.lower() == ".bat":
            if token_path.exists():
                return ensure_cmd_wrapper(token_path)
            return ensure_cmd_wrapper(token_path)
        elif token_path.suffix.lower() == ".exe":
            return [str(token_path)]

    # 複数トークン指定時
    suffixes = [Path(p).suffix.lower() for p in parts]
    if any(suf == ".bat" for suf in suffixes):
        if not (parts[0].lower() == "cmd" and len(parts) > 1 and parts[1].lower() in {"/c", "/k"}):
            return ["cmd", "/c"] + parts
        return parts

    if not any("sync-with-cloud" in Path(token).name.lower() or token.lower() == "sync-with-cloud" for token in parts):
        parts.append("sync-with-cloud")
    return parts


def sync_assets_to_cloud(source_dir: Path, *, deck_id: str, command: str, remote_path: str) -> dict[str, str]:
    remote_path = remote_path.replace("\\", "/").strip("/")
    if not remote_path:
        remote_path = DEFAULT_CLOUD_PATH.format(deck_id=deck_id)
    parts = resolve_cloud_cli(command)
    cmd = parts + [str(source_dir), remote_path]
    try:
        completed = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise RuntimeError(f"sync-with-cloud コマンドが見つかりません: {command}") from e
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        hint = f" (stderr: {stderr})" if stderr else ""
        raise RuntimeError(f"sync-with-cloud が終了コード {completed.returncode} で失敗しました{hint}")
    mapping: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        line = line.strip()
        if not line or ";" not in line:
            continue
        key, url = line.split(";", 1)
        if "_" not in key:
            continue
        _, name = key.split("_", 1)
        mapping[name] = url.strip()
    if not mapping:
        raise RuntimeError("Steam Cloudへのアップロード結果を解析できませんでした。sync-with-cloud の出力を確認してください。")
    return mapping


def apply_cloud_urls(save_path: Path, manifest_path: Path, remote_path: str, url_mapping: dict[str, str]) -> str:
    if not url_mapping:
        raise RuntimeError("Steam CloudのURLマッピングが空です。")

    data = json.loads(save_path.read_text(encoding="utf-8"))
    missing: set[str] = set()
    used_urls: dict[str, str] = {}

    def update(node: Any) -> None:
        if isinstance(node, dict):
            custom_decks = node.get("CustomDeck")
            if isinstance(custom_decks, dict):
                for custom in custom_decks.values():
                    if not isinstance(custom, dict):
                        continue
                    for field in ("FaceURL", "BackURL"):
                        url = custom.get(field)
                        if not isinstance(url, str) or not url:
                            continue
                        name = url.rsplit("/", 1)[-1]
                        if name in url_mapping:
                            remote = url_mapping[name]
                            custom[field] = remote
                            used_urls[name] = remote
                        elif name:
                            missing.add(name)
            for key in ("ObjectStates", "ContainedObjects"):
                children = node.get(key)
                if isinstance(children, list):
                    for item in children:
                        update(item)
        elif isinstance(node, list):
            for item in node:
                update(item)

    update(data)

    if missing:
        raise RuntimeError("Steam Cloudアップロードに存在しないファイルがあります: " + ", ".join(sorted(missing)))
    if not used_urls:
        raise RuntimeError("Steam CloudのURLに差し替える対象が見つかりませんでした。")

    save_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base_sample = used_urls.get(CARD_BACK_NAME) or next(iter(used_urls.values()))
    asset_base = base_sample.rsplit("/", 1)[0] + "/"
    manifest["asset_base"] = asset_base
    manifest["cloud"] = {"path": remote_path, "urls": used_urls}
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return asset_base


def convert_single_deck(
    deck_id: str,
    *,
    output: str,
    refresh: bool,
    install: bool,
    cloud_path: str,
    cloud_cli: str,
) -> int:
    try:
        deck = fetch_deck(deck_id)
        asset_base = PLACEHOLDER_ASSET_BASE.format(deck_id=deck_id)
        save_path, bundle, mapping = generate(deck, Path(output), asset_base, refresh)
        required_names = set(mapping.values()) | {CARD_BACK_NAME}
        remote_path = cloud_path.format(deck_id=deck_id)
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        all_urls = sync_assets_to_cloud(CACHE_ROOT, deck_id=deck_id, command=cloud_cli, remote_path=remote_path)
        filtered_urls = {name: all_urls[name] for name in required_names if name in all_urls}
        missing_urls = required_names - filtered_urls.keys()
        if missing_urls:
            raise RuntimeError("Steam Cloudに存在しないアセットがあります: " + ", ".join(sorted(missing_urls)))
        asset_base = apply_cloud_urls(save_path, bundle / "manifest.json", remote_path, filtered_urls)
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        print(f"ERROR[{deck_id}]: {e}", file=sys.stderr)
        return 1
    print(f"created[{deck_id}]: {save_path}")
    print(f"assets [{deck_id}]: {bundle / 'assets'}")
    print(f"cache  [{deck_id}]: {CACHE_ROOT}")
    print(f"cloud  [{deck_id}]: {asset_base}")
    if install:
        print(f"installed[{deck_id}]: {install_save(save_path)}")
    print("TTSを起動し Games > Save & Load からセーブを読み込むと、画像はSteam Cloudから配信されます。")
    return 0



def cmd_convert(args: argparse.Namespace) -> int:
    deck_id = normalize_deck_id(args.deck)
    return convert_single_deck(
        deck_id,
        output=args.output,
        refresh=args.refresh,
        install=args.install,
        cloud_path=args.cloud_path,
        cloud_cli=args.cloud_cli,
    )


def convert_recipe_deck(
    recipe_id: str,
    *,
    output: str,
    refresh: bool,
    install: bool,
    cloud_path: str,
    cloud_cli: str,
) -> int:
    try:
        deck_meta, main, ride = fetch_recipe_deck(recipe_id)
        deck_struct = build_recipe_deck_structure(recipe_id, deck_meta, main, ride)
        asset_base = PLACEHOLDER_ASSET_BASE.format(deck_id=deck_struct["deck_id"])
        save_path, bundle, mapping = generate(deck_struct, Path(output), asset_base, refresh)
        required_names = set(mapping.values()) | {CARD_BACK_NAME}
        remote_path = cloud_path.format(deck_id=deck_struct["deck_id"])
        CACHE_ROOT.mkdir(parents=True, exist_ok=True)
        all_urls = sync_assets_to_cloud(CACHE_ROOT, deck_id=deck_struct["deck_id"], command=cloud_cli, remote_path=remote_path)
        filtered_urls = {name: all_urls[name] for name in required_names if name in all_urls}
        missing_urls = required_names - filtered_urls.keys()
        if missing_urls:
            raise RuntimeError("Steam Cloudに存在しないアセットがあります: " + ", ".join(sorted(missing_urls)))
        asset_base = apply_cloud_urls(save_path, bundle / "manifest.json", remote_path, filtered_urls)
    except (urllib.error.URLError, RuntimeError, ValueError) as e:
        print(f"ERROR[recipe {recipe_id}]: {e}", file=sys.stderr)
        return 1
    print(f"created[recipe {recipe_id}]: {save_path}")
    print(f"assets [recipe {recipe_id}]: {bundle / 'assets'}")
    print(f"cache  [recipe {recipe_id}]: {CACHE_ROOT}")
    print(f"cloud  [recipe {recipe_id}]: {asset_base}")
    if install:
        print(f"installed[recipe {recipe_id}]: {install_save(save_path)}")
    print("TTSを起動し Games > Save & Load からセーブを読み込むと、画像はSteam Cloudから配信されます。")
    return 0


def cmd_recipe(args: argparse.Namespace) -> int:
    deck_ids, recipe_ids = extract_recipe_sources(args.url)
    if not deck_ids and not recipe_ids:
        raise RuntimeError("公式デッキレシピから変換対象を抽出できませんでした。URLを確認してください。")

    total = len(deck_ids) + len(recipe_ids)
    print(f"found {total} deck(s) in recipe: {args.url}")
    result = 0
    idx = 0
    for deck_id in deck_ids:
        idx += 1
        print(f"== [{idx}/{total}] converting {deck_id} ==")
        status = convert_single_deck(
            deck_id,
            output=args.output,
            refresh=args.refresh,
            install=args.install,
            cloud_path=args.cloud_path,
            cloud_cli=args.cloud_cli,
        )
        if status != 0:
            result = status
    for recipe_id in recipe_ids:
        idx += 1
        print(f"== [{idx}/{total}] converting recipe #{recipe_id} ==")
        status = convert_recipe_deck(
            recipe_id,
            output=args.output,
            refresh=args.refresh,
            install=args.install,
            cloud_path=args.cloud_path,
            cloud_cli=args.cloud_cli,
        )
        if status != 0:
            result = status
    return result



def main() -> int:
    p = argparse.ArgumentParser(description="DECK LOG公開デッキをTTS Saveへ変換")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("convert", help="Deck ID/URLからセーブと画像を生成（Steam Cloud必須）")
    c.add_argument("deck", help="例: 1NWU1J または https://decklog.bushiroad.com/view/1NWU1J")
    c.add_argument("-o", "--output", default="output")
    c.add_argument("--refresh", action="store_true", help="キャッシュ済み画像を再取得")
    c.add_argument("--install", action="store_true", help="WindowsのTTS SavesへJSONをコピー")
    c.add_argument(
        "--cloud-path",
        default=ENV_CLOUD_PATH or DEFAULT_CLOUD_PATH,
        help="Steam Cloud上の保存先パス。{deck_id}を使用可能。環境変数 DECKLOG2TTS_CLOUD_PATH でも設定可",
    )
    c.add_argument(
        "--cloud-cli",
        default=ENV_CLOUD_CLI or "sync-with-cloud",
        help="sync-with-cloudコマンドのパスまたはディレクトリ。環境変数 DECKLOG2TTS_CLOUD_CLI に設定すると省略可",
    )
    c.set_defaults(func=cmd_convert)

    r = sub.add_parser("recipe", help="公式サイトのデッキレシピURLから複数デッキを変換")
    r.add_argument("url", help="例: https://cf-vanguard.com/deckrecipe/.../ .../")
    r.add_argument("-o", "--output", default="output")
    r.add_argument("--refresh", action="store_true", help="キャッシュ済み画像を再取得")
    r.add_argument("--install", action="store_true", help="WindowsのTTS SavesへJSONをコピー")
    r.add_argument(
        "--cloud-path",
        default=ENV_CLOUD_PATH or DEFAULT_CLOUD_PATH,
        help="Steam Cloud上の保存先パス。{deck_id}を使用可能。環境変数 DECKLOG2TTS_CLOUD_PATH でも設定可",
    )
    r.add_argument(
        "--cloud-cli",
        default=ENV_CLOUD_CLI or "sync-with-cloud",
        help="sync-with-cloudコマンドのパスまたはディレクトリ。環境変数 DECKLOG2TTS_CLOUD_CLI に設定すると省略可",
    )
    r.set_defaults(func=cmd_recipe)
    args = p.parse_args()
    return args.func(args)

if __name__ == "__main__":
    raise SystemExit(main())

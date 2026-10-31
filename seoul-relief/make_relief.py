#!/usr/bin/env python3
"""Seoul marathon relief map -> STL generator (25x25 cm tile, red route, record plate).

Outputs (all in mm, origin at the SW corner, z up), written to --out:
  body.stl     base + terrain + simplified buildings   (white)
  route.stl    raised running route + km markers       (red)
  text.stl     raised record text on the bottom band   (any colour)
  preview.png  shaded preview of the whole tile

Buildings are simplified: only buildings >= --min-height are used, nearby ones merge
(grey dilation to --min-width), and the taller the building the more its height is
exaggerated (--b-min .. --b-max, curve --gamma).
"""
import argparse
import hashlib
import json
import math
import struct
import sys
import time
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from pyproj import Transformer
from scipy import ndimage
from shapely.geometry import LineString, Polygon
from shapely.ops import polygonize, unary_union

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OPENTOPO_URL = "https://api.opentopodata.org/v1/srtm30m"


# --------------------------------------------------------------------------- geometry frame
class Frame:
    """lon/lat <-> model millimetres. The map area is the rectangle
    [0, W] x [band, band + H]; the strip below it is the text band."""

    def __init__(self, route_ll, width_mm, height_mm, band_mm, pad):
        lat_c = (route_ll[:, 0].min() + route_ll[:, 0].max()) / 2
        lon_c = (route_ll[:, 1].min() + route_ll[:, 1].max()) / 2
        epsg = (32600 if lat_c >= 0 else 32700) + int((lon_c + 180) // 6) + 1
        self.fwd = Transformer.from_crs(4326, epsg, always_xy=True)
        self.inv = Transformer.from_crs(epsg, 4326, always_xy=True)
        X, Y = self.fwd.transform(route_ll[:, 1], route_ll[:, 0])
        w_m = (X.max() - X.min()) * (1 + pad)
        h_m = (Y.max() - Y.min()) * (1 + pad)
        self.m_per_mm = max(w_m / width_mm, h_m / height_mm, 1e-6)
        cx, cy = (X.max() + X.min()) / 2, (Y.max() + Y.min()) / 2
        self.x0 = cx - width_mm * self.m_per_mm / 2
        self.y0 = cy - height_mm * self.m_per_mm / 2
        self.W, self.H, self.band = width_mm, height_mm, band_mm

    def ll_to_mm(self, lon, lat):
        X, Y = self.fwd.transform(lon, lat)
        return (np.asarray(X) - self.x0) / self.m_per_mm, (np.asarray(Y) - self.y0) / self.m_per_mm + self.band

    def mm_to_ll(self, x, y):
        lon, lat = self.inv.transform(self.x0 + np.asarray(x) * self.m_per_mm,
                                      self.y0 + (np.asarray(y) - self.band) * self.m_per_mm)
        return np.asarray(lon), np.asarray(lat)

    def bbox_ll(self):
        xs = [0, self.W, self.W, 0]
        ys = [self.band, self.band, self.band + self.H, self.band + self.H]
        lon, lat = self.mm_to_ll(xs, ys)
        return lat.min(), lon.min(), lat.max(), lon.max()  # S, W, N, E


# --------------------------------------------------------------------------- input data
def parse_gpx(path):
    pts = []
    for el in ET.parse(path).iter():
        if el.tag.rsplit("}", 1)[-1] in ("trkpt", "rtept"):
            pts.append((float(el.get("lat")), float(el.get("lon"))))
    if len(pts) < 2:
        sys.exit(f"{path}: no track points found")
    return np.array(pts)


def parse_height(tags, floor_h, height_key="height", levels_key="building:levels"):
    h = tags.get(height_key)
    if h is not None:
        try:
            return float(str(h).lower().replace("m", "").split(";")[0].strip())
        except ValueError:
            pass
    lv = tags.get(levels_key)
    if lv is not None:
        try:
            return float(str(lv).split(";")[0]) * floor_h
        except ValueError:
            pass
    return None


def int_ge_regex(t):
    """Regex alternatives matching decimal integers >= t (t >= 1), no anchors."""
    s = str(int(t))
    d = len(s)
    alts = [s, f"[1-9][0-9]{{{d},}}"]
    for k in range(d):
        digit = int(s[k])
        if digit < 9:
            lo = digit + 1 if k else max(digit + 1, 1)
            alts.append(f"{s[:k]}[{lo}-9][0-9]{{{d - k - 1}}}")
    return "|".join(alts)


def overpass_post(query, url, cache_dir):
    import requests
    cache_dir.mkdir(parents=True, exist_ok=True)
    f = cache_dir / f"overpass_{hashlib.sha1(query.encode()).hexdigest()[:16]}.json"
    if f.exists():
        return json.loads(f.read_text())
    for attempt in range(4):
        r = requests.post(url, data={"data": query}, timeout=420)
        if r.status_code == 200:
            f.write_text(r.text)
            return r.json()
        if r.status_code in (429, 504):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
    raise RuntimeError(f"Overpass failed ({r.status_code}); try --overpass <other mirror> or --buildings file.geojson")


def split_bbox(bbox, n):
    s, w, nn, e = bbox
    for i in range(n):
        for j in range(n):
            yield (s + (nn - s) * i / n, w + (e - w) * j / n,
                   s + (nn - s) * (i + 1) / n, w + (e - w) * (j + 1) / n)


def fetch_osm_buildings(frame, a, out):
    seen, result = set(), []
    lv = math.ceil(a.min_height / a.floor_height)
    for k, bb in enumerate(split_bbox(frame.bbox_ll(), a.tiles)):
        s, w, n, e = bb
        box = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
        q = (f'[out:json][timeout:300];('
             f'way["building"]["height"~"^({int_ge_regex(max(int(a.min_height), 1))})([.][0-9]+)?( ?m)?$"]{box};'
             f'way["building"]["building:levels"~"^({int_ge_regex(max(lv, 1))})$"]{box};'
             f');out geom;')
        print(f"  overpass buildings tile {k + 1}/{a.tiles ** 2}", flush=True)
        for el in overpass_post(q, a.overpass, out / "cache")["elements"]:
            if el["type"] != "way" or el["id"] in seen or "geometry" not in el:
                continue
            seen.add(el["id"])
            h = parse_height(el.get("tags", {}), a.floor_height)
            if h:
                result.append((np.array([(p["lon"], p["lat"]) for p in el["geometry"]]), h))
    return result


def load_geojson_buildings(path, a):
    result = []
    for ft in json.loads(Path(path).read_text())["features"]:
        h = parse_height(ft.get("properties") or {}, a.floor_height, a.height_field, a.levels_field)
        g = ft["geometry"]
        if not h or g["type"] not in ("Polygon", "MultiPolygon"):
            continue
        polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        result += [(np.array(p[0])[:, :2], h) for p in polys]
    return result


def parse_water(elements):
    """Closed ways + multipolygon relations (outer minus inner, so islands like Yeouido stay land)."""
    shapes = []
    for el in elements:
        if el["type"] == "way" and el.get("geometry"):
            pts = [(p["lon"], p["lat"]) for p in el["geometry"]]
            if len(pts) >= 4 and pts[0] == pts[-1]:
                shapes.append(Polygon(pts))
        elif el["type"] == "relation":
            def lines(role):
                return [LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
                        for m in el.get("members", [])
                        if m.get("type") == "way" and m.get("role") == role and len(m.get("geometry") or []) > 1]
            outer = list(polygonize(unary_union(lines("outer")))) if lines("outer") else []
            if not outer:
                continue
            shape = unary_union(outer)
            inner = list(polygonize(unary_union(lines("inner")))) if lines("inner") else []
            if inner:
                shape = shape.difference(unary_union(inner))
            shapes.append(shape)
    polys = []
    for s in shapes:
        s = s if s.is_valid else s.buffer(0)
        polys += list(s.geoms) if hasattr(s, "geoms") else [s]
    return [p for p in polys if not p.is_empty]


def fetch_osm_water(frame, a, out):
    s, w, n, e = frame.bbox_ll()
    box = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
    q = ('[out:json][timeout:300];('
         f'way["natural"="water"]{box};relation["natural"="water"]{box};'
         f'way["waterway"="riverbank"]{box};relation["waterway"="riverbank"]{box};'
         ');out geom;')
    print("  overpass water", flush=True)
    return parse_water(overpass_post(q, a.overpass, out / "cache")["elements"])


def fetch_opentopo(lat, lon, cache_dir):
    import requests
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(np.concatenate([lat, lon]).tobytes()).hexdigest()[:16]
    f = cache_dir / f"terrain_{key}.npy"
    if f.exists():
        return np.load(f)
    res = np.zeros(len(lat))
    for k in range(0, len(lat), 100):
        loc = "|".join(f"{la:.5f},{lo:.5f}" for la, lo in zip(lat[k:k + 100], lon[k:k + 100]))
        for attempt in range(5):
            r = requests.get(OPENTOPO_URL, params={"locations": loc, "interpolation": "bilinear"}, timeout=60)
            if r.status_code == 200:
                break
            time.sleep(2 * (attempt + 1))
        r.raise_for_status()
        res[k:k + 100] = [x["elevation"] if x["elevation"] is not None else np.nan for x in r.json()["results"]]
        time.sleep(1.1)  # public API: 1 request / second
    np.save(f, res)
    return res


# --------------------------------------------------------------------------- rasterising
def rasterize_buildings(buildings, frame, nx, ny, res):
    """Max-height grid in metres at vertex resolution (row 0 = south)."""
    img = Image.new("F", (nx, ny), 0.0)
    if not buildings:
        return np.zeros((ny, nx), np.float32)
    d = ImageDraw.Draw(img)
    allp = np.concatenate([b[0] for b in buildings])
    x, y = frame.ll_to_mm(allp[:, 0], allp[:, 1])
    u, v = x / res, y / res
    cuts = np.cumsum([len(b[0]) for b in buildings])[:-1]
    heights = np.array([b[1] for b in buildings])
    us, vs = np.split(u, cuts), np.split(v, cuts)
    for k in np.argsort(heights):  # ascending, so taller buildings overwrite
        pts = list(zip(us[k], vs[k]))
        d.polygon(pts, fill=float(heights[k]))
        d.point((float(us[k].mean()), float(vs[k].mean())), fill=float(heights[k]))
    return np.array(img, dtype=np.float32)


def rasterize_water(polys, frame, nx, ny, res):
    img = Image.new("L", (nx, ny), 0)
    d = ImageDraw.Draw(img)
    for p in polys:
        for ring, fill in [(p.exterior, 255)] + [(r, 0) for r in p.interiors]:
            c = np.asarray(ring.coords)
            x, y = frame.ll_to_mm(c[:, 0], c[:, 1])
            if len(c) >= 3 and (fill == 0 or abs(np.ptp(x)) / res + abs(np.ptp(y)) / res > 2):
                d.polygon(list(zip(x / res, y / res)), fill=fill)
    return np.asarray(img) > 127


def disk(r):
    r = max(int(math.ceil(r)), 1)
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return xx ** 2 + yy ** 2 <= r * r + 0.25


def map_building_heights(h_m, a):
    """metres -> millimetres of relief; taller buildings are exaggerated more."""
    t = np.clip((h_m - a.min_height) / max(a.max_height - a.min_height, 1e-6), 0, 1)
    return np.where(h_m >= a.min_height, a.b_min + (a.b_max - a.b_min) * t ** a.gamma, 0.0).astype(np.float32)


# --------------------------------------------------------------------------- meshing
def heightfield_solid(z, res):
    """Closed, manifold solid: top surface z[i, j] at (j*res, i*res), flat bottom at z=0."""
    ny, nx = z.shape
    jj, ii = np.meshgrid(np.arange(nx), np.arange(ny))
    top = np.stack([jj * res, ii * res, z], -1).reshape(-1, 3)
    idx = np.arange(ny * nx).reshape(ny, nx)
    v00, v01, v11, v10 = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    faces = [np.stack([v00, v01, v11], 1), np.stack([v00, v11, v10], 1)]
    per = np.concatenate([idx[0, :-1], idx[:-1, -1], idx[-1, :0:-1], idx[:0:-1, 0]])  # CCW from above
    L = len(per)
    pv = top[per].copy()
    pv[:, 2] = 0
    centre = np.array([[(nx - 1) * res / 2, (ny - 1) * res / 2, 0.0]])
    verts = np.concatenate([top, pv, centre])
    b = ny * nx + np.arange(L)
    bn = np.roll(b, -1)
    u, vv = per, np.roll(per, -1)
    faces += [np.stack([b, bn, vv], 1), np.stack([b, vv, u], 1)]  # outward skirt
    faces.append(np.stack([np.full(L, ny * nx + L), bn, b], 1))  # bottom fan, normal -z
    return verts[np.concatenate(faces)].astype(np.float32)


def column_solid(zb, zt, mask, res, ox=0.0, oy=0.0, eps=1e-6):
    """One box per masked cell; hidden faces between touching boxes are dropped."""
    ii, jj = np.nonzero(mask)
    if len(ii) == 0:
        return np.zeros((0, 3, 3), np.float32)
    M, ZB, ZT = np.pad(mask, 1), np.pad(zb, 1), np.pad(zt, 1)
    x0, y0 = ox + jj * res, oy + ii * res
    x1, y1 = x0 + res, y0 + res
    b, t = zb[ii, jj], zt[ii, jj]
    out = []

    def P(x, y, z):
        return np.stack([x, y, z], 1)

    def quad(p0, p1, p2, p3):
        out.append(np.stack([p0, p1, p2], 1))
        out.append(np.stack([p0, p2, p3], 1))

    quad(P(x0, y0, t), P(x1, y0, t), P(x1, y1, t), P(x0, y1, t))
    quad(P(x0, y0, b), P(x0, y1, b), P(x1, y1, b), P(x1, y0, b))
    for di, dj in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        nm = M[ii + 1 + di, jj + 1 + dj]
        nb, nt = ZB[ii + 1 + di, jj + 1 + dj], ZT[ii + 1 + di, jj + 1 + dj]
        hi1 = np.where(nm, np.minimum(t, nb), t)
        spans = [(b, hi1, hi1 > b + eps), (np.maximum(b, nt), t, nm & (t > np.maximum(b, nt) + eps))]
        for lo, hi, ok in spans:
            if not ok.any():
                continue
            X0, X1, Y0, Y1, l, h = x0[ok], x1[ok], y0[ok], y1[ok], lo[ok], hi[ok]
            if (di, dj) == (0, 1):
                quad(P(X1, Y0, l), P(X1, Y1, l), P(X1, Y1, h), P(X1, Y0, h))
            elif (di, dj) == (0, -1):
                quad(P(X0, Y1, l), P(X0, Y0, l), P(X0, Y0, h), P(X0, Y1, h))
            elif (di, dj) == (1, 0):
                quad(P(X1, Y1, l), P(X0, Y1, l), P(X0, Y1, h), P(X1, Y1, h))
            else:
                quad(P(X0, Y0, l), P(X1, Y0, l), P(X1, Y0, h), P(X0, Y0, h))
    return np.concatenate(out).astype(np.float32)


def write_stl(path, tris):
    tris = np.asarray(tris, np.float32)
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-20)
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"], rec["v"] = n, tris
    with open(path, "wb") as f:
        f.write(b"seoul-relief".ljust(80, b"\0"))
        f.write(struct.pack("<I", len(tris)))
        rec.tofile(f)


# --------------------------------------------------------------------------- route + text
def route_geometry(route_ll, frame, a, ny, nx, res, sup):
    """Route line + km-marker masks on a grid `sup` times finer than the body grid."""
    x, y = frame.ll_to_mm(route_ll[:, 1], route_ll[:, 0])
    rs = res / sup
    h, w = (ny - 1) * sup, (nx - 1) * sup
    line, dots = Image.new("L", (w, h), 0), Image.new("L", (w, h), 0)
    ld, dd = ImageDraw.Draw(line), ImageDraw.Draw(dots)
    px = list(zip(x / rs, y / rs))
    wpx = max(a.route_width / rs, 1.0)
    ld.line(px, fill=255, width=int(round(wpx)), joint="curve")
    for cx, cy in px[:1] + px[-1:]:  # round the ends
        ld.ellipse([cx - wpx / 2, cy - wpx / 2, cx + wpx / 2, cy + wpx / 2], fill=255)
    cum = np.concatenate([[0], np.cumsum(np.hypot(np.diff(x), np.diff(y)))])
    markers = []
    if a.km_markers > 0:
        step = a.km_markers * 1000 / frame.m_per_mm
        markers = [(np.interp(s, cum, x), np.interp(s, cum, y), 1.0) for s in np.arange(step, cum[-1] - step / 3, step)]
    markers += [(x[0], y[0], 1.5), (x[-1], y[-1], 1.5)]  # start / finish are larger
    for mx, my, k in markers:
        r = a.route_width * 0.95 * k / rs
        dd.ellipse([mx / rs - r, my / rs - r, mx / rs + r, my / rs + r], fill=255)
    L, D = np.asarray(line) > 127, np.asarray(dots) > 127
    return L | D, D, cum[-1] * frame.m_per_mm / 1000


def load_font(path, size):
    cands = ([path] if path else []) + [
        "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc",
        "C:/Windows/Fonts/malgunbd.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in cands:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def text_mask(lines, width_mm, band_mm, res_t, font_path, margin_mm=3.0):
    w, h = int(round(width_mm / res_t)), int(round(band_mm / res_t))
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    m = margin_mm / res_t
    slot = (h - 2 * m) / len(lines)
    for k, line in enumerate(lines):
        size = max(int(slot * 0.72), 8)
        while True:
            font = load_font(font_path, size)
            bb = d.textbbox((0, 0), line, font=font)
            if bb[2] - bb[0] <= w * 0.92 or size <= 8:
                break
            size = int(size * 0.93)
        d.text((w / 2 - (bb[0] + bb[2]) / 2, m + slot * (k + 0.5) - (bb[1] + bb[3]) / 2), line, fill=255, font=font)
    return np.flipud(np.asarray(img) > 127)  # row 0 = south


# --------------------------------------------------------------------------- demo data
def demo_data():
    """Offline sample: a made-up Sangam -> Jamsil route over synthetic Seoul-ish terrain/buildings."""
    wp = np.array([(37.5683, 126.8972), (37.5640, 126.9120), (37.5560, 126.9280), (37.5490, 126.9460),
                   (37.5560, 126.9700), (37.5700, 126.9760), (37.5700, 127.0000), (37.5600, 127.0200),
                   (37.5470, 127.0400), (37.5300, 127.0600), (37.5122, 127.0723)])
    t = np.linspace(0, 1, 400)
    s = np.linspace(0, 1, len(wp))
    route = np.stack([np.interp(t, s, wp[:, 0]), np.interp(t, s, wp[:, 1])], 1)
    rng = np.random.default_rng(7)
    clusters = [(37.5000, 127.0300, 140), (37.5219, 126.9245, 80), (37.5800, 126.8900, 40),
                (37.5700, 126.9800, 70), (37.5126, 127.1026, 40), (37.5440, 127.0560, 50)]
    buildings = []
    for la, lo, n in clusters:
        for _ in range(n):
            dy, dx = rng.normal(0, 700, 2)
            w, d = rng.uniform(30, 90, 2)
            la0, lo0 = la + dy / 111000, lo + dx / (111000 * math.cos(math.radians(la)))
            ring = np.array([(lo0, la0), (lo0 + w / 88000, la0), (lo0 + w / 88000, la0 + d / 111000),
                             (lo0, la0 + d / 111000), (lo0, la0)])
            buildings.append((ring, float(np.clip(rng.lognormal(math.log(75), 0.55), 25, 330))))
    ring = lambda lo, la, w, d: np.array([(lo, la), (lo + w / 88000, la), (lo + w / 88000, la + d / 111000),
                                          (lo, la + d / 111000), (lo, la)])
    buildings.append((ring(127.1026, 37.5126, 70, 70), 555.0))  # an outlier, to test the height cap
    river = LineString([(126.86, 37.5700), (126.8950, 37.5560), (126.9200, 37.5400), (126.9450, 37.5300),
                        (126.9800, 37.5170), (127.0300, 37.5190), (127.0900, 37.5300), (127.14, 37.5340)])
    water = [river.buffer(0.0032)]
    hills = [(37.5512, 126.9882, 270, .012), (37.6588, 126.9770, 700, .035), (37.58, 126.958, 300, .01),
             (37.4450, 126.9636, 600, .03), (37.5510, 127.1030, 285, .012)]

    def elev(lat, lon):
        e = np.full(np.shape(lat), 15.0)
        for la, lo, h, sg in hills:
            e = e + h * np.exp(-((lat - la) ** 2 + (lon - lo) ** 2) / (2 * sg ** 2))
        return e
    return route, buildings, water, elev


# --------------------------------------------------------------------------- preview
def render_preview(total, route_mask, text_m, res, path, scale=2):
    gy, gx = np.gradient(total, res)
    n = np.stack([-gx * 2.0, -gy * 2.0, np.ones_like(total)], -1)
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    light = np.array([-0.5, 0.6, 0.8])
    shade = np.clip(n @ (light / np.linalg.norm(light)), 0, 1)
    g = 0.30 + 0.70 * shade
    rgb = np.stack([g, g, g], -1)
    rgb[route_mask] = np.array([0.86, 0.10, 0.13]) * (0.55 + 0.45 * shade[route_mask, None])
    rgb[text_m] = np.array([0.35, 0.35, 0.40])
    img = Image.fromarray((np.flipud(rgb) * 255).astype(np.uint8))
    img.resize((img.width * scale, img.height * scale), Image.LANCZOS).save(path)


# --------------------------------------------------------------------------- main
def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gpx", help="route (GPX from your watch, or the official course)")
    p.add_argument("--demo", action="store_true", help="offline sample data (no GPX / network needed)")
    p.add_argument("--out", default="out")
    g = p.add_argument_group("tile")
    g.add_argument("--size", type=float, default=250, help="tile edge in mm (default 250)")
    g.add_argument("--band", type=float, default=30, help="text band at the bottom, mm (0 = none)")
    g.add_argument("--res", type=float, default=0.5, help="grid resolution in mm (0.4 = finer, bigger files)")
    g.add_argument("--base", type=float, default=3.0, help="plate thickness, mm")
    g.add_argument("--pad", type=float, default=0.10, help="margin around the route, fraction")
    g = p.add_argument_group("buildings")
    g.add_argument("--buildings", help="GeoJSON in lon/lat instead of OpenStreetMap")
    g.add_argument("--height-field", default="height")
    g.add_argument("--levels-field", default="building:levels")
    g.add_argument("--floor-height", type=float, default=3.2, help="m per floor when only levels are known")
    g.add_argument("--min-height", type=float, default=40, help="ignore buildings lower than this (m)")
    g.add_argument("--max-height", type=float, default=300, help="heights above this are capped (m)")
    g.add_argument("--b-min", type=float, default=1.0, help="relief of the shortest kept building, mm")
    g.add_argument("--b-max", type=float, default=14.0, help="relief of the tallest building, mm")
    g.add_argument("--gamma", type=float, default=0.8, help="<1 lifts mid-height buildings, >1 favours only the tallest")
    g.add_argument("--min-width", type=float, default=1.2, help="smallest printable footprint, mm (merges neighbours)")
    g.add_argument("--tiles", type=int, default=3, help="split the Overpass request into n x n tiles")
    g.add_argument("--overpass", default=OVERPASS_URL)
    g = p.add_argument_group("terrain")
    g.add_argument("--terrain", choices=["opentopo", "flat"], default="opentopo")
    g.add_argument("--terrain-exag", type=float, default=2.5)
    g.add_argument("--terrain-step", type=float, default=450, help="elevation sample spacing, m")
    g.add_argument("--water-depth", type=float, default=0.6, help="how far water lies below land, mm")
    g.add_argument("--no-water", action="store_true")
    g = p.add_argument_group("route")
    g.add_argument("--route-width", type=float, default=1.6, help="mm")
    g.add_argument("--route-height", type=float, default=0.8, help="mm above the surface")
    g.add_argument("--km-markers", type=float, default=5, help="marker every N km (0 = off)")
    g = p.add_argument_group("record text")
    g.add_argument("--line", action="append", help="text line (repeat). e.g. --line 'JTBC SEOUL MARATHON 2026' --line '42.195 km  3:45:12'")
    g.add_argument("--font", help="TTF/TTC path (needed for Korean unless Nanum/Noto is installed)")
    g.add_argument("--text-height", type=float, default=1.0, help="mm")
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    elev_fn = None
    if a.demo:
        route_ll, buildings, water_polys, elev_fn = demo_data()
    elif a.gpx:
        route_ll, buildings, water_polys = parse_gpx(a.gpx), None, None
    else:
        sys.exit("give --gpx FILE (or --demo)")

    lines = a.line or []
    band = a.band if lines or a.band == 0 else 0
    if lines and band == 0:
        sys.exit("--line needs --band > 0")
    W, Hmap = a.size, a.size - band
    frame = Frame(route_ll, W, Hmap, band, a.pad)
    nx, ny = int(round(W / a.res)) + 1, int(round(a.size / a.res)) + 1
    yy = np.arange(ny)[:, None] * a.res
    map_rows = np.broadcast_to(yy >= band - 1e-6, (ny, nx))
    print(f"scale 1:{frame.m_per_mm * 1000:,.0f}  ({frame.m_per_mm:.0f} m per mm)  grid {nx}x{ny}")

    # buildings
    if buildings is None:
        print("buildings...")
        buildings = load_geojson_buildings(a.buildings, a) if a.buildings else fetch_osm_buildings(frame, a, out)
    buildings = [b for b in buildings if b[1] >= a.min_height]
    print(f"  {len(buildings)} buildings >= {a.min_height:.0f} m")
    h_m = rasterize_buildings(buildings, frame, nx, ny, a.res)
    h_m[~map_rows] = 0

    # water
    water = np.zeros((ny, nx), bool)
    if not a.no_water:
        if water_polys is None:
            print("water...")
            try:
                water_polys = fetch_osm_water(frame, a, out)
            except Exception as e:  # noqa: BLE001
                print(f"  WARNING: water fetch failed ({e}); continuing without water")
                water_polys = []
        water = rasterize_water(water_polys, frame, nx, ny, a.res) & map_rows

    # terrain
    terrain = np.zeros((ny, nx), np.float32)
    if elev_fn is not None or a.terrain == "opentopo":
        print("terrain...")
        ncx = max(int(W * frame.m_per_mm / a.terrain_step) + 1, 8)
        ncy = max(int(Hmap * frame.m_per_mm / a.terrain_step) + 1, 8)
        gx, gy = np.meshgrid(np.linspace(0, W, ncx), np.linspace(band, band + Hmap, ncy))
        lon, lat = frame.mm_to_ll(gx.ravel(), gy.ravel())
        try:
            e = elev_fn(lat, lon) if elev_fn else fetch_opentopo(lat, lon, out / "cache")
            e = e.reshape(ncy, ncx)
            e = np.where(np.isnan(e), np.nanmin(e), e)
            vy = (np.arange(ny)[:, None] * a.res - band) / Hmap * (ncy - 1)
            vx = np.arange(nx)[None, :] * a.res / W * (ncx - 1)
            full = ndimage.map_coordinates(e, [np.broadcast_to(vy, (ny, nx)), np.broadcast_to(vx, (ny, nx))],
                                           order=3, mode="nearest")
            full = ndimage.gaussian_filter(full, 2.0)
            terrain = np.clip((full - np.percentile(full[map_rows], 1)) * a.terrain_exag / frame.m_per_mm, 0, None)
        except Exception as ex:  # noqa: BLE001
            print(f"  WARNING: terrain fetch failed ({ex}); using FLAT terrain")
    surf = np.full((ny, nx), a.base, np.float32)
    land = map_rows & ~water
    surf[land] += terrain[land] + a.water_depth

    # route
    sup = 2
    route_m, dots_m, km = route_geometry(route_ll, frame, a, ny, nx, a.res, sup)
    print(f"route {km:.1f} km")

    # buildings -> relief: merge + widen thin ones, then keep the route clear
    rm = np.asarray(Image.fromarray(route_m.astype(np.uint8) * 255).resize((nx - 1, ny - 1), Image.BOX)) > 40
    clear = ndimage.binary_dilation(np.pad(rm, ((0, 1), (0, 1))), iterations=2)
    bld = map_building_heights(ndimage.grey_dilation(h_m, footprint=disk(a.min_width / 2 / a.res)), a)
    bld[clear | water] = 0
    total = surf + bld
    print(f"  tallest point {total.max():.1f} mm, buildings {bld.max():.1f} mm, terrain {terrain.max():.1f} mm")

    # meshes
    write_stl(out / "body.stl", heightfield_solid(total, a.res))
    rs = a.res / sup
    cy, cx = np.mgrid[0:route_m.shape[0], 0:route_m.shape[1]]
    z0 = ndimage.map_coordinates(surf, [(cy + 0.5) / sup, (cx + 0.5) / sup], order=1, mode="nearest")
    zt = z0 + a.route_height + np.where(dots_m, a.route_height, 0.0)
    write_stl(out / "route.stl", column_solid(z0 - 0.3, zt, route_m, rs))
    tmask = np.zeros((1, 1), bool)
    if lines:
        res_t = 0.2
        tmask = text_mask(lines, W, band, res_t, a.font)
        ones = np.full(tmask.shape, a.base)
        write_stl(out / "text.stl", column_solid(ones - 0.2, ones + a.text_height, tmask, res_t))

    # preview
    big_route = np.asarray(Image.fromarray(route_m.astype(np.uint8) * 255).resize((nx, ny), Image.BOX)) > 40
    tm = np.zeros((ny, nx), bool)
    if lines:
        tm_img = Image.fromarray(tmask.astype(np.uint8) * 255).resize((nx, int(round(band / a.res))), Image.BOX)
        tm[: int(round(band / a.res))] = np.asarray(tm_img) > 100
    preview_z = total + big_route * a.route_height * 2 + tm * a.text_height
    render_preview(preview_z, big_route, tm, a.res, out / "preview.png")
    print(f"wrote body.stl route.stl{' text.stl' if lines else ''} preview.png -> {out}/")


if __name__ == "__main__":
    main()

"""Fetch and stitch Esri World Imagery XYZ tiles covering a lon/lat bbox,
with pixel <-> lon/lat conversion for the stitched mosaic (standard Web
Mercator slippy-map tile math)."""
import math
from pathlib import Path

import requests
from PIL import Image, ImageDraw

TILE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
TILE_SIZE = 256
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "fortyguard" / "_tile_cache"


def deg2tile(lon, lat, zoom):
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    return x, y


def tile2deg(x, y, zoom):
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * y / n)))
    return lon, math.degrees(lat_rad)


def fetch_tile(z, x, y):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{z}_{x}_{y}.png"
    if cache_path.exists():
        return Image.open(cache_path).convert("RGB")
    resp = requests.get(TILE_URL.format(z=z, x=x, y=y), timeout=30)
    resp.raise_for_status()
    cache_path.write_bytes(resp.content)
    return Image.open(cache_path).convert("RGB")


def fetch_mosaic(min_lon, min_lat, max_lon, max_lat, zoom):
    """Returns (PIL.Image mosaic, tile_x0, tile_y0, zoom) where (tile_x0,
    tile_y0) is the tile-space coordinate of the mosaic's top-left pixel."""
    x0f, y0f = deg2tile(min_lon, max_lat, zoom)  # top-left (max lat, min lon)
    x1f, y1f = deg2tile(max_lon, min_lat, zoom)  # bottom-right (min lat, max lon)
    tx0, ty0 = int(math.floor(x0f)), int(math.floor(y0f))
    tx1, ty1 = int(math.floor(x1f)), int(math.floor(y1f))

    n_x, n_y = tx1 - tx0 + 1, ty1 - ty0 + 1
    mosaic = Image.new("RGB", (n_x * TILE_SIZE, n_y * TILE_SIZE))
    for j, ty in enumerate(range(ty0, ty1 + 1)):
        for i, tx in enumerate(range(tx0, tx1 + 1)):
            mosaic.paste(fetch_tile(zoom, tx, ty), (i * TILE_SIZE, j * TILE_SIZE))
    return mosaic, tx0, ty0, zoom


def pixel_to_lonlat(px, py, tile_x0, tile_y0, zoom):
    return tile2deg(tile_x0 + px / TILE_SIZE, tile_y0 + py / TILE_SIZE, zoom)


def lonlat_to_pixel(lon, lat, tile_x0, tile_y0, zoom):
    x, y = deg2tile(lon, lat, zoom)
    return (x - tile_x0) * TILE_SIZE, (y - tile_y0) * TILE_SIZE


def rasterize_polygon_mask(size, polygon_px):
    """size: (width, height) in pixels. polygon_px: list of (x, y) pixel
    coords. Returns a numpy uint8 array, 255 inside the polygon, 0 outside."""
    import numpy as np
    mask_img = Image.new("L", size, 0)
    ImageDraw.Draw(mask_img).polygon(polygon_px, fill=255)
    return np.array(mask_img)

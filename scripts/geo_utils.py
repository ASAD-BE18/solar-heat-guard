"""Local tangent-plane (equirectangular) projection helpers.

Good to sub-meter accuracy at the scale of a single site (~1 km); not meant
for anything larger. All rows/generators in this project run their geometry
in these local x/y meters, then project back to lon/lat for GeoJSON output.
"""
import math

EARTH_RADIUS_M = 6378137.0


def make_projector(lon0, lat0):
    lat0_rad = math.radians(lat0)
    m_per_deg_lat = math.pi / 180 * EARTH_RADIUS_M
    m_per_deg_lon = m_per_deg_lat * math.cos(lat0_rad)

    def to_local(lon, lat):
        return ((lon - lon0) * m_per_deg_lon, (lat - lat0) * m_per_deg_lat)

    def to_lonlat(x, y):
        return (lon0 + x / m_per_deg_lon, lat0 + y / m_per_deg_lat)

    return to_local, to_lonlat

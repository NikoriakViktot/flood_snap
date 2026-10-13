"""The browser PNG encoding and the Web Mercator grid of the HAND model, without WhiteboxTools."""
from __future__ import annotations

import numpy as np
import pytest

from lab_service.hand import packaging as PK


def test_hand_png_round_trip_is_lossless():
    hand = np.array([[0.0, 0.1, 6553.4, 7000.0], [12.34, 0.05, 2.0, 1.0]], np.float32)
    valid = np.array([[1, 1, 1, 1], [1, 0, 1, 1]], bool)
    channel = np.array([[0, 1, 0, 0], [1, 0, 0, 0]], np.uint8)
    data = PK.png_rgba(PK.encode_hand_png(hand, channel, valid))
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and PK.png_chunks(data) == ["IHDR", "IDAT", "IEND"]
    rgba = PK.read_png_rgba(data)
    h, ch, v = PK.decode_hand_png(rgba)
    assert v.tolist() == valid.tolist()
    assert h[0].tolist() == pytest.approx([0.0, 0.1, 6553.4, 6553.4])      # clamped at 65534 dm
    assert h[1, 0] == pytest.approx(12.3) and h[1, 2] == 2.0
    assert ch.tolist() == [[False, True, False, False], [True, False, False, False]]
    assert rgba[1, 1].tolist() == [0, 0, 0, 0] and set(np.unique(rgba[..., 3]).tolist()) == {0, 255}


def test_aux_png_round_trip_and_catchment_limit():
    slope = np.array([[0.0, 12.25, 90.0], [45.4, 3.0, 0.5]], np.float32)
    catch = np.array([[1, 300, 65534], [2, 0, 7]], np.int32)
    valid = np.ones((2, 3), bool)
    valid[1, 1] = False
    rgba = PK.read_png_rgba(PK.png_rgba(PK.encode_aux_png(slope, catch, valid)))
    s, c, v = PK.decode_aux_png(rgba)
    assert v.tolist() == valid.tolist() and c[0].tolist() == [1, 300, 65534] and c[1, 1] == 0
    assert s[0].tolist() == pytest.approx([0.0, 12.0, 90.0]) and s[1, 0] == pytest.approx(45.5)   # rint(24.5) = 24
    with pytest.raises(Exception, match="catchments"):
        PK.encode_aux_png(slope, np.full((2, 3), 70000, np.int32), valid)


def test_mercator_grid_is_aligned_to_zoom_12_tiles():
    g = PK.mercator_grid([5_400_000, 2_900_000, 5_403_000, 2_902_400], "EPSG:3035")
    res = 2 * PK.ORIGIN / (256 * 2 ** 12)
    assert g["res_m"] == pytest.approx(38.21851414258813)
    t = g["transform"]
    for x in (t.c, t.c + g["width"] * res):
        assert abs((x + PK.ORIGIN) / res - round((x + PK.ORIGIN) / res)) < 1e-6
    for y in (t.f, t.f - g["height"] * res):
        assert abs((PK.ORIGIN - y) / res - round((PK.ORIGIN - y) / res)) < 1e-6
    assert g["width"] * res == pytest.approx(g["bounds_3857"][2] - g["bounds_3857"][0], abs=1e-3)
    w, s, e, n = g["bounds"]
    assert 20 < w < e < 30 and 44 < s < n < 53                            # somewhere over Ukraine


def test_cog_translate_writes_a_verified_cog(tmp_path):
    import shutil
    if not shutil.which("gdal_translate"):
        pytest.skip("gdal_translate not on PATH")
    from rasterio.transform import from_origin
    a = np.random.default_rng(1).random((300, 280)).astype(np.float32) * 10
    a[:5, :] = -9999
    src = PK.write_gtiff(tmp_path / "a.tif", a, from_origin(5_400_000, 2_900_000, 30, 30), "EPSG:3035", -9999, "float32")
    info = PK.verify_cog(PK.cog_translate(src, tmp_path / "a.cog.tif", "float32"))
    assert info["layout"] == "COG" and info["block"] == [256, 256] and info["overviews"] == [2] and info["nodata"] == -9999

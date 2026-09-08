"""Quick check: visualize the compound water mask GeoTIFF with alpha channel support.

Compound mask: (σ⁰_VV < 0.017) AND (slope < 8°) AND (HAND < 25m)
"""

import sys
from pathlib import Path

import rasterio
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap

tif = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
    "data/water_mask_S1B_IW_GRDH_1SDV_20200611T043639_20200611T043704_021981_029B85_067B.tif"
)

with rasterio.open(tif) as src:
    mask_band = src.read(1)
    bounds = src.bounds
    crs = src.crs
    n_bands = src.count

    # Alpha channel from gdalwarp -dstalpha (band 2)
    if n_bands >= 2:
        alpha = src.read(2)
        inside = alpha > 0
    else:
        inside = np.ones_like(mask_band, dtype=bool)

    print(f"File:   {tif.name}")
    print(f"Size:   {src.width} x {src.height} ({n_bands} bands)")
    print(f"CRS:    {crs}")
    print(f"Bounds: W={bounds.left:.4f} S={bounds.bottom:.4f} E={bounds.right:.4f} N={bounds.top:.4f}")

    # Stats only within basin (alpha > 0)
    data_inside = mask_band[inside]
    water = np.count_nonzero(data_inside == 1)
    land = np.count_nonzero(data_inside == 0)
    total_inside = data_inside.size
    outside = mask_band.size - total_inside

    print(f"\nWithin basin ({total_inside:,} px):")
    print(f"  Water:   {water:,} px ({100*water/total_inside:.1f}%)")
    print(f"  Land:    {land:,} px ({100*land/total_inside:.1f}%)")
    print(f"  Outside: {outside:,} px (masked by alpha)")

    # Build 3-class display: 0=outside(white), 1=land(tan), 2=water(blue)
    display = np.full_like(mask_band, 0, dtype=np.uint8)  # outside
    display[inside & (mask_band == 0)] = 1  # land
    display[inside & (mask_band == 1)] = 2  # water

    cmap = ListedColormap(["#f0f0f0", "#c8b878", "#2166ac"])  # outside, land, water

    fig, ax = plt.subplots(1, 1, figsize=(10, 10))
    extent = [bounds.left, bounds.right, bounds.bottom, bounds.top]
    ax.imshow(display, extent=extent, cmap=cmap, vmin=0, vmax=2,
              origin="upper", interpolation="nearest")
    ax.set_title(f"Compound mask (σ⁰<0.017 ∧ slope<8° ∧ HAND<25m) — {water/total_inside*100:.1f}% water\n{tif.stem}",
                 fontsize=11)
    ax.set_xlabel("Longitude")
    ax.set_ylabel("Latitude")

    # Legend
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor="#2166ac", label=f"Water ({water:,} px)"),
        Patch(facecolor="#c8b878", label=f"Land ({land:,} px)"),
        Patch(facecolor="#f0f0f0", edgecolor="gray", label="Outside basin"),
    ]
    ax.legend(handles=legend_elements, loc="lower left", fontsize=9)

    out_png = tif.with_suffix(".png")
    fig.savefig(out_png, dpi=150, bbox_inches="tight")
    print(f"\nSaved:  {out_png}")
    plt.show()

from types import SimpleNamespace as NS

import pytest

from ugv_tools_api.supervisor.video_io import FrameCache, render_costmap_png


def test_get_empty():
    c = FrameCache()
    assert c.get() is None
    assert c.received == 0


def test_set_updates_latest_and_counter():
    c = FrameCache()
    c.set(b"frame1")
    c.set(b"frame2")
    assert c.get() == b"frame2"
    assert c.received == 2


def test_framecache_repr_includes_counts():
    c = FrameCache()
    assert "received=0" in repr(c)
    c.set(b"xxxx")
    r = repr(c)
    assert "received=1" in r
    assert "bytes=4" in r


def _make_grid(width: int, height: int, data) -> NS:
    origin = NS(
        position=NS(x=0.0, y=0.0, z=0.0),
        orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0),
    )
    info = NS(width=width, height=height, resolution=0.1, origin=origin)
    return NS(info=info, data=data)


def test_render_occupancy_grid_to_png():
    """Rendering a synthetic occupancy grid must yield a valid PNG blob
    that exercises every color branch: free, unknown, lethal, and the
    inflation gradient (cells with values 1..99).
    """
    inflation = list(range(1, 100, 2))[:50]  # 1, 3, 5, ..., 99
    data = [0] * 20 + [100] * 10 + [-1] * 20 + inflation
    assert len(data) == 100  # 10 x 10
    msg = _make_grid(10, 10, data)

    png = render_costmap_png(msg)
    assert png[:8] == b'\x89PNG\r\n\x1a\n'  # PNG magic
    # Varied pixel content defeats zlib compression below ~120-200 bytes;
    # 100 is a safe floor that confirms real IDAT chunks are present.
    assert len(png) > 100


def test_render_rejects_mismatched_data_length():
    """len(data) != width*height must raise a diagnostic ValueError."""
    msg = _make_grid(10, 10, [0] * 50)  # claims 100 cells, has 50
    with pytest.raises(ValueError, match="data length 50 does not match"):
        render_costmap_png(msg)

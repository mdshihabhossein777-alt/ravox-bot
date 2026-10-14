def _load_font(size):
    """Multi-fallback font loader for Pillow 12+."""
    candidates = [
        "fonts/Poppins-Bold.ttf",
        "fonts/Inter-Bold.ttf",
        "arial.ttf",
        "DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)   # Pillow 12 supports size param
import io
import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter


THEMES = {
    "galaxy": {
        "bg1": (15, 15, 40),
        "bg2": (75, 30, 140),
        "accent": (167, 139, 250),
        "text": (255, 255, 255),
        "subtext": (200, 190, 240),
    },
    "neon": {
        "bg1": (5, 5, 15),
        "bg2": (0, 100, 120),
        "accent": (0, 255, 200),
        "text": (255, 255, 255),
        "subtext": (255, 100, 200),
    },
    "dark": {
        "bg1": (15, 15, 20),
        "bg2": (45, 45, 55),
        "accent": (180, 180, 200),
        "text": (255, 255, 255),
        "subtext": (150, 150, 160),
    },
    "fire": {
        "bg1": (40, 5, 0),
        "bg2": (140, 40, 0),
        "accent": (255, 120, 0),
        "text": (255, 240, 200),
        "subtext": (255, 180, 80),
    },
    "ocean": {
        "bg1": (5, 20, 45),
        "bg2": (0, 80, 130),
        "accent": (56, 189, 248),
        "text": (255, 255, 255),
        "subtext": (140, 220, 240),
    },
    "anime": {
        "bg1": (35, 10, 45),
        "bg2": (150, 40, 120),
        "accent": (255, 105, 180),
        "text": (255, 255, 255),
        "subtext": (220, 170, 255),
    },
}

CARD_W = 900
CARD_H = 300
FONT_PATH = "fonts/Poppins-Bold.ttf"


async def _fetch_avatar(url: str) -> Image.Image:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.read()
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    img = ImageOps.fit(img, (size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask=mask)
    return result


def _gradient(w, h, c1, c2):
    base = Image.new("RGB", (w, h), c1)
    top = Image.new("RGB", (w, h), c2)
    mask = Image.new("L", (w, h))
    mask.putdata([int(255 * (y / h)) for y in range(h) for _ in range(w)])
    base.paste(top, (0, 0), mask)
    return base


def _load_font(size):
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        try:
            return ImageFont.truetype("arial.ttf", size)
        except Exception:
            return ImageFont.load_default()


async def generate_welcome_card(
    username: str,
    discriminator: str,
    avatar_url: str,
    member_count: int,
    server_name: str,
    theme: str = "galaxy",
    action: str = "welcome",
) -> discord.File:
    t = THEMES.get(theme, THEMES["galaxy"])

    bg = _gradient(CARD_W, CARD_H, t["bg1"], t["bg2"])
    blurred = bg.filter(ImageFilter.GaussianBlur(radius=25))
    bg = Image.blend(bg, blurred, 0.4)
    draw = ImageDraw.Draw(bg, "RGBA")

    # Decorative circles
    draw.ellipse((CARD_W - 220, -120, CARD_W + 100, 200), fill=(*t["accent"], 40))
    draw.ellipse((-120, CARD_H - 180, 160, CARD_H + 100), fill=(*t["accent"], 30))

    # Avatar
    try:
        avatar = await _fetch_avatar(avatar_url)
    except Exception:
        avatar = Image.new("RGBA", (256, 256), (*t["accent"], 255))

    av_size = 180
    avatar = _circle_crop(avatar, av_size)

    glow_size = av_size + 24
    glow = Image.new("RGBA", (glow_size, glow_size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((0, 0, glow_size, glow_size), fill=(*t["accent"], 200))
    glow = glow.filter(ImageFilter.GaussianBlur(radius=10))

    av_x = 60
    av_y = (CARD_H - av_size) // 2
    bg.paste(glow, (av_x - 12, av_y - 12), glow)
    bg.paste(avatar, (av_x, av_y), avatar)

    font_title = _load_font(46)
    font_sub = _load_font(26)
    font_small = _load_font(18)

    tx = av_x + av_size + 40

    if action == "welcome":
        label, label_color = "WELCOME", t["accent"]
    else:
        label, label_color = "GOODBYE", (255, 90, 90)

    draw.text((tx, 55), label, font=font_small, fill=label_color)
    draw.text((tx, 85), username[:22], font=font_title, fill=t["text"])

    if discriminator and discriminator != "0":
        draw.text((tx, 145), f"#{discriminator}", font=font_sub, fill=t["subtext"])

    draw.text((tx, 180), f"in {server_name[:32]}", font=font_sub, fill=t["subtext"])
    draw.text((tx, 222), f"Member #{member_count}", font=font_small, fill=t["accent"])

    draw.rectangle((0, CARD_H - 6, CARD_W, CARD_H), fill=(*t["accent"], 255))

    buf = io.BytesIO()
    bg.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return discord.File(buf, filename=f"{action}.png")
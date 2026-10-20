"""The Pack Station app icon: a cute bean hugging a parcel, on an Atlas green gradient.
Drawn in code (no image files), so any size comes out sharp."""
from __future__ import annotations

from PIL import Image, ImageChops, ImageDraw, ImageFilter


def bean_icon(size: int) -> Image.Image:
    S = 4 * size                       # draw big, shrink at the end for smooth edges
    u = S / 100                        # 1 unit = 1% of the icon
    def P(x, y): return (x * u, y * u)

    # background: Atlas forest green fading to a lighter green (top-left to bottom-right)
    top, bot = (44, 70, 58), (118, 160, 128)
    ramp = Image.linear_gradient("L").resize((S, S))            # 0 at top -> 255 at bottom
    g = ImageChops.add(ramp.point(lambda v: v // 2), ramp.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_LEFT_RIGHT).point(lambda v: v // 2))
    bg = Image.composite(Image.new("RGB", (S, S), bot), Image.new("RGB", (S, S), top), g)
    # soft glow behind the bean
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse([P(18, 16), P(82, 84)], fill=110)
    glow = glow.filter(ImageFilter.GaussianBlur(9 * u))
    bg = Image.composite(Image.new("RGB", (S, S), (214, 236, 214)), bg, glow)

    # bean body: a fat ellipse with a bite taken out of the left side (kidney shape)
    body = Image.new("L", (S, S), 0)
    bd = ImageDraw.Draw(body)
    bd.ellipse([P(22, 18), P(78, 84)], fill=255)
    bite = Image.new("L", (S, S), 0)
    ImageDraw.Draw(bite).ellipse([P(-14, 30), P(29, 70)], fill=255)   # wide, shallow dent = bean curve
    body = ImageChops.subtract(body, bite)
    body = body.filter(ImageFilter.GaussianBlur(3.5 * u)).point(lambda v: 255 if v > 128 else 0)  # round off the dent's corners
    body = body.rotate(-12, resample=Image.BICUBIC, center=(S / 2, S / 2))

    # drop shadow
    sh = body.filter(ImageFilter.GaussianBlur(3 * u))
    sh = ImageChops.offset(sh, int(1.5 * u), int(3 * u)).point(lambda v: v * 0.45)
    bg = Image.composite(Image.new("RGB", (S, S), (20, 40, 30)), bg, sh)

    # bean colour: warm caramel with a lighter belly gradient
    bean = Image.new("RGB", (S, S), (196, 132, 82))
    belly = Image.new("L", (S, S), 0)
    ImageDraw.Draw(belly).ellipse([P(30, 26), P(74, 72)], fill=255)
    belly = belly.filter(ImageFilter.GaussianBlur(10 * u))
    bean = Image.composite(Image.new("RGB", (S, S), (232, 176, 118)), bean, belly)
    bg = Image.composite(bean, bg, body)

    d = ImageDraw.Draw(bg)
    # shine
    shine = Image.new("L", (S, S), 0)
    ImageDraw.Draw(shine).ellipse([P(58, 25), P(65, 30)], fill=190)
    shine = shine.filter(ImageFilter.GaussianBlur(1.2 * u))
    bg = Image.composite(Image.new("RGB", (S, S), (255, 238, 214)), bg, ImageChops.multiply(shine, body))
    d = ImageDraw.Draw(bg)

    # face
    eye = (52, 34, 28)
    for ex in (44, 60):
        d.ellipse([P(ex - 3.4, 44), P(ex + 3.4, 51.5)], fill=eye)
        d.ellipse([P(ex - 1.6, 45.2), P(ex + 0.6, 47.6)], fill="white")     # eye sparkle
    for cx in (38.5, 66):
        cheek = Image.new("L", (S, S), 0)
        ImageDraw.Draw(cheek).ellipse([P(cx - 4.2, 52), P(cx + 4.2, 57.5)], fill=170)
        cheek = cheek.filter(ImageFilter.GaussianBlur(1.2 * u))
        bg = Image.composite(Image.new("RGB", (S, S), (240, 120, 120)), bg, cheek)
    d = ImageDraw.Draw(bg)
    d.arc([P(48, 50), P(56, 57)], start=20, end=160, fill=eye, width=int(1.6 * u))   # little smile

    # tiny parcel it's hugging
    box = [P(44, 62), P(64, 78)]
    d.rounded_rectangle(box, radius=2 * u, fill=(250, 244, 230), outline=eye, width=int(1.3 * u))
    d.line([P(54, 62), P(54, 78)], fill=(212, 245, 60), width=int(3 * u))           # lime tape
    d.line([P(44, 69.5), P(64, 69.5)], fill=(212, 245, 60), width=int(3 * u))
    d.rounded_rectangle(box, radius=2 * u, outline=eye, width=int(1.3 * u))
    # little arms holding the box
    d.ellipse([P(40, 66), P(47, 72)], fill=(196, 132, 82), outline=eye, width=int(1.1 * u))
    d.ellipse([P(61, 66), P(68, 72)], fill=(196, 132, 82), outline=eye, width=int(1.1 * u))

    return bg.resize((size, size), Image.LANCZOS)

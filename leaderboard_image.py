import io
from PIL import Image, ImageDraw, ImageFont

def get_font(size: int, bold: bool = False):
    font_names = [
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Segoe UI.ttf",
        "FreeSans.ttf"
    ]
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def truncate_str(text: str, max_chars: int = 22) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 2] + ".."

def create_medal_icon(rank: int, size: int = 32) -> Image.Image:
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    
    if rank == 1:
        bg_color = (245, 166, 35)   # Gold
        text_color = (255, 255, 255)
    elif rank == 2:
        bg_color = (155, 155, 155) # Silver
        text_color = (255, 255, 255)
    elif rank == 3:
        bg_color = (205, 127, 50)  # Bronze
        text_color = (255, 255, 255)
    else:
        bg_color = (220, 224, 230)
        text_color = (70, 70, 70)

    draw.ellipse([0, 0, size - 1, size - 1], fill=bg_color)
    
    font = get_font(16, bold=True)
    rank_str = str(rank)
    if font:
        try:
            bbox = font.getbbox(rank_str)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
        except Exception:
            w, h = 10, 12
        draw.text(((size - w) / 2, (size - h) / 2 - 2), rank_str, fill=text_color, font=font)
    
    return img

def generate_leaderboard_image(participants: list, quiz_name: str = "Quiz Result", max_rows: int = 15) -> io.BytesIO:
    display_rows = participants[:max_rows]
    row_count = max(len(display_rows), 1)

    padding = 20
    row_height = 50
    header_height = 55
    card_width = 600
    card_height = header_height + (row_count * row_height) + (padding * 2)

    canvas_bg = (240, 242, 245)
    image = Image.new("RGB", (card_width, card_height), canvas_bg)
    draw = ImageDraw.Draw(image)

    card_margin = 10
    card_box = [
        card_margin,
        card_margin,
        card_width - card_margin,
        card_height - card_margin
    ]
    card_bg = (255, 255, 255)
    border_color = (218, 224, 233)
    
    try:
        draw.rounded_rectangle(card_box, radius=14, fill=card_bg, outline=border_color, width=2)
    except AttributeError:
        draw.rectangle(card_box, fill=card_bg, outline=border_color, width=2)

    header_bg = (245, 247, 250)
    header_box = [
        card_margin + 2,
        card_margin + 2,
        card_width - card_margin - 2,
        card_margin + header_height
    ]
    try:
        draw.rounded_rectangle(header_box, radius=12, fill=header_bg)
    except AttributeError:
        draw.rectangle(header_box, fill=header_bg)

    font_header = get_font(20, bold=True)
    font_body = get_font(18, bold=False)
    font_bold = get_font(18, bold=True)

    col_rank_x = card_margin + 25
    col_name_x = card_margin + 85
    col_correct_x = card_width - card_margin - 130
    col_wrong_x = card_width - card_margin - 50

    draw.text((col_rank_x, card_margin + 16), "#", fill=(80, 90, 105), font=font_header)
    draw.text((col_name_x, card_margin + 16), "Name", fill=(80, 90, 105), font=font_header)
    draw.text((col_correct_x, card_margin + 16), "✅", fill=(34, 139, 34), font=font_header)
    draw.text((col_wrong_x, card_margin + 16), "❌", fill=(220, 20, 60), font=font_header)

    draw.line(
        [(card_margin + 2, card_margin + header_height), (card_width - card_margin - 2, card_margin + header_height)],
        fill=(225, 230, 238),
        width=2
    )

    y_start = card_margin + header_height + 2

    for i, p in enumerate(display_rows, start=1):
        row_y = y_start + ((i - 1) * row_height)
        
        if i % 2 == 0:
            row_bg = (250, 252, 255)
            draw.rectangle([card_margin + 2, row_y, card_width - card_margin - 2, row_y + row_height], fill=row_bg)

        draw.line(
            [(card_margin + 10, row_y + row_height), (card_width - card_margin - 10, row_y + row_height)],
            fill=(238, 242, 246),
            width=1
        )

        if i <= 3:
            medal_img = create_medal_icon(i, size=28)
            image.paste(medal_img, (col_rank_x - 4, row_y + 10), medal_img)
        else:
            draw.text((col_rank_x + 2, row_y + 14), str(i), fill=(100, 110, 125), font=font_bold)

        name_str = truncate_str(p.get("name", "User"), max_chars=22)
        draw.text((col_name_x, row_y + 14), name_str, fill=(30, 35, 45), font=font_body)

        correct_cnt = str(p.get("correct", 0))
        draw.text((col_correct_x + 4, row_y + 14), correct_cnt, fill=(46, 125, 50), font=font_bold)

        wrong_cnt = str(p.get("wrong", 0))
        draw.text((col_wrong_x + 4, row_y + 14), wrong_cnt, fill=(198, 40, 40), font=font_bold)

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output

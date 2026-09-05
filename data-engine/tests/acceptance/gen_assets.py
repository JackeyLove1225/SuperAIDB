# 验收资产生成：从 476 页定额原书切出测试段（幂等，存在即跳过）
from pathlib import Path

import fitz

SRC = Path("uploads/湖北省房屋建筑与装饰工程消耗量定额及全费用基价表（结构·屋面）（2024）.pdf")
ASSETS = Path("tests/acceptance/assets")


def main():
    ASSETS.mkdir(parents=True, exist_ok=True)
    text_pdf = ASSETS / "quota_text_p20_29.pdf"
    scan_pdf = ASSETS / "quota_scan_p34_39.pdf"
    image = ASSETS / "quota_p21.png"
    if text_pdf.exists() and scan_pdf.exists() and image.exists():
        print("资产已存在，跳过生成")
        return
    src = fitz.open(str(SRC))
    # 文本版：原书 20-29 页（带文本层）
    out = fitz.open()
    out.insert_pdf(src, from_page=19, to_page=28)
    out.save(str(text_pdf), deflate=True)
    # 扫描版：原书 34-39 页渲染成纯图像 PDF（无文本层，触发 OCR 回退）
    out2 = fitz.open()
    for i in range(33, 39):
        page = src[i]
        pix = page.get_pixmap(dpi=150)
        newpage = out2.new_page(width=page.rect.width, height=page.rect.height)
        newpage.insert_image(page.rect, stream=pix.tobytes("png"))
    out2.save(str(scan_pdf), deflate=True)
    # 单页图片：第 21 页
    pix = src[20].get_pixmap(dpi=150)
    pix.save(str(image))
    print(f"资产生成完成: {text_pdf.name}, {scan_pdf.name}, {image.name}")


if __name__ == "__main__":
    main()

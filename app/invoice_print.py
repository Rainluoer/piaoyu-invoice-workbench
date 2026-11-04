"""只读渲染本地发票，供浏览器统一排版打印；不修改原件。"""
from contextlib import closing
import io

from PIL import Image, ImageOps
import pypdfium2 as pdfium

from invoice_company import PDFIUM_LOCK


def page_count(content, suffix):
    if suffix.lower() == '.pdf':
        with PDFIUM_LOCK, pdfium.PdfDocument(content) as document:
            count = len(document)
            if not count:
                raise ValueError('PDF 没有可打印页面')
            return count
    with Image.open(io.BytesIO(content)) as image:
        image.verify()
    return 1


def render_page(content, suffix, page):
    output = io.BytesIO()
    if suffix.lower() == '.pdf':
        with PDFIUM_LOCK, pdfium.PdfDocument(content) as document:
            if page < 1 or page > len(document):
                raise ValueError('页码超出范围')
            with closing(document[page - 1]) as source, closing(source.render(scale=200 / 72)) as bitmap:
                bitmap.to_pil().save(output, format='PNG')
    else:
        if page != 1:
            raise ValueError('页码超出范围')
        with Image.open(io.BytesIO(content)) as image:
            ImageOps.exif_transpose(image).save(output, format='PNG')
    return output.getvalue()

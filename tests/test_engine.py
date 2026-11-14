import tempfile
import unittest
from pathlib import Path

import fitz
from PIL import Image, ImageDraw, ImageFont

from engine import Document, Redaction, apply_redactions, export_pdf


class LocalRedactorEngineTests(unittest.TestCase):
    def make_fixture(self, path: Path):
        image = Image.new("RGB", (1600, 900), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 50)
        rows = [
            (90, "Name: Rahul Sharma"),
            (205, "Aadhaar: 1234 5678 9012"),
            (320, "PAN: ABCDE1234F"),
            (435, "Email: rahul@example.com"),
            (550, "Mobile: 9876543210"),
            (665, "Account Number: 1234567890123456"),
            (780, "IFSC: HDFC0001234"),
        ]
        for y, text in rows:
            draw.text((80, y), text, font=font, fill="black")
        image.save(path)

    def test_local_ocr_identifier_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "synthetic-test.png"
            self.make_fixture(source)
            doc = Document(source)
            regions = doc.detect(0)
            labels = {region.label for region in regions}
            self.assertIn("Aadhaar number", labels)
            self.assertIn("PAN", labels)
            self.assertIn("Email", labels)
            self.assertIn("Indian mobile", labels)
            self.assertIn("Bank account number", labels)
            self.assertIn("IFSC code", labels)

    def test_digital_pdf_text_layer_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "digital.pdf"
            pdf = fitz.open()
            page = pdf.new_page(width=612, height=792)
            page.insert_text((60, 100), "Aadhaar: 1234 5678 9012", fontsize=18)
            page.insert_text((60, 150), "PAN: ABCDE1234F", fontsize=18)
            page.insert_text((60, 200), "Email: rahul@example.com", fontsize=18)
            pdf.save(source)
            pdf.close()
            doc = Document(source)
            regions = doc.detect(0)
            labels = {region.label for region in regions}
            self.assertIn("Aadhaar number", labels)
            self.assertIn("PAN", labels)
            self.assertIn("Email", labels)

    def test_manual_redaction_writes_black_pixels(self):
        image = Image.new("RGB", (100, 100), "white")
        redacted = apply_redactions(image, [Redaction(20, 20, 30, 30, "black")])
        self.assertEqual(redacted.getpixel((35, 35)), (0, 0, 0))
        self.assertEqual(redacted.getpixel((10, 10)), (255, 255, 255))

    def test_pdf_export_is_image_only(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.png"
            output = Path(temp) / "redacted.pdf"
            self.make_fixture(source)
            doc = Document(source)
            doc.pages[0].redactions.append(Redaction(200, 200, 500, 100, "black"))
            export_pdf(doc.pages, output, "FOR TEST ONLY", "14 NOV 2026")
            with fitz.open(output) as result:
                self.assertEqual(len(result), 1)
                self.assertEqual(result[0].get_text().strip(), "")
                self.assertEqual(len(result[0].get_images()), 1)


if __name__ == "__main__":
    unittest.main()

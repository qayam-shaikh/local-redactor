# Local Redactor

A local-first Ubuntu desktop utility for redacting sensitive information from images and multi-page PDFs. It is a native PySide6 desktop application; it does not run a web server, upload files, call cloud APIs, or require an account.

## What's in this updated build

- A redesigned desktop interface with document preview, page navigation, zoom, scan controls, manual tools, watermark settings, and export.
- Open or drag-and-drop PNG, JPG/JPEG, WebP, BMP, and multi-page PDF files.
- Local multi-pass Tesseract OCR with image enhancement and coordinate mapping.
- Optional Microsoft Presidio PII analysis using the local spaCy `en_core_web_lg` model.
- Presidio's built-in Indian recognizers for Aadhaar, PAN, passport, voter ID, vehicle registration, and GSTIN, along with common PII recognizers such as person names, locations, email, phone, and payment cards.
- Additional local pattern checks for Indian mobile numbers, contextual bank account numbers, and IFSC codes.
- QR-code region suggestions when OpenCV can detect them.
- Page-by-page review, apply-all suggestions for the current page, and manual drag-to-redact.
- Solid black redaction (recommended) or pixelation.
- Diagonal purpose/date watermark burned into output pages.
- Re-encoded images and freshly generated image-only PDFs. The source text layer and source metadata are not copied.

## Install on Ubuntu

The first installation needs an internet connection to download the Python packages and the spaCy model. Once installed, document processing runs locally and does not need internet access.

Extract the ZIP into a permanent folder, for example `~/Applications/LocalRedactor`. Keep that folder in place after installation because the Ubuntu application-menu launcher points to it.

```bash
sudo apt update
sudo apt install -y python3-venv tesseract-ocr libxcb-cursor0 libxkbcommon-x11-0 libxcb-xinerama0 libegl1 libgl1
chmod +x install.sh run.sh
./install.sh
```

The installer creates a project-local `.venv`, installs the dependencies, downloads the local spaCy `en_core_web_lg` model, and adds **Local Redactor** to the Ubuntu application menu.

Launch it from the application grid, or run:

```bash
./run.sh
```

### If the PII model did not download

The app still opens and performs OCR plus deterministic identifier-pattern checks. To enable the additional Presidio NER/recognizer layer after connecting to the internet, run:

```bash
.venv/bin/python -m spacy download en_core_web_lg
```

Then restart the application. The interface shows whether the Presidio + spaCy model is ready or whether only the fallback detector is available.

## Use

1. Open an image or PDF, or drag one into the application.
2. Choose **All pages** to scan a whole PDF, or **This page** to scan just the page currently visible.
3. Review every amber-highlighted suggestion. The model and OCR can miss information or produce false positives.
4. Click **Apply all suggestions on this page** to black out/pixelate the suggestions on that page.
5. To cover anything else, choose **Draw on document**, then click-drag on the preview and release. The covered pixels should appear in the preview immediately. Zoom controls are available for precise selection.
6. Enter a purpose watermark, such as `FOR RENTAL KYC ONLY`, and check the date.
7. Export the flattened file and inspect the exported copy before sharing it.

## Important security limitations

- Automatic detection is assistance, not a guarantee. Always inspect every page. It may miss handwriting, unusual fonts, low-resolution scans, non-English text, rotated text, names, addresses, or unrecognized document formats.
- Presidio's Indian identifier recognizers are rule/context/checksum-based. Its general person/location recognition uses the installed spaCy model. Accuracy still depends on OCR quality and document layout.
- A solid black bar is the recommended redaction. Pixelation is cosmetic and may leave information inferable.
- The PDF export is reconstructed from raster page images. It has no selectable source text layer, and source annotations, forms, attachments, bookmarks, and metadata are not preserved. It is flattened, not a cryptographic PDF signature or access-control mechanism.
- The watermark is burned into page pixels. It discourages reuse but cannot technically prevent copying or editing.
- Keep the original file until you have reviewed the export. This tool is not a substitute for a manual privacy/security review before sharing identity or financial documents.

## Local processing and privacy

The application code contains no upload endpoint, web server, analytics, or runtime cloud/API call. OCR, PII recognition, QR detection, redaction, watermarking, and export happen in the local process. The setup script downloads software/model packages only during installation; it does not upload the documents you process.

## Verify the local engine

After installing, run the included tests (they use generated sample data, not your personal documents):

```bash
./run-tests.sh
```

The suite checks OCR identifier suggestions, native PDF text-layer detection, manual black-pixel redaction, and flattened PDF export.

## Uninstall

Remove the app launcher and project folder:

```bash
rm -f ~/.local/share/applications/local-redactor.desktop
rm -rf ~/Applications/LocalRedactor
```

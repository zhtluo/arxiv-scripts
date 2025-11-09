import os
import tempfile
import fontforge
import pikepdf
import argparse


def safe_fontname(old_name, replacement):
    return old_name.replace("CM", replacement)


def safe_fontfamily(old_name, replacement):
    return old_name.replace("Computer Modern", replacement)


def rename_font(input_path, output_path, replacement, replace_family):
    font = fontforge.open(input_path)

    print(
        f"[+] Original Font Names: {font.fontname}, {font.familyname}, {font.fullname}"
    )
    font.fontname = safe_fontname(font.fontname, replacement)
    font.familyname = safe_fontfamily(font.familyname, replace_family)
    font.fullname = safe_fontname(font.fullname, replacement)

    font.generate(output_path)


def process_pdf(input_pdf, output_pdf, replacement, replace_family):
    pdf = pikepdf.open(input_pdf)

    for obj in pdf.objects:
        if isinstance(obj, pikepdf.Dictionary) and "/FontDescriptor" in obj:
            fontdesc = obj["/FontDescriptor"]
            if not isinstance(fontdesc, pikepdf.Dictionary):
                continue

            fontname = str(fontdesc.get("/FontName", ""))
            if "CM" not in fontname:
                continue  # skip non-CM fonts

            print(f"[+] Found CM font: {fontname}")
            fontfile_key = None

            for key in ["/FontFile", "/FontFile2", "/FontFile3"]:
                if key in fontdesc:
                    fontfile_key = key
                    break

            if not fontfile_key:
                print("[-] No embedded font file found")
                continue

            old_font_stream = fontdesc[fontfile_key].read_bytes()

            with tempfile.TemporaryDirectory() as tmpdir:
                raw_font_path = os.path.join(tmpdir, "original_font.pfb")
                renamed_font_path = os.path.join(tmpdir, "renamed_font.pfb")

                with open(raw_font_path, "wb") as f:
                    f.write(old_font_stream)

                rename_font(
                    raw_font_path, renamed_font_path, replacement, replace_family
                )

                with open(renamed_font_path, "rb") as f:
                    new_font_data = f.read()
                new_stream = pikepdf.Stream(pdf, new_font_data)

                new_fontname = safe_fontname(fontname, replacement)
                fontdesc[fontfile_key] = new_stream
                fontdesc["/FontName"] = pikepdf.Name(new_fontname)

                if "/BaseFont" in obj:
                    print(f"[+] Updating BaseFont from {obj['/BaseFont']}")
                    base_font = str(obj["/BaseFont"])
                    if "CM" in base_font:
                        new_base = safe_fontname(base_font, replacement)
                        obj["/BaseFont"] = pikepdf.Name(new_base)

                print(f"[✓] Replaced: {fontname} → {new_fontname}")

    # --- Strip Document Info ---
    print(f"[*] Metadata: {pdf.docinfo}")
    print("[*] Stripping metadata...")
    for key in list(pdf.docinfo.keys()):
        del pdf.docinfo[key]

    # --- Remove XMP Metadata if present ---
    if "/Metadata" in pdf.Root:
        del pdf.Root["/Metadata"]
    print("[*] Removed XMP metadata stream")

    pdf.save(output_pdf)
    print(f"[✔] Saved updated PDF to: {output_pdf}")


# === CLI ===
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Patch embedded CM fonts in a PDF.")
    parser.add_argument("input_pdf", help="Input PDF file")
    parser.add_argument("output_pdf", help="Output PDF file")
    parser.add_argument(
        "--replace-with", default="SF", help="String to replace 'CM' with (default: SF)"
    )
    parser.add_argument(
        "--replace-family",
        default="Safe Font",
        help="Replacement for 'Computer Modern' family name (default: 'Safe Font')",
    )

    args = parser.parse_args()
    process_pdf(args.input_pdf, args.output_pdf, args.replace_with, args.replace_family)

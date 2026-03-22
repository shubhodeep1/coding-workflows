#!/usr/bin/env python3
import argparse
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ai_context_utils import classify_attachment_type, extract_attachment_candidates


_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _sanitize_name(name: str, fallback: str) -> str:
    candidate = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    candidate = candidate.strip("._")
    return (candidate or fallback)[:120]


def _safe_rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=f"missing_binary:{cmd[0]}")


def _extract_text(path: Path, fallback_text_path: Path) -> tuple[bool, str]:
    result = _run(["pdftotext", str(path), str(fallback_text_path)])
    if result.returncode == 0 and fallback_text_path.exists():
        return True, ""
    return False, (result.stderr or result.stdout or "pdftotext failed").strip()


def _ocr_image(image_path: Path, output_txt_path: Path) -> tuple[bool, str]:
    output_base = output_txt_path.with_suffix("")
    result = _run(["tesseract", str(image_path), str(output_base), "-l", "eng"], timeout=240)
    expected = output_base.with_suffix(".txt")
    if result.returncode == 0 and expected.exists():
        expected.replace(output_txt_path)
        return True, ""
    return False, (result.stderr or result.stdout or "tesseract failed").strip()


def _convert_doc_to_txt(path: Path, out_dir: Path) -> tuple[bool, Path | None, str]:
    result = _run(["pandoc", str(path), "-t", "plain", "-o", str(out_dir / (path.stem + ".txt"))])
    out_path = out_dir / (path.stem + ".txt")
    if result.returncode == 0 and out_path.exists():
        return True, out_path, ""

    soffice = _run([
        "libreoffice",
        "--headless",
        "--convert-to",
        "txt:Text",
        "--outdir",
        str(out_dir),
        str(path),
    ])
    converted = out_dir / (path.stem + ".txt")
    if soffice.returncode == 0 and converted.exists():
        return True, converted, ""

    err = " ".join(filter(None, [result.stderr, result.stdout, soffice.stderr, soffice.stdout])).strip()
    return False, None, (err or "doc conversion failed")


def _extract_doc_images(path: Path, out_dir: Path) -> list[Path]:
    images: list[Path] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix == ".docx":
        try:
            with zipfile.ZipFile(path) as zf:
                for member in zf.namelist():
                    if not member.startswith("word/media/"):
                        continue
                    raw_name = Path(member).name
                    target = out_dir / _sanitize_name(raw_name, "doc_image.bin")
                    with zf.open(member) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    images.append(target)
        except zipfile.BadZipFile:
            pass
    return images


def _render_pdf_pages(path: Path, out_prefix: Path) -> tuple[list[Path], str]:
    result = _run(["pdftoppm", "-png", str(path), str(out_prefix)], timeout=300)
    if result.returncode != 0:
        return [], (result.stderr or result.stdout or "pdftoppm failed").strip()
    pages = sorted(out_prefix.parent.glob(out_prefix.name + "-*.png"))
    return pages, ""


def _http_error(code: int | None) -> str:
    return f"http_{code if code is not None else 'unknown'}"


def _download(url: str, out_path: Path, token: str) -> tuple[bool, str, str]:
    headers = {"User-Agent": "issue-attachment-bundle/1.0"}
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme.lower() not in ("http", "https"):
        return False, "unsupported_scheme", ""
    hostname = (parsed.hostname or "").lower()
    is_github_host = (
        hostname == "github.com"
        or hostname.endswith(".github.com")
        or hostname == "githubusercontent.com"
        or hostname.endswith(".githubusercontent.com")
    )
    req = urllib.request.Request(url, headers=headers)
    if token and token.strip() and is_github_host:
        req.add_unredirected_header("Authorization", f"Bearer {token.strip()}")
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            code = response.getcode()
            if code != 200:
                _safe_unlink(out_path)
                return False, f"http_{code}", ""
            content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0].strip().lower()
            total = 0
            with out_path.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        handle.close()
                        _safe_unlink(out_path)
                        return False, "attachment_too_large", ""
                    handle.write(chunk)
            return True, "", content_type
    except urllib.error.HTTPError as exc:
        _safe_unlink(out_path)
        return False, _http_error(exc.code), ""
    except urllib.error.URLError as exc:
        _safe_unlink(out_path)
        reason = exc.reason
        reason_str = str(reason).replace(" ", "_").lower()[:50] if reason is not None else "unknown"
        return False, reason_str, ""
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(out_path)
        return False, "internal_error", ""


def _build_bundle(issue_body: str, out_dir: Path, gh_token: str) -> dict:
    originals = out_dir / "attachments" / "originals"
    ocr_dir = out_dir / "attachments" / "derived" / "ocr"
    docs_text_dir = out_dir / "attachments" / "derived" / "docs_text"
    docs_images_dir = out_dir / "attachments" / "derived" / "docs_images"
    pdf_text_dir = out_dir / "attachments" / "derived" / "pdf_text"
    pdf_pages_dir = out_dir / "attachments" / "derived" / "pdf_pages"
    for p in [originals, ocr_dir, docs_text_dir, docs_images_dir, pdf_text_dir, pdf_pages_dir]:
        p.mkdir(parents=True, exist_ok=True)

    entries = []
    seen_urls = set()
    candidates = extract_attachment_candidates(issue_body)

    for index, candidate in enumerate(candidates, start=1):
        source_url = candidate["url"]
        normalized = urllib.parse.urlunsplit(urllib.parse.urlsplit(source_url)._replace(fragment=""))
        if normalized in seen_urls:
            continue
        seen_urls.add(normalized)

        guessed_name = _sanitize_name(Path(urllib.parse.urlsplit(source_url).path).name or f"attachment_{index}", f"attachment_{index}.bin")
        original_path = originals / f"{index:03d}_{guessed_name}"

        ok, err, mime_type = _download(source_url, original_path, gh_token)
        entry = {
            "source_url": source_url,
            "normalized_url": normalized,
            "status": "ok" if ok else "error",
            "error": err if not ok else "",
            "mime_type": mime_type or mimetypes.guess_type(original_path.name)[0] or "application/octet-stream",
            "detected_type": "other",
            "size_bytes": original_path.stat().st_size if ok and original_path.exists() else 0,
            "sha256": _sha256(original_path) if ok and original_path.exists() else "",
            "original_path": _safe_rel(original_path, out_dir) if ok and original_path.exists() else "",
            "derivatives": {},
        }

        if not ok:
            entries.append(entry)
            continue

        detected_type = classify_attachment_type(original_path.name, entry["mime_type"])
        entry["detected_type"] = detected_type

        if detected_type == "image":
            ocr_txt = ocr_dir / f"{original_path.stem}.txt"
            ocr_ok, ocr_err = _ocr_image(original_path, ocr_txt)
            if ocr_ok:
                entry["derivatives"]["ocr_text"] = _safe_rel(ocr_txt, out_dir)
            else:
                entry["derivatives"]["ocr_error"] = ocr_err

        elif detected_type == "doc":
            txt_ok, txt_path, txt_err = _convert_doc_to_txt(original_path, docs_text_dir)
            if txt_ok and txt_path:
                entry["derivatives"]["doc_text"] = _safe_rel(txt_path, out_dir)
            else:
                entry["derivatives"]["doc_text_error"] = txt_err

            doc_images = _extract_doc_images(original_path, docs_images_dir / original_path.stem)
            if doc_images:
                rel_images = []
                rel_ocr = []
                for image in doc_images:
                    rel_images.append(_safe_rel(image, out_dir))
                    ocr_txt = ocr_dir / f"{image.stem}.txt"
                    ocr_ok, ocr_err = _ocr_image(image, ocr_txt)
                    if ocr_ok:
                        rel_ocr.append(_safe_rel(ocr_txt, out_dir))
                    else:
                        rel_ocr.append(f"ERROR:{ocr_err}")
                entry["derivatives"]["doc_images"] = rel_images
                entry["derivatives"]["doc_images_ocr"] = rel_ocr

        elif detected_type == "pdf":
            pdf_text = pdf_text_dir / f"{original_path.stem}.txt"
            text_ok, text_err = _extract_text(original_path, pdf_text)
            if text_ok:
                entry["derivatives"]["pdf_text"] = _safe_rel(pdf_text, out_dir)
            else:
                entry["derivatives"]["pdf_text_error"] = text_err

            page_prefix = pdf_pages_dir / original_path.stem
            pages, pages_err = _render_pdf_pages(original_path, page_prefix)
            if pages:
                rel_pages = []
                rel_ocr = []
                for page in pages:
                    rel_pages.append(_safe_rel(page, out_dir))
                    ocr_txt = ocr_dir / f"{page.stem}.txt"
                    ocr_ok, ocr_err = _ocr_image(page, ocr_txt)
                    if ocr_ok:
                        rel_ocr.append(_safe_rel(ocr_txt, out_dir))
                    else:
                        rel_ocr.append(f"ERROR:{ocr_err}")
                entry["derivatives"]["pdf_pages"] = rel_pages
                entry["derivatives"]["pdf_pages_ocr"] = rel_ocr
            elif pages_err:
                entry["derivatives"]["pdf_pages_error"] = pages_err

        entries.append(entry)

    manifest = {
        "schema": 1,
        "files": entries,
        "summary": {
            "discovered": len(candidates),
            "processed": len(entries),
            "ok": len([x for x in entries if x["status"] == "ok"]),
            "error": len([x for x in entries if x["status"] != "ok"]),
        },
    }
    manifest_path = out_dir / "attachments" / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _zip_dir(source_dir: Path, out_zip: Path) -> None:
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(source_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(source_dir))


def build_cmd(args: argparse.Namespace) -> int:
    issue_body = Path(args.issue_body_file).read_text(encoding="utf-8")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = _build_bundle(issue_body=issue_body, out_dir=out_dir, gh_token=args.gh_token)

    if args.zip_output:
        _zip_dir(out_dir / "attachments", Path(args.zip_output))

    if args.manifest_output:
        Path(args.manifest_output).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def extract_cmd(args: argparse.Namespace) -> int:
    zip_path = Path(args.zip_path)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        out_root = out_dir.resolve()
        for member in zf.infolist():
            if ((member.external_attr >> 16) & 0o170000) == 0o120000:
                continue
            member_path = (out_dir / member.filename).resolve()
            if not member_path.is_relative_to(out_root):
                continue
            zf.extract(member, out_dir)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue attachment bundle builder/loader")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build")
    p_build.add_argument("--issue-body-file", required=True)
    p_build.add_argument("--out-dir", required=True)
    p_build.add_argument("--gh-token", default="")
    p_build.add_argument("--zip-output", default="")
    p_build.add_argument("--manifest-output", default="")
    p_build.set_defaults(func=build_cmd)

    p_extract = sub.add_parser("extract")
    p_extract.add_argument("--zip-path", required=True)
    p_extract.add_argument("--out-dir", required=True)
    p_extract.set_defaults(func=extract_cmd)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

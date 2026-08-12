"""pdftotext extraction adapter for the mandatory ATS-parseability step.
pdftotext is already a hard dependency of onboarding, so there is no degraded
mode: the check is mandatory (deviating deliberately from ai-job-search's
graceful degrade, which trades away the guarantee)."""

import subprocess

from domain.ports import PdfTextExtractor


class PdfTextError(RuntimeError):
    pass


class PopplerPdfTextExtractor(PdfTextExtractor):
    def extract_layout(self, pdf_bytes: bytes) -> str:
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", "-", "-"], input=pdf_bytes, capture_output=True)
        except FileNotFoundError as e:
            raise PdfTextError(
                "pdftotext not found; install poppler-utils (the ATS check is"
                " mandatory, there is no degraded mode)") from e
        if proc.returncode != 0:
            raise PdfTextError(
                f"pdftotext exited {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}")
        return proc.stdout.decode("utf-8", errors="replace")

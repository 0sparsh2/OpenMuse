"""
Library / documents checks (issue #14) — offline.

Builds a real fillable PDF form, then proves: uploads + reading, secret and
active-PDF rejection, Markdown/CSV/XLSX/PDF generation (PDF via headless
Chromium), form field discovery, filling into a NEW copy (original untouched,
signatures refused), per-user isolation, knowledge-bank ingestion, and the
agent tools' document cards.
"""
from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
os.environ.pop("NVIDIA_NIM_API_KEY", None)

from pypdf import PdfReader, PdfWriter                          # noqa: E402
from pypdf.generic import (ArrayObject, BooleanObject, DictionaryObject,  # noqa: E402
                           FloatObject, NameObject, NumberObject, TextStringObject)

from api import ApiBackend                                       # noqa: E402
from gateway import ModelResponse, ToolCall                      # noqa: E402
from memory.service import MemoryService                         # noqa: E402
from policy import AutonomousDecider                             # noqa: E402

RESULTS: list[tuple[str, bool]] = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def make_form() -> bytes:
    """A one-page permission slip with text, checkbox and signature fields."""
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    fields = ArrayObject()

    def field(name, ftype, rect, label, extra=None):
        d = DictionaryObject({
            NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject(ftype), NameObject("/T"): TextStringObject(name),
            NameObject("/TU"): TextStringObject(label),
            NameObject("/Rect"): ArrayObject([FloatObject(v) for v in rect]),
            NameObject("/F"): NumberObject(4)})
        d.update(extra or {})
        ref = w._add_object(d)
        fields.append(ref)
        return ref
    annots = ArrayObject([
        field("child_name", "/Tx", [100, 700, 400, 720], "Student name"),
        field("parent_phone", "/Tx", [100, 660, 400, 680], "Parent phone"),
        field("agree", "/Btn", [100, 620, 115, 635], "I give permission",
              {NameObject("/V"): NameObject("/Off"), NameObject("/AS"): NameObject("/Off")}),
        field("signature", "/Sig", [100, 560, 400, 600], "Parent signature")])
    page[NameObject("/Annots")] = annots
    w._root_object[NameObject("/AcroForm")] = DictionaryObject({
        NameObject("/Fields"): fields, NameObject("/NeedAppearances"): BooleanObject(True)})
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="openmuse-lib-")
    mem = MemoryService(tmp, prompts_dir=os.path.join(ROOT, "prompts"), llm=None)
    SCRIPT: list = []

    def respond(request, history):
        tools = [m for m in request.messages if m.role == "tool"]
        if len(tools) < len(SCRIPT):
            name, args = SCRIPT[len(tools)]
            return ModelResponse(text="", stop_reason="tool_calls",
                                 tool_calls=[ToolCall(id=f"d{len(tools)}", name=name, arguments=args)])
        return ModelResponse(text="done", stop_reason="stop")

    backend = ApiBackend(workspace_root=os.path.join(tmp, "ws"), respond=respond, memory_service=mem,
                         db_path=os.path.join(tmp, "db.sqlite"), library_root=os.path.join(tmp, "users"))
    backend.decider = AutonomousDecider()
    lib = backend.library

    # -- uploads + reading ----------------------------------------------------------
    note = lib.add("usr_a", "trip notes.md", b"# Aquarium trip\nBus leaves at **8:15**. Bring lunch.")
    check("upload stored with type + size", note["mime"] == "text/markdown" and note["size"] > 10)
    check("text is readable", "Bus leaves at" in lib.text(note))
    for name, data, why in [("keys.txt", b"aws_secret_access_key = AKIAABCDEFGHIJKLMNOP/abcdEFGHijklMNOPqrstUVWXyz0123456789", "secrets"),
                            ("evil.pdf", b"%PDF-1.4\n1 0 obj << /OpenAction << /S /JavaScript /JS (app.alert(1)) >> >>", "active PDF")]:
        try:
            lib.add("usr_a", name, data); ok = False
        except ValueError:
            ok = True
        check(f"rejects {why}", ok)

    # -- generation --------------------------------------------------------------------
    md = lib.create("usr_a", title="Packing list", fmt="md", content="- Lunch\n- Water")
    check("markdown created", lib.data(md).startswith(b"- Lunch"))
    csv_ = lib.create("usr_a", title="Flights", fmt="csv", columns=["Airline", "Price"], rows=[["JetBlue", 647]])
    check("csv created", lib.data(csv_).decode().splitlines() == ["Airline,Price", "JetBlue,647"])
    xl = lib.create("usr_a", title="Flights", fmt="xlsx", columns=["Airline", "Price"],
                    rows=[["JetBlue", 647], ["Alaska", 648]])
    check("xlsx created and readable", "JetBlue\t647" in lib.text(xl) and xl["name"].endswith(".xlsx"))
    t0 = time.time()
    pdf = lib.create("usr_a", title="Trip summary", fmt="pdf",
                     content="## Details\n- Leaves **8:15**\n\n| Item | Cost |\n|---|---|\n| Ticket | $12 |")
    text = lib.text(pdf)
    check("pdf rendered by Chromium from Markdown", lib.data(pdf).startswith(b"%PDF")
          and "Trip summary" in text and "Ticket" in text and "$12" in text, text[:120])
    print(f"      (pdf render took {time.time() - t0:.1f}s)")

    # -- PDF forms ------------------------------------------------------------------------
    form = lib.add("usr_a", "permission slip.pdf", make_form(), source="email attachment")
    fields = {f["name"]: f for f in lib.pdf_fields(form)}
    check("form fields discovered with types + labels",
          fields.get("child_name", {}).get("type") == "text" and fields.get("agree", {}).get("type") == "checkbox"
          and fields.get("signature", {}).get("type") == "signature"
          and fields["parent_phone"]["label"] == "Parent phone", str(fields)[:200])
    before = hashlib.sha256(lib.data(form)).hexdigest()
    filled, review = lib.pdf_fill("usr_a", form, {"child_name": "Maya Rivera", "parent_phone": "555-0142",
                                                 "agree": True})
    got = PdfReader(io.BytesIO(lib.data(filled))).get_fields()
    check("filled copy has the values", str(got["child_name"].get("/V")) == "Maya Rivera"
          and str(got["parent_phone"].get("/V")) == "555-0142" and str(got["agree"].get("/V")) == "/Yes")
    check("original is untouched", hashlib.sha256(lib.data(form)).hexdigest() == before)
    check("filled copy saved separately", filled["artifact_id"] != form["artifact_id"]
          and filled["name"] == "permission slip (filled).pdf" and filled["meta"]["filled_from"] == form["artifact_id"])
    check("review lists label -> value", {"label": "Student name", "value": "Maya Rivera"} in review
          and {"label": "I give permission", "value": "Checked"} in review)
    for bad, why in [({"signature": "M. Rivera"}, "signature fields are refused"),
                     ({"nope": "x"}, "unknown fields are refused")]:
        try:
            lib.pdf_fill("usr_a", form, bad); ok = False
        except ValueError:
            ok = True
        check(why, ok)

    # -- isolation + memory ------------------------------------------------------------------
    check("B can't see A's documents", lib.get("usr_b", form["artifact_id"]) is None and lib.list("usr_b") == [])
    doc = mem.ingest("usr_a", note["name"], data=lib.data(note), filename=note["name"])
    hits = mem.memory("usr_a").recall("when does the bus leave", sources=["documents"])
    check("Add to memory -> searchable in the knowledge bank", doc["chunks"] >= 1 and hits
          and "8:15" in hits[0]["text"])
    b2 = ApiBackend(workspace_root=os.path.join(tmp, "ws"), db_path=os.path.join(tmp, "db.sqlite"),
                    library_root=os.path.join(tmp, "users"))
    check("library survives a restart", len(b2.library.list("usr_a")) == len(lib.list("usr_a")))

    # -- through the agent ------------------------------------------------------------------
    def run_script(steps, text):
        SCRIPT[:] = steps
        chat = backend.create_session(user_id="usr_a").chat_id
        run, _, _ = backend.submit_message(chat_id=chat, user_id="usr_a", content=[{"type": "text", "text": text}])
        t = time.time()
        while run.state not in ("COMPLETED", "FAILED", "WAITING_FOR_APPROVAL") and time.time() - t < 90:
            time.sleep(0.05)
        return run, [e.data.get("display") for e in backend.eventbus.read_since(run.run_id, -1)
                     if e.type == "tool.result" and e.data.get("display")]

    run, cards = run_script([("docs.create", {"title": "Cheapest flights", "format": "xlsx",
                                              "columns": ["Airline", "Price"], "rows": [["JetBlue", 647]]})],
                            "make a spreadsheet")
    check("agent creates a spreadsheet on its own", run.state == "COMPLETED")
    check("chat shows a document card", cards and cards[0]["type"] == "document"
          and cards[0]["title"] == "Cheapest flights.xlsx" and cards[0]["artifact_id"].startswith("art_"))
    run, cards = run_script([("docs.pdf_fields", {"artifact_id": form["artifact_id"]}),
                             ("docs.pdf_fill", {"artifact_id": form["artifact_id"],
                                                "values": {"child_name": "Maya Rivera"}})], "fill the slip")
    check("agent fills a form into a new copy", run.state == "COMPLETED")
    check("form card shows the filled values for review", cards and cards[-1].get("fields")
          == [{"label": "Student name", "value": "Maya Rivera"}])

    passed = sum(ok for _, ok in RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())

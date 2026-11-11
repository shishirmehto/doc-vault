"""Generate the sample documents and samples/facts.yaml.

    uv run python scripts/make_samples.py

Everything here is fictitious and written for this repository. The output is
deterministic: the same script and library versions give the same bytes, so
the content hash of each sample is stable.

Each document is a list of pages and each page a list of blocks, so the page
a fact sits on is decided here, not by a layout engine. FACTS names the facts
later tests and evals rely on. The script refuses to write a fact whose text
is not on the page it claims.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from docx import Document
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen.canvas import Canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
FIXED_TIME = datetime(2026, 1, 1)

MARK = "SAMPLE - fictitious document. Not a real insurance contract."

POLICY = "sample-cgl-policy.pdf"
QUOTE = "sample-renewal-quote.pdf"
ENDORSEMENT = "sample-endorsement-additional-insured.docx"

POLICY_FOOTER = ("This policy is subject to all terms, conditions and exclusions of the forms listed in the "
                 "Schedule of Forms and Endorsements.")
QUOTE_FOOTER = "This quote is not a binder. Coverage is not in effect until the insurer confirms it in writing."


@dataclass(frozen=True)
class H:
    text: str


@dataclass(frozen=True)
class P:
    text: str


@dataclass(frozen=True)
class KV:
    """A declarations line: label on the left, value at a fixed tab stop."""

    label: str
    value: str

    @property
    def text(self) -> str:
        return f"{self.label} {self.value}"


type Block = H | P | KV

POLICY_PAGES: list[list[Block]] = [
    [
        H("COMMERCIAL GENERAL LIABILITY POLICY"),
        H("DECLARATIONS"),
        KV("Insurer:", "Sample Mutual Insurance Company"),
        KV("Policy Number:", "SAMPLE-CGL-2026-0001"),
        KV("Named Insured:", "Example Contractors LLC"),
        KV("Mailing Address:", "100 Example Street, Sampletown, ST 00000"),
        KV("Form of Business:", "Limited liability company"),
        KV("Business Description:", "Residential and light commercial roofing contractor"),
        KV("Producer:", "Example Brokerage Services"),
        P("Policy Period: From 01/01/2026 To 01/01/2027, beginning and ending at 12:01 a.m. standard time at "
          "the mailing address shown above."),
        P("In return for the payment of the premium, and subject to all the terms of this policy, we agree "
          "with you to provide the insurance stated in this policy."),
        KV("Total Advance Premium:", "$18,450"),
        KV("Premium Audit Period:", "Annual"),
        P("These Declarations, together with the coverage form and the endorsements listed in the Schedule of "
          "Forms and Endorsements, complete this policy. Words such as you and your refer to the Named "
          "Insured shown above. Words such as we, us and our refer to the Insurer."),
    ],
    [
        H("DECLARATIONS (continued)"),
        H("LIMITS OF INSURANCE"),
        KV("Each Occurrence Limit", "$1,000,000"),
        KV("Damage to Premises Rented to You Limit", "$100,000 any one premises"),
        KV("Medical Expense Limit", "$5,000 any one person"),
        KV("Personal and Advertising Injury Limit", "$1,000,000"),
        KV("General Aggregate Limit", "$2,000,000"),
        KV("Products-Completed Operations Aggregate Limit", "$2,000,000"),
        H("DEDUCTIBLE"),
        KV("Property Damage Deductible", "$2,500 per occurrence"),
        P("The deductible applies to all damages because of property damage as the result of any one "
          "occurrence, regardless of the number of persons or organizations who sustain damages. We may pay "
          "any part of the deductible to settle a claim, and you must reimburse us promptly."),
        H("PREMIUM BASIS"),
        KV("Classification", "Roofing - residential, three storeys or less"),
        KV("Estimated Annual Payroll", "$640,000"),
        KV("Rate per $1,000 of Payroll", "$28.83"),
        P("The General Aggregate Limit is the most we will pay during the policy period for all damages "
          "other than damages included in the products-completed operations hazard. The Each Occurrence "
          "Limit is the most we will pay for all damages arising out of any one occurrence, and it sits "
          "inside whichever aggregate limit applies."),
    ],
    [
        H("SCHEDULE OF FORMS AND ENDORSEMENTS"),
        P("The following forms and endorsements are attached to and form part of this policy on its "
          "effective date."),
        KV("CG 00 01", "Commercial General Liability Coverage Form"),
        KV("CG 21 47", "Employment-Related Practices Exclusion"),
        KV("CG 21 67", "Fungi or Bacteria Exclusion"),
        KV("SM 10 05", "Roofing Operations Limitation - Height and Open Roof"),
        KV("SM 20 02", "Subcontractor Certificate Condition"),
        P("A form number beginning with CG identifies a standard industry form number. A form number "
          "beginning with SM identifies a form issued by Sample Mutual Insurance Company. The wording in "
          "this sample was written for demonstration and is not the wording of any published form."),
        P("An endorsement issued after the effective date changes this policy only from the effective date "
          "shown in that endorsement. If the wording of an endorsement conflicts with the coverage form, "
          "the endorsement governs."),
    ],
    [
        H("SECTION I - COVERAGE A: BODILY INJURY AND PROPERTY DAMAGE LIABILITY"),
        H("1. Insuring Agreement"),
        P("We agree to pay the amounts that an insured is legally required to pay as damages for bodily "
          "injury or property damage covered by this policy. The bodily injury or property damage must be "
          "caused by an occurrence that takes place in the coverage territory, and it must happen during "
          "the policy period."),
        P("The amount we pay for damages is limited as described in the Limits of Insurance shown in the "
          "Declarations. Our duty to pay ends when we have used up the applicable limit in the payment of "
          "judgments or settlements."),
        P("Damages for bodily injury include damages claimed by any person or organization for care, loss "
          "of services or death that results at any time from the bodily injury."),
        P("No insured may, except at that insured's own cost, make a payment, assume an obligation or incur "
          "an expense without our consent, other than for first aid given to others at the time of an "
          "accident."),
        H("2. Defence and Settlement"),
        P("We have the right and the duty to defend the insured against any suit seeking damages covered by "
          "this policy, but we have no duty to defend the insured against any suit seeking damages for "
          "bodily injury or"),
    ],
    [
        P("property damage to which this policy does not apply. We may investigate any occurrence and "
          "settle any claim or suit at our discretion."),
        P("Defence costs that we pay are in addition to the Limits of Insurance. They do not reduce the "
          "Each Occurrence Limit or either aggregate limit."),
        H("SECTION II - EXCLUSIONS"),
        P("This policy does not apply to the following."),
        P("a. Expected or Intended Injury. Bodily injury or property damage that the insured expected or "
          "intended. This exclusion does not apply to bodily injury that results from the use of reasonable "
          "force to protect persons or property."),
        P("b. Roofing Height Limitation. Bodily injury or property damage arising out of roofing work "
          "performed on any building more than three storeys or 40 feet in height, whichever is lower."),
        P("c. Open Roof. Property damage to a building or its contents caused by rain, hail, snow or wind "
          "entering through a roof opening, unless the opening was protected by a secured waterproof "
          "covering whenever work was not in progress."),
        P("d. Torch-Applied Roofing. Property damage arising out of the use of an open flame or torch to "
          "apply roofing membrane, unless a fire watch was kept for at least 60 minutes after the flame "
          "was last used."),
        P("e. Workers Compensation. Any obligation of the insured under a workers compensation, disability "
          "benefits or unemployment compensation law."),
    ],
    [
        H("SECTION IV - CONDITIONS"),
        P("1. Cancellation. You may cancel this policy at any time by sending us written notice. We may "
          "cancel this policy by mailing written notice to the first Named Insured at least 30 days before "
          "the effective date of cancellation, or at least 10 days before that date if we cancel for "
          "nonpayment of premium."),
        P("2. Duties After an Occurrence. You must notify us in writing of any occurrence that may result "
          "in a claim as soon as practicable, and in any case within 15 days after you first become aware "
          "of it. The notice must say how, when and where the occurrence took place."),
        P("3. Subcontractors. You must obtain a certificate of insurance from every subcontractor before "
          "that subcontractor starts work. The certificate must show commercial general liability limits "
          "of at least $1,000,000 each occurrence. If you do not hold a certificate for a subcontractor, "
          "we will charge premium for that subcontractor's work at the rate shown in the Declarations."),
        P("4. Premium Audit. We will compute the earned premium at the end of each audit period from your "
          "payroll records. You must keep those records and send us copies when we ask for them."),
        P("5. Transfer of Rights. If the insured has rights to recover all or part of a payment we have "
          "made, those rights transfer to us. The insured must do nothing after a loss to impair them."),
    ],
    [
        H("ENDORSEMENT CG 21 47"),
        H("EMPLOYMENT-RELATED PRACTICES EXCLUSION"),
        P("This endorsement changes the policy. Please read it carefully. It modifies the insurance "
          "provided under the Commercial General Liability Coverage Form."),
        P("This policy does not apply to bodily injury or personal and advertising injury to a person "
          "arising out of a refusal to employ that person, the termination of that person's employment, or "
          "an employment-related practice directed at that person, such as demotion, evaluation, "
          "reassignment, discipline, harassment or discrimination."),
        P("This exclusion applies whether the insured may be liable as an employer or in any other "
          "capacity. It also applies to a claim by the spouse, child, parent or sibling of that person as "
          "a consequence of the injury."),
        P("All other terms and conditions of this policy remain unchanged."),
    ],
]

QUOTE_PAGES: list[list[Block]] = [
    [
        H("RENEWAL QUOTE - COMMERCIAL GENERAL LIABILITY"),
        KV("Insurer:", "Sample Mutual Insurance Company"),
        KV("Quote Number:", "SAMPLE-Q-2027-0042"),
        KV("Named Insured:", "Example Contractors LLC"),
        KV("Expiring Policy Number:", "SAMPLE-CGL-2026-0001"),
        KV("Producer:", "Example Brokerage Services"),
        P("Proposed Policy Period: From 01/01/2027 To 01/01/2028, beginning and ending at 12:01 a.m. "
          "standard time at the mailing address of the Named Insured."),
        KV("Quote Valid Until:", "12/15/2026"),
        P("We are pleased to offer renewal terms for the policy shown above. The terms on the following "
          "pages replace the expiring terms. Please review the changes on page 3 with your client before "
          "you ask us to bind coverage."),
    ],
    [
        H("PROPOSED LIMITS OF INSURANCE"),
        KV("Each Occurrence Limit", "$1,000,000"),
        KV("Damage to Premises Rented to You Limit", "$100,000 any one premises"),
        KV("Medical Expense Limit", "$10,000 any one person"),
        KV("Personal and Advertising Injury Limit", "$1,000,000"),
        KV("General Aggregate Limit", "$3,000,000"),
        KV("Products-Completed Operations Aggregate Limit", "$2,000,000"),
        H("PROPOSED DEDUCTIBLE"),
        KV("Property Damage Deductible", "$5,000 per occurrence"),
        H("PROPOSED PREMIUM"),
        KV("Estimated Annual Payroll", "$710,000"),
        KV("Rate per $1,000 of Payroll", "$30.85"),
        KV("Total Annual Premium", "$21,900"),
        P("The premium is an estimate based on the payroll shown. It is subject to audit at the end of the "
          "policy period."),
    ],
    [
        H("CHANGES FROM THE EXPIRING POLICY"),
        P("1. The General Aggregate Limit increases from $2,000,000 to $3,000,000."),
        P("2. The Medical Expense Limit increases from $5,000 to $10,000 any one person."),
        P("3. The Property Damage Deductible increases from $2,500 to $5,000 per occurrence."),
        P("4. The Total Annual Premium increases from $18,450 to $21,900."),
        P("5. Form SM 30 11, Silica or Silica-Related Dust Exclusion, is added. All other forms listed on "
          "the expiring policy continue to apply."),
        H("CONDITIONS OF THIS QUOTE"),
        P("We must receive the following before we can bind coverage: a signed renewal application, and "
          "currently valued loss runs for the past five years."),
        P("If the Named Insured has started roofing work on buildings more than three storeys in height, "
          "tell us before binding, because the expiring Roofing Height Limitation continues to apply."),
    ],
]

ENDORSEMENT_SCHEDULE = [
    ("Endorsement Number", "3"),
    ("Policy Number", "SAMPLE-CGL-2026-0001"),
    ("Named Insured", "Example Contractors LLC"),
    ("Endorsement Effective Date", "03/15/2026"),
    ("Additional Insured", "Example Property Holdings LLC"),
    ("Location of Covered Operations", "200 Sample Avenue, Sampletown, ST 00000"),
    ("Additional Premium", "$350"),
]

ENDORSEMENT_PARAGRAPHS = [
    "This endorsement changes the policy. Please read it carefully. It modifies the insurance provided under "
    "the Commercial General Liability Coverage Form.",
    "A. Who Is an Insured is amended to include as an additional insured the person or organization shown in "
    "the Schedule, but only with respect to liability for bodily injury or property damage caused, in whole "
    "or in part, by your acts or omissions, or by the acts or omissions of those acting on your behalf, in "
    "the performance of your ongoing operations for the additional insured at the location shown in the "
    "Schedule.",
    "B. The insurance provided to the additional insured applies only to the extent permitted by law. If a "
    "written contract requires you to provide this coverage, the insurance provided to the additional "
    "insured will not be broader than the coverage that the contract requires.",
    "C. This insurance does not apply to bodily injury or property damage that occurs after all work on the "
    "project, other than service, maintenance or repairs, has been completed at the location shown in the "
    "Schedule, or after the part of your work out of which the injury or damage arises has been put to its "
    "intended use by any person or organization other than another contractor on the same project.",
    "D. The most we will pay on behalf of the additional insured is the amount required by the written "
    "contract or the amount available under the Limits of Insurance shown in the Declarations, whichever is "
    "less. This endorsement does not increase the Limits of Insurance.",
    "E. The additional insured must give us written notice of an occurrence as soon as practicable, and must "
    "send us copies of any demand, notice, summons or legal paper received in connection with a claim or "
    "suit. The additional insured must also tender the defence of the claim to any other insurer that "
    "provides coverage to it for the same loss.",
    "F. Primary and Noncontributory. If a written contract requires it, this insurance is primary to and will "
    "not seek contribution from any other insurance available to the additional insured, provided that the "
    "additional insured is a Named Insured under that other insurance.",
    "G. Waiver of Transfer of Rights. We waive any right of recovery we may have against the additional "
    "insured because of payments we make for injury or damage arising out of your ongoing operations at the "
    "location shown in the Schedule, where a written contract requires the waiver.",
    "H. Notice of Cancellation to the Additional Insured. If we cancel this policy for any reason other than "
    "nonpayment of premium, we will mail written notice to the additional insured shown in the Schedule at "
    "least 20 days before the effective date of cancellation. Our failure to mail the notice does not extend "
    "the policy and does not create coverage.",
    "I. The additional premium shown in the Schedule is fully earned on the effective date of this "
    "endorsement and is not subject to adjustment at audit.",
    "J. This endorsement ends on the earliest of the following dates: the date the written contract ends, the "
    "date your operations at the location shown in the Schedule are completed, or the expiry date of this "
    "policy.",
    "All other terms and conditions of this policy remain unchanged.",
]


@dataclass(frozen=True)
class Fact:
    """One planted fact. `text` is on `page`, word for word.

    repeated_on: other pages that carry the same text (boilerplate).
    continues: (page, text) when the clause runs on from the bottom of `page`
    to the top of the next page.
    For a DOCX, `page` is the section number the extractor assigns.
    """

    id: str
    document: str
    page: int
    text: str
    kind: str
    repeated_on: tuple[int, ...] = ()
    continues: tuple[int, str] | None = None


FACTS = [
    Fact("policy-number", POLICY, 1, "Policy Number: SAMPLE-CGL-2026-0001", "identifier"),
    Fact("policy-named-insured", POLICY, 1, "Named Insured: Example Contractors LLC", "party"),
    Fact("policy-period", POLICY, 1, "Policy Period: From 01/01/2026 To 01/01/2027", "period"),
    Fact("policy-premium", POLICY, 1, "Total Advance Premium: $18,450", "premium"),
    Fact("policy-each-occurrence-limit", POLICY, 2, "Each Occurrence Limit $1,000,000", "limit"),
    Fact("policy-medical-expense-limit", POLICY, 2, "Medical Expense Limit $5,000 any one person", "limit"),
    Fact("policy-general-aggregate-limit", POLICY, 2, "General Aggregate Limit $2,000,000", "limit"),
    Fact("policy-products-aggregate-limit", POLICY, 2,
         "Products-Completed Operations Aggregate Limit $2,000,000", "limit"),
    Fact("policy-deductible", POLICY, 2, "Property Damage Deductible $2,500 per occurrence", "deductible"),
    Fact("policy-form-cg-21-47", POLICY, 3, "CG 21 47 Employment-Related Practices Exclusion", "form"),
    Fact("policy-defence-clause", POLICY, 4,
         "we have no duty to defend the insured against any suit seeking damages for bodily injury or",
         "page-spanning clause",
         continues=(5, "property damage to which this policy does not apply")),
    Fact("policy-roofing-height-exclusion", POLICY, 5,
         "roofing work performed on any building more than three storeys or 40 feet in height, whichever is lower",
         "exclusion"),
    Fact("policy-open-roof-exclusion", POLICY, 5,
         "unless the opening was protected by a secured waterproof covering whenever work was not in progress",
         "exclusion"),
    Fact("policy-cancellation-notice", POLICY, 6,
         "at least 30 days before the effective date of cancellation, or at least 10 days before that date if "
         "we cancel for nonpayment of premium", "condition"),
    Fact("policy-subcontractor-certificates", POLICY, 6,
         "commercial general liability limits of at least $1,000,000 each occurrence", "condition"),
    Fact("policy-employment-exclusion", POLICY, 7,
         "arising out of a refusal to employ that person, the termination of that person's employment",
         "exclusion"),
    Fact("policy-footer-boilerplate", POLICY, 2, POLICY_FOOTER, "repeated boilerplate",
         repeated_on=(3, 4, 5, 6, 7)),
    Fact("quote-number", QUOTE, 1, "Quote Number: SAMPLE-Q-2027-0042", "identifier"),
    Fact("quote-period", QUOTE, 1, "Proposed Policy Period: From 01/01/2027 To 01/01/2028", "period"),
    Fact("quote-each-occurrence-limit", QUOTE, 2, "Each Occurrence Limit $1,000,000", "limit"),
    Fact("quote-medical-expense-limit", QUOTE, 2, "Medical Expense Limit $10,000 any one person", "limit"),
    Fact("quote-general-aggregate-limit", QUOTE, 2, "General Aggregate Limit $3,000,000", "limit"),
    Fact("quote-deductible", QUOTE, 2, "Property Damage Deductible $5,000 per occurrence", "deductible"),
    Fact("quote-premium", QUOTE, 2, "Total Annual Premium $21,900", "premium"),
    Fact("quote-new-exclusion", QUOTE, 3, "Form SM 30 11, Silica or Silica-Related Dust Exclusion, is added",
         "exclusion"),
    Fact("quote-footer-boilerplate", QUOTE, 1, QUOTE_FOOTER, "repeated boilerplate", repeated_on=(2, 3)),
    Fact("endorsement-effective-date", ENDORSEMENT, 1, "Endorsement Effective Date | 03/15/2026", "period"),
    Fact("endorsement-additional-insured", ENDORSEMENT, 1, "Additional Insured | Example Property Holdings LLC",
         "party"),
    Fact("endorsement-additional-premium", ENDORSEMENT, 1, "Additional Premium | $350", "premium"),
    Fact("endorsement-cancellation-notice", ENDORSEMENT, 2,
         "at least 20 days before the effective date of cancellation", "condition"),
]

# Renewal changes a comparison question should surface, as (field, policy fact, quote fact).
CHANGES = [
    ("general aggregate limit", "policy-general-aggregate-limit", "quote-general-aggregate-limit"),
    ("medical expense limit", "policy-medical-expense-limit", "quote-medical-expense-limit"),
    ("property damage deductible", "policy-deductible", "quote-deductible"),
    ("premium", "policy-premium", "quote-premium"),
]
UNCHANGED = [("each occurrence limit", "policy-each-occurrence-limit", "quote-each-occurrence-limit")]

# Topics no sample mentions. A question about one must be refused, not answered.
ABSENT_TOPICS = ["flood", "earthquake", "cyber liability", "aircraft", "liquor liability"]

LEFT, RIGHT, TOP, BOTTOM = 72, 540, 730, 110
BODY_FONT, BOLD_FONT, SIZE, LEADING, GAP, TAB = "Helvetica", "Helvetica-Bold", 10.5, 14, 7, 250


def write_pdf(path: Path, title: str, pages: list[list[Block]], footer: str, footer_from: int) -> None:
    canvas = Canvas(str(path), pagesize=letter, invariant=1)
    canvas.setTitle(title)
    canvas.setAuthor("doc-vault sample generator")
    for number, blocks in enumerate(pages, start=1):
        canvas.setFont(BODY_FONT, 8)
        canvas.drawString(LEFT, 756, MARK)
        y = TOP
        for block in blocks:
            match block:
                case H(text):
                    y = _draw_lines(canvas, simpleSplit(text, BOLD_FONT, SIZE, RIGHT - LEFT), BOLD_FONT, LEFT, y)
                case P(text):
                    y = _draw_lines(canvas, simpleSplit(text, BODY_FONT, SIZE, RIGHT - LEFT), BODY_FONT, LEFT, y)
                case KV(label, value):
                    canvas.setFont(BODY_FONT, SIZE)
                    canvas.drawString(LEFT, y, label)
                    y = _draw_lines(canvas, simpleSplit(value, BODY_FONT, SIZE, RIGHT - LEFT - TAB), BODY_FONT,
                                    LEFT + TAB, y)
            y -= GAP
        if y < BOTTOM:
            raise SystemExit(f"{path.name} page {number} overflows; move a block to the next page")
        canvas.setFont(BODY_FONT, 8)
        if number >= footer_from:
            _draw_lines(canvas, simpleSplit(footer, BODY_FONT, 8, RIGHT - LEFT), BODY_FONT, LEFT, 84, size=8, leading=10)
        canvas.drawString(LEFT, 60, f"Page {number} of {len(pages)}")
        canvas.showPage()
    canvas.save()


def _draw_lines(canvas: Canvas, lines: list[str], font: str, x: float, y: float, *, size: float = SIZE,
                leading: float = LEADING) -> float:
    canvas.setFont(font, size)
    for line in lines:
        canvas.drawString(x, y, line)
        y -= leading
    return y


def write_docx(path: Path) -> None:
    doc = Document()
    props = doc.core_properties
    props.author = "doc-vault sample generator"
    props.title = "Sample endorsement"
    props.created = props.modified = FIXED_TIME
    doc.add_paragraph(MARK)
    doc.add_heading("ENDORSEMENT SM 40 03", level=1)
    doc.add_heading("ADDITIONAL INSURED - OWNERS, LESSEES OR CONTRACTORS", level=2)
    doc.add_paragraph("SCHEDULE")
    table = doc.add_table(rows=0, cols=2)
    for label, value in ENDORSEMENT_SCHEDULE:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = label, value
    for text in ENDORSEMENT_PARAGRAPHS:
        doc.add_paragraph(text)
    doc.add_paragraph(MARK)
    raw = io.BytesIO()
    doc.save(raw)
    # python-docx stamps each zip entry with the current time, which would change the content hash on every run.
    with zipfile.ZipFile(raw) as src, zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as dst:
        for name in sorted(src.namelist()):
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(info, src.read(name))


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def page_texts() -> dict[str, dict[int, str]]:
    """The text this script puts on each PDF page. DOCX sections are decided by the extractor, so they are
    checked by the test suite against the extracted text instead."""
    def pdf(pages: list[list[Block]], footer: str, footer_from: int) -> dict[int, str]:
        return {n: squash(" ".join([b.text for b in blocks] + ([footer] if n >= footer_from else [])))
                for n, blocks in enumerate(pages, start=1)}
    return {POLICY: pdf(POLICY_PAGES, POLICY_FOOTER, 2), QUOTE: pdf(QUOTE_PAGES, QUOTE_FOOTER, 1)}


def check_facts() -> None:
    drawn = page_texts()
    for fact in FACTS:
        if fact.document not in drawn:
            continue
        claims = [(fact.page, fact.text), *((p, fact.text) for p in fact.repeated_on)]
        if fact.continues:
            claims.append(fact.continues)
        for page, text in claims:
            if squash(text) not in drawn[fact.document][page]:
                raise SystemExit(f"fact {fact.id}: text is not on page {page} of {fact.document}")


def facts_yaml() -> str:
    by_id = {f.id: f for f in FACTS}

    def change(row: tuple[str, str, str]) -> dict[str, object]:
        field, policy_fact, quote_fact = row
        return {"field": field, "policy": policy_fact, "quote": quote_fact,
                "policy_text": by_id[policy_fact].text, "quote_text": by_id[quote_fact].text}

    def fact(f: Fact) -> dict[str, object]:
        out: dict[str, object] = {"id": f.id, "document": f.document, "page": f.page, "text": f.text, "kind": f.kind}
        if f.repeated_on:
            out["repeated_on"] = list(f.repeated_on)
        if f.continues:
            out["continues"] = {"page": f.continues[0], "text": f.continues[1]}
        return out

    data = {
        "documents": {
            POLICY: {"kind": "pdf", "page_basis": "physical", "pages": len(POLICY_PAGES)},
            QUOTE: {"kind": "pdf", "page_basis": "physical", "pages": len(QUOTE_PAGES)},
            ENDORSEMENT: {"kind": "docx", "page_basis": "synthetic", "pages": 2},
        },
        "facts": [fact(f) for f in FACTS],
        "changes": [change(row) for row in CHANGES],
        "unchanged": [change(row) for row in UNCHANGED],
        "absent_topics": ABSENT_TOPICS,
    }
    header = ("# Generated by scripts/make_samples.py. Do not edit by hand.\n"
              "# Each fact's text is on its page word for word. Whitespace may differ after extraction.\n"
              "# For the DOCX, page means the synthetic section number.\n")
    return header + yaml.safe_dump(data, sort_keys=False, width=1000)


def main(out: Path = OUT) -> None:
    check_facts()
    out.mkdir(parents=True, exist_ok=True)
    write_pdf(out / POLICY, "Sample commercial general liability policy", POLICY_PAGES, POLICY_FOOTER, 2)
    write_pdf(out / QUOTE, "Sample renewal quote", QUOTE_PAGES, QUOTE_FOOTER, 1)
    write_docx(out / ENDORSEMENT)
    (out / "facts.yaml").write_text(facts_yaml())


if __name__ == "__main__":
    main()

"""Builds the sample files in this folder. Run after changing it:

    .venv\\Scripts\\python tests\\fixtures\\make_fixtures.py

The test files compare against the dishes written here, so keep them in step.
"""

from __future__ import annotations

import csv
import zlib
from pathlib import Path

HERE = Path(__file__).parent
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# mess_menu_week.pdf: one dated week, days down the side, meals across the top.
WEEK_MENU = {
    "Monday": ("Aloo paratha, curd, pickle, tea", "Rajma, jeera rice, roti, salad",
               "Samosa, green chutney, tea", "Paneer butter masala, roti, dal, rice"),
    "Tuesday": ("Idli, sambar, coconut chutney, coffee", "Chole, bhature, onion salad",
                "Bread pakora, tea", "Egg curry, rice, roti, dal fry"),
    "Wednesday": ("Poha, boiled egg, banana, tea/coffee", "Kadhi pakora, rice, roti, aloo gobi",
                  "Veg sandwich, tea", "Chicken biryani, raita, gulab jamun"),
    "Thursday": ("Upma, peanut chutney, milk", "Dal makhani, rice, roti, mix veg",
                 "Bhel puri, tea", "Kadai paneer, roti, rice, dal"),
    "Friday": ("Masala dosa, sambar, chutney, coffee", "Veg pulao, raita, papad",
               "Pav bhaji, tea", "Fish curry, rice, roti, dal"),
    "Saturday": ("Chole kulche, tea", "Veg biryani, raita, salad", "Maggi, tea", "Malai kofta, roti, rice"),
    "Sunday": ("Puri, aloo sabzi, halwa, tea", "Mutton curry, rice, roti", "Pakode, tea",
               "Khichdi, kadhi, papad, pickle"),
}


def make_pdf() -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(HERE / "mess_menu_week.pdf"), pagesize=landscape(A4),
                            leftMargin=20, rightMargin=20, topMargin=30, bottomMargin=30)
    data = [["Day", "Breakfast (7:30-9:30)", "Lunch (12:30-2:30)", "Evening Snacks", "Dinner (7:30-9:30)"]]
    data += [[day, *meals] for day, meals in WEEK_MENU.items()]
    table = Table(data, colWidths=[70, 190, 190, 150, 200])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
    ]))
    doc.build([
        Paragraph("Hostel Mess Menu", styles["Title"]),
        Paragraph("Week: 9th Nov to 15th Nov 2026", styles["Normal"]),
        Spacer(1, 12),
        table,
        Spacer(1, 12),
        Paragraph("Mess timings may change on holidays.", styles["Normal"]),
    ])


# mess_menu_hostel.pdf: shaped like a real hostel menu that broke table detection:
# spaced-out headings ("B R E A K F A S T"), "MONDAY / Date-16/11/2026" labels, one dish
# per line, shaded rows, and an extra non-meal column ("PS11").
HOSTEL = {
    "MONDAY": (["Poha", "Boiled egg", "Tea/ Coffee/ Milk"], ["Rajma Masala", "White Rice/Chapatti", "Pickle / Yoghurt"],
               ["Chicken Curry", "Paneer Butter Masala", "White Rice / Pickle"], ["Samosa", "Tea/Coffee"],
               ["Dal tadka", "Jeera Rice"]),
    "TUESDAY": (["Idli / Sambar", "Coconut Chutney", "Bread/Butter/Jam"], ["Chole", "Bhatura", "Green Salad"],
                ["Egg Curry", "Dal Fry", "Chapatti"], ["Vada Pav", "Fresh Milk"], ["Paneer Tikka", "Pulao"]),
    "WEDNESDAY": (["Upma", "Fruit (max 2)"], ["Kadhi Pakoda", "White Rice"], ["Chicken Biryani", "Raitha"],
                  ["Muffin (2 Max)", "Tea /Coffee"], ["Chole Masala", "Daniya Pulao"]),
    "THURSDAY": (["Masala Dosa/ Sambar", "Tomato Chutney"], ["Malai Kofta", "Khichadi"], ["Mutton Curry", "Kheer"],
                 ["Onion Pakoda", "Lassi"], ["Kaju paneer", "Veg Pulao"]),
    "FRIDAY": (["Paratha (Wheat)", "Curd"], ["Mix Veg", "Mandi Rice"], ["Penne Pasta", "Gulab jamun"],
               ["Spring Roll", "Lemonade"], ["Dal Fry", "Pulao"]),
    "SATURDAY": (["Bread Pakora", "Pancakes"], ["Kadhai Paneer", "Rasam"], ["Veg Biryani", "Mirchi ka Salan"],
                 ["Punjabi Samosa", "Sweet Lassi"], ["", ""]),
    "SUNDAY": (["Puri Bhaji", "Halwa"], ["Special Lunch"], ["Aloo Beans", "Lemon Rice"], ["Rusk", "Tea Coffee"],
               ["", ""]),
}


def make_hostel_pdf() -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle

    def spaced(word: str) -> str:
        return " ".join(word)

    data = [["Week 1", spaced("BREAKFAST"), spaced("LUNCH"), spaced("DINNER"), spaced("SNACKS"), spaced("PS11")]]
    for i, (day, meals) in enumerate(HOSTEL.items()):
        data.append([f"{day}\nDate-{16 + i}/11/2026"] + ["\n".join(m) for m in meals])
    table = Table(data, colWidths=[90, 170, 170, 150, 110, 110])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 1), (-1, -1), 0.5, colors.black),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.mistyrose, colors.white]),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    SimpleDocTemplate(str(HERE / "mess_menu_hostel.pdf"), pagesize=landscape(A4),
                      leftMargin=20, rightMargin=20, topMargin=20, bottomMargin=20).build([table])


def make_png() -> None:
    """A 'photo' of the PDF menu, for the vision-model path."""
    import pdfplumber

    with pdfplumber.open(HERE / "mess_menu_week.pdf") as pdf:
        pdf.pages[0].to_image(resolution=150).save(HERE / "mess_menu_photo.png")


# mess_menu_rotating.xlsx: repeats every week, days across the top, meals down the side,
# a merged "Breakfast" label over two rows and one row merged across all seven days.
ROTATING = {
    "breakfast": ["Bread, butter, jam", "Aloo paratha, curd", "Idli, sambar, chutney", "Poha, jalebi",
                  "Upma, banana", "Dosa, chutney", "Chole bhature"],
    "lunch": ["Dal, rice, roti", "Rajma, rice", "Kadhi, rice", "Chole, rice", "Dal tadka, rice",
              "Veg biryani, raita", "Paneer, roti, rice"],
    "snacks": ["Samosa", "Biscuits", "Pakode", "Sandwich", "Maggi", "Bhel", "Cake"],
    "dinner": ["Mix veg, roti", "Egg curry, rice", "Aloo gobi, roti", "Paneer bhurji, roti",
               "Dal makhani, roti", "Fried rice, manchurian", "Chicken biryani\nRaita\nGulab jamun"],
}


def make_xlsx() -> None:
    import openpyxl
    from openpyxl.styles import Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Menu"
    ws["A1"] = "Regular Mess Menu (same every week)"
    ws.append([])
    ws.append(["Meal", *DAYS])                             # row 3
    ws.append(["Breakfast", *ROTATING["breakfast"]])       # row 4
    ws.append(["", "Tea, coffee"])                         # row 5, B5:H5 merged
    ws.append(["Lunch", *ROTATING["lunch"]])               # row 6
    ws.append(["Snacks (5-6 pm)", *ROTATING["snacks"]])    # row 7
    ws.append(["Dinner", *ROTATING["dinner"]])             # row 8
    ws.merge_cells("A4:A5")
    ws.merge_cells("B5:H5")
    ws["H8"].alignment = Alignment(wrap_text=True)
    wb.save(HERE / "mess_menu_rotating.xlsx")


def write_csv(name: str, rows: list[list[str]]) -> None:
    with open(HERE / name, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)


def make_csvs() -> None:
    # The week after the PDF, with the date in each day's label.
    write_csv("mess_menu_dated.csv", [
        ["Day", "Breakfast", "Lunch", "Dinner"],
        ["Mon 16/11", "Bread omelette, tea", "Dal, rice, roti", "Paneer tikka, roti"],
        ["Tue 17/11", "Paratha, curd", "Rajma, rice", "Egg bhurji, roti"],
        ["Wed 18/11", "Uttapam, chutney, tea", "Kadhi, rice", "Veg korma, roti"],
        ["Thu 19/11", "Poha, tea", "Chole, rice", "Dal fry, rice"],
        ["Fri 20/11", "Idli, sambar", "Veg pulao, raita", "Chicken curry, rice"],
        ["Sat 21/11", "Puri sabzi", "Biryani, raita", "Pasta"],
        ["Sun 22/11", "Aloo paratha", "Thali", "Khichdi"],
    ])
    # Replaces the PDF's week (sent later, so it wins where they overlap).
    write_csv("mess_menu_week_revised.csv", [
        ["Revised mess menu 9 Nov - 15 Nov 2026"],
        ["Day", "Breakfast", "Lunch", "Snacks", "Dinner"],
        *[[day, "Aloo poha, jalebi, tea" if day == "Wednesday" else WEEK_MENU[day][0], *WEEK_MENU[day][1:]]
          for day in DAYS],
    ])
    # No dates and no hint whether it repeats: Jarvis has to ask.
    write_csv("mess_menu_undated.csv", [
        ["Day", "Breakfast", "Lunch", "Dinner"],
        *[[day, f"{day} breakfast special", "Dal, rice", "Roti, sabzi"] for day in DAYS],
    ])
    write_csv("timetable.csv", [
        ["Day", "09:00-10:00", "10:00-11:00", "11:15-12:15"],
        ["Monday", "DBMS (Room 101)", "Operating Systems", "Maths"],
        ["Tuesday", "OS lab (Lab 3)", "OS lab (Lab 3)", "DBMS"],
        ["Wednesday", "Maths", "DBMS (Room 101)", "Free"],
        ["Thursday", "Operating Systems", "Maths", "DBMS tutorial"],
        ["Friday", "DBMS lab (Lab 2)", "DBMS lab (Lab 2)", "Operating Systems"],
    ])


def make_texts() -> None:
    (HERE / "hostel_notice.txt").write_text(
        "Hostel notice\n"
        "Laundry is collected on Tuesdays and Fridays before 9 am from the ground floor.\n"
        "The gym in block C is open 6 am to 10 pm.\n"
        "Guests must sign in at the warden's office.\n",
        encoding="utf-8",
    )
    (HERE / "exam_schedule.txt").write_text(
        "End-sem examination date sheet\n"
        "24 Nov 2026 - Database Management Systems, 10 am, Hall A\n"
        "26 Nov 2026 - Operating Systems, 10 am, Hall B\n"
        "28 Nov 2026 - Engineering Mathematics, 2 pm, Hall A\n",
        encoding="utf-8",
    )
    # A menu written as sentences: no grid, so the model has to read it.
    (HERE / "mess_menu_text.txt").write_text(
        "Mess menu for this week, same every week.\n"
        "Monday: breakfast is poori bhaji and tea, lunch is dal chawal, dinner is aloo matar with roti.\n"
        "Tuesday: breakfast is upma with coffee, lunch is rajma chawal, dinner is egg curry with rice.\n"
        "Wednesday: breakfast is masala dosa, lunch is kadhi chawal, dinner is paneer do pyaza with roti.\n"
        "Thursday: breakfast is idli sambar, lunch is chole rice, dinner is mix veg with roti.\n",
        encoding="utf-8",
    )


OBSERVANCE = "Observance\\nTo hide observances\\, go to Google Calendar Settings > Holidays in India"
PUBLIC = "Public holiday"
TENTATIVE = "Public holiday\\nDate is tentative and may change"

# Shaped like Google's public holiday calendars (holidays.py), 2025-2028.
INDIA = [
    ("20251020", "Diwali/Deepavali", PUBLIC),        # last year: ignored
    ("20260101", "New Year's Day", OBSERVANCE),
    ("20260304", "Holi", PUBLIC),
    ("20261108", "Diwali/Deepavali", PUBLIC),
    ("20261108", "Naraka Chaturdasi", OBSERVANCE),
    ("20261109", "Govardhan Puja", OBSERVANCE),
    ("20261224", "Christmas Eve", OBSERVANCE),
    ("20261225", "Christmas", PUBLIC),
    ("20271029", "Diwali/Deepavali", PUBLIC),
    ("20271225", "Christmas", PUBLIC),
    ("20281017", "Diwali/Deepavali", PUBLIC),        # two years ahead: ignored
]
UAE = [
    ("20260101", "New Year's Day", PUBLIC),          # also in the India list
    ("20260320", "Eid al-Fitr", TENTATIVE),
    ("20261202", "National Day", PUBLIC),
    ("20270310", "Eid al-Fitr", TENTATIVE),
]


def ics(name: str, events: list[tuple[str, str, str]]) -> str:
    from datetime import date, timedelta

    lines = ["BEGIN:VCALENDAR", "PRODID:-//Google Inc//Google Calendar 70.9054//EN", "VERSION:2.0",
             f"X-WR-CALNAME:Holidays in {name}"]
    for start, title, about in events:
        end = (date(int(start[:4]), int(start[4:6]), int(start[6:])) + timedelta(days=1)).strftime("%Y%m%d")
        lines += ["BEGIN:VEVENT", f"DTSTART;VALUE=DATE:{start}", f"DTEND;VALUE=DATE:{end}",
                  "DTSTAMP:20261101T000000Z", f"UID:{start}_{zlib.crc32(title.encode())}@google.com",
                  "CLASS:PUBLIC", "CREATED:20250101T000000Z",
                  f"DESCRIPTION:{about}", "STATUS:CONFIRMED", "TRANSP:TRANSPARENT"]
        summary = f"SUMMARY:{title}"
        # long lines are folded: CRLF plus a space continues the line (RFC 5545)
        lines += [summary[:12], " " + summary[12:]] if len(summary) > 20 else [summary]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def make_holidays() -> None:
    (HERE / "holidays_india.ics").write_bytes(ics("India", INDIA).encode())
    (HERE / "holidays_uae.ics").write_bytes(ics("United Arab Emirates", UAE).encode())


if __name__ == "__main__":
    make_holidays()
    make_pdf()
    make_png()
    make_xlsx()
    make_csvs()
    make_texts()
    print("fixtures written to", HERE)

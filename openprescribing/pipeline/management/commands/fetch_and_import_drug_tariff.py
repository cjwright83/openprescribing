"""
Fetches Drug Tariff from NHSBSA website, and saves to CSV
"""
from io import BytesIO
import datetime
from urllib.parse import unquote, urljoin
import logging
import os
import re
import requests

import bs4
import calendar
from openpyxl import load_workbook

from django.core.management import BaseCommand
from django.db import transaction

from gcutils.bigquery import Client
from frontend.models import TariffPrice
from frontend.models import ImportLog
from openprescribing.slack import notify_slack


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        url = "https://www.nhsbsa.nhs.uk/pharmacies-gp-practices-and-appliance-contractors/drug-tariff/drug-tariff-part-viii/"
        rsp = requests.get(url)
        doc = bs4.BeautifulSoup(rsp.content, "html.parser")

        month_abbrs = [x.lower() for x in calendar.month_abbr]

        imported_months = []

        for a in doc.findAll("a", href=re.compile(r"Part%20VIIIA.+\.xlsx$")):
            # a.attrs['href'] typically has a filename part like
            # Part%20VIIIA%20September%202017.xlsx
            #
            # We split that into ['Part', 'VIIIA', 'September', '2017']
            base_filename = unquote(
                os.path.splitext(os.path.basename(a.attrs["href"]))[0]
            )

            if base_filename == "Part VIIIA Nov 20 updated":
                # November 2020 has a different filename.  In general we want to be
                # warned (through the scraper crashing) about updates (because we have
                # to delete all records for the month in question, and reimport) so
                # special-casing is appropriate here.
                year, month = "2020", 11

            else:
                words = re.split(r"[ -]+", base_filename)
                month_name, year = words[-2:]

                # We have seen the last token in `words` be "19_0".  The year is
                # reported to us via Slack, so if we pull out some nonsense here we
                # *should* notice.
                year = re.match("\d+", year).group()
                if len(year) == 2:
                    year = "20" + year

                # We have seen the month be `September`, `Sept`, and `Sep`.
                month_abbr = month_name.lower()[:3]
                month = month_abbrs.index(month_abbr)

            date = datetime.date(int(year), month, 1)
            if ImportLog.objects.filter(category="tariff", current_at=date).exists():
                continue

            xls_url = urljoin(url, a.attrs["href"])
            xls_file = BytesIO(requests.get(xls_url).content)

            import_month(xls_file, date)
            imported_months.append((year, month))

        if imported_months:
            client = Client("dmd")
            client.upload_model(TariffPrice)

            for year, month in imported_months:
                msg = "Imported Drug Tariff for %s_%s" % (year, month)
                notify_slack(msg)
        else:
            msg = "Found no new tariff data to import"
            notify_slack(msg)


def import_month(xls_file, date):
    wb = load_workbook(xls_file)
    rows = wb.active.rows

    # The first row is a title, and the second is empty
    next(rows)
    next(rows)

    # The third row is column headings
    header_row = next(rows)
    headers = ["".join((c.value or "?").lower().split()) for c in header_row]
    assert headers == [
        "medicine",
        "packsize",
        "?",
        "vmppsnomedcode",
        "drugtariffcategory",
        "basicprice",
    ]

    with transaction.atomic():
        for row in rows:
            values = [c.value for c in row]
            if all(v is None for v in values):
                continue

            d = dict(zip(headers, values))

            if d["basicprice"] is None:
                msg = "Missing price for {} Drug Tariff for {}".format(
                    d["medicine"], date
                )
                notify_slack(msg)
                continue

            TariffPrice.objects.get_or_create(
                date=date,
                vmpp_id=d["vmppsnomedcode"],
                tariff_category_id=get_tariff_cat_id(d["drugtariffcategory"]),
                price_pence=int(d["basicprice"]),
            )

        ImportLog.objects.create(category="tariff", current_at=date, filename="none")


def get_tariff_cat_id(cat):
    if "Category A" in cat:
        return 1
    elif "Category C" in cat:
        return 3
    elif "Category M" in cat:
        return 11
    else:
        assert False, "Unknown category: {}".format(cat)

"""Read the departure printed on a railway ticket, independent of the issue date."""
from datetime import date, time
import re
from unicodedata import normalize


DATE = r'(\d{4})\s*[年/-]\s*(\d{1,2})\s*[月/-]\s*(\d{1,2})\s*日?'
CLOCK = r'(\d{1,2})\s*[:：]\s*(\d{2})'
DEPARTURE = re.compile(DATE + r'\s+' + CLOCK + r'\s*(?:发车|开(?:车)?(?!票))')
LABELLED_DATE = re.compile(r'乘车日期\s*:?\s*' + DATE)
LABELLED_TIME = re.compile(r'发车时间\s*:?\s*' + CLOCK)
ROUTE = re.compile(r'^([\u3400-\u9fff· ]{1,32}?)\s*([GDCZTKYS]\s*\d{1,5})\s*([\u3400-\u9fff· ]{1,32}?)$')


def rail_travel_details(text):
    text = normalize('NFKC', text or '')
    departure = DEPARTURE.search(text)
    date_match = departure or LABELLED_DATE.search(text)
    time_match = departure or LABELLED_TIME.search(text)
    travel_date = travel_time = ''
    if date_match:
        try:
            travel_date = date(*map(int, date_match.groups()[:3])).isoformat()
        except ValueError:
            pass
    if time_match:
        try:
            travel_time = time(*map(int, time_match.groups()[-2:])).isoformat(timespec='minutes')
        except ValueError:
            pass
    route_text = text[:departure.start()] if departure else text
    for line in reversed(route_text.splitlines()[-8:]):
        route = ROUTE.fullmatch(line.strip())
        if route:
            origin, train, destination = (field.replace(' ', '') for field in route.groups())
            return dict(date=travel_date, time=travel_time, train=train,
                        origin=origin, destination=destination)
    return dict(date=travel_date, time=travel_time, train='', origin='', destination='')

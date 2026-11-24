import unittest

from rail_travel import rail_travel_details


class RailTravelTests(unittest.TestCase):
    def test_trip_departure_and_stations_not_invoice_date(self):
        text = (
            '电子发票（铁路电子客票）\n开票日期：2026年11月30日\n'
            '甲城东 Ｇ1234 乙城西\n站 站\n2026年11月24日 8：21开 04车12C号'
        )
        self.assertEqual(rail_travel_details(text), dict(
            date='2026-11-24', time='08:21', train='G1234', origin='甲城东', destination='乙城西',
        ))

    def test_missing_departure_time_is_empty_and_issue_date_is_not_used(self):
        self.assertEqual(rail_travel_details('乘车日期：2026-11-24')['date'], '2026-11-24')
        for text in ('开票日期：2026年11月30日 09:01开票', '2026年02月30日 27:22开'):
            with self.subTest(text=text):
                result = rail_travel_details(text)
                self.assertEqual((result['date'], result['time']), ('', ''))

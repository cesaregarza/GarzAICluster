import importlib.util
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / 'helm/splattop/files/sendou-proxy/sendou_response.py'
SPEC = importlib.util.spec_from_file_location('sendou_response', MODULE_PATH)
response = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(response)


class SendouResponseTests(unittest.TestCase):
    def test_reported_match_uses_new_route_key_and_keeps_its_data(self):
        match = {'data': {'match': {'id': 160207}, 'teams': [], 'matchIsOver': True}}
        self.assertEqual(response.extract_route({'root': {'data': {}}, response.MATCH_ROUTE: match}, response.MATCH_ROUTE), match)

    def test_each_existing_route_accepts_plain_json(self):
        for route in (response.MATCH_ROUTE, response.TOURNAMENT_ROUTE, response.TEAM_ROUTE, response.QUEUE_ROUTE):
            with self.subTest(route=route):
                data = {'data': {'id': 1}}
                self.assertEqual(response.extract_route({route: data}, route), data)

    def test_previous_match_route_name_is_still_accepted_for_json(self):
        self.assertEqual(response.extract_route({response.MATCH_ROUTE_ALIAS: {'data': {'id': 1}}}, response.MATCH_ROUTE),
                         {'data': {'id': 1}})

    def test_exact_route_takes_precedence_over_alias(self):
        self.assertEqual(response.extract_route({response.MATCH_ROUTE: {'data': {'id': 1}},
                                                 response.MATCH_ROUTE_ALIAS: {'data': {'id': 2}}}, response.MATCH_ROUTE),
                         {'data': {'id': 1}})

    def test_data_envelope_is_preserved(self):
        data = {'data': {'match': {'id': 1}}, 'headers': {'x-example': 'value'}}
        self.assertEqual(response.extract_route({response.MATCH_ROUTE: data}, response.MATCH_ROUTE), data)

    def test_missing_route_is_rejected_instead_of_returning_other_loaders(self):
        with self.assertRaises(ValueError):
            response.extract_route({'root': {'data': {}}}, response.MATCH_ROUTE)

    def test_invalid_route_envelopes_are_rejected(self):
        for value in (None, [], 'invalid', {'error': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                response.extract_route({response.MATCH_ROUTE: value}, response.MATCH_ROUTE)

    def test_non_object_payloads_are_rejected(self):
        for value in (None, True, 1, 'invalid', []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                response.extract_route(value, response.MATCH_ROUTE)

    def test_team_list_stays_a_flat_array(self):
        teams = [{'id': 99524}, {'id': 99713}]
        self.assertEqual(response.extract_teams({'data': {'tournament': {'ctx': {'teams': teams}}}}), teams)

    def test_invalid_team_list_is_rejected(self):
        for value in ({}, {'data': None}, {'data': {'tournament': {'ctx': {'teams': {}}}}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                response.extract_teams(value)

    def test_tournament_list_requests_only_its_parent_loader(self):
        self.assertEqual(response.loader_url('https://sendou.ink/to/4478.data'),
                         'https://sendou.ink/to/4478.data?_routes=features%2Ftournament%2Froutes%2Fto.%24id')

    def test_match_team_and_queue_urls_are_preserved(self):
        for url in ('https://sendou.ink/to/4478/matches/160207.data',
                    'https://sendou.ink/to/4478/teams/99524.data', 'https://sendou.ink/q/match/1.data'):
            with self.subTest(url=url):
                self.assertEqual(response.loader_url(url), url)


if __name__ == '__main__':
    unittest.main()

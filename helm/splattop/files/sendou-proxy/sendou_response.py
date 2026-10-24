"""Select route data from Sendou's JSON loader responses."""

import re
from urllib.parse import quote

TOURNAMENT_ROUTE = "features/tournament/routes/to.$id"
MATCH_ROUTE = "features/tournament-match/routes/to.$id.matches.$mid"
MATCH_ROUTE_ALIAS = "features/tournament-bracket/routes/to.$id.matches.$mid"
TEAM_ROUTE = "features/tournament/routes/to.$id.teams.$tid"
QUEUE_ROUTE = "features/sendouq-match/routes/q.match.$id"


def loader_url(url):
    """Load the parent tournament data without its index route's redirect."""
    if re.fullmatch(r"https://sendou\.ink/to/[0-9]+\.data", url):
        return url + "?_routes=" + quote(TOURNAMENT_ROUTE, safe="")
    return url


def extract_route(payload, route_key):
    """Return the requested loader's data envelope, excluding other loaders."""
    if not isinstance(payload, dict):
        raise ValueError("Expected a Sendou JSON object")
    candidates = [route_key]
    if route_key == MATCH_ROUTE:
        candidates.append(MATCH_ROUTE_ALIAS)
    for candidate in candidates:
        if candidate in payload:
            route_data = payload[candidate]
            if not isinstance(route_data, dict) or "data" not in route_data:
                raise ValueError("Sendou route response is missing its data envelope")
            return route_data
    raise ValueError("Requested Sendou route is missing from the response")


def extract_teams(route_data):
    """Preserve the existing team-list endpoint's flat array response."""
    try:
        teams = route_data["data"]["tournament"]["ctx"]["teams"]
    except (KeyError, TypeError) as error:
        raise ValueError("Sendou tournament response is missing its teams") from error
    if not isinstance(teams, list):
        raise ValueError("Expected a Sendou tournament team list")
    return teams

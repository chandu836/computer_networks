from common.models import RouteDecision
from routing.queue_aware_routing import (
    find_candidate_paths,
    rank_candidate_paths,
)

def select_path(
    graph,
    source,
    destination,
    queue_source,
    use_smoothed=False,
    missing_delay_ms=10.0,
    failed_links=None,
    failed_switches=None,
):
    """
    plain queue-aware baseline: pick the operational path with the
    lowest total estimated queueing delay (no flowlets, no hysteresis).
    """

    decision = select_decision(
        graph,
        source=source,
        destination=destination,
        queue_source=queue_source,
        use_smoothed=use_smoothed,
        missing_delay_ms=missing_delay_ms,
        failed_links=failed_links,
        failed_switches=failed_switches,
    )

    return decision.path if decision is not None else None

def select_decision(
    graph,
    source,
    destination,
    queue_source,
    use_smoothed=False,
    missing_delay_ms=10.0,
    failed_links=None,
    failed_switches=None,
):
    """
    plain queue-aware baseline returning a route decision with cost.
    """

    candidates = find_candidate_paths(
        graph,
        source=source,
        destination=destination,
        failed_links=failed_links,
        failed_switches=failed_switches,
    )

    if not candidates:
        return None

    # pure queueing delay cost: sum of link delays, ties broken by hop count.
    ranked = rank_candidate_paths(
        candidates,
        queue_source,
        use_smoothed=use_smoothed,
        utilization_weight=0.0,
        hop_weight=0.0,
        missing_delay_ms=missing_delay_ms,
    )

    best_path, best_cost = ranked[0]

    return RouteDecision(
        path=list(best_path),
        cost=best_cost,
        reason="baseline_lowest_delay",
    )
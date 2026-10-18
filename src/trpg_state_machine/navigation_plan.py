"""Static granted endpoint composition, reusing RE local pathfinding verbatim."""

import heapq
from dataclasses import asdict

from ._operations import StepActor, TraversePassage
from .errors import StateMachineError
from .grid import GridService, cell_in_bounds


def granted_layout(row, actor, package):
    knowledge = package.navigation
    if knowledge is None:
        raise StateMachineError(
            "navigation_knowledge_unavailable", "No approved navigation knowledge"
        )
    grants = (
        dict(knowledge.pc_template_grants).get(row["pc_template_id"], ())
        if actor == row["pc_actor_id"]
        else dict(knowledge.actor_grants).get(actor, ())
    )
    regions = [r for r in knowledge.regions if r.region_id in grants]
    scenes = {s for r in regions for s in r.scene_ids}
    passages = {p for r in regions for p in r.passage_ids}
    return scenes, passages


def plan_navigation(row, actor, position, operation, package):
    scenes, passage_ids = granted_layout(row, actor, package)
    if position.scene_id not in scenes or operation.target_scene_id not in scenes:
        raise StateMachineError(
            "navigation_knowledge_unavailable", "Target is outside approved knowledge"
        )
    topologies = {sid: GridService.package_topology(package, sid) for sid in sorted(scenes)}
    target = (operation.target_scene_id, operation.target_cell)
    start = (position.scene_id, position.cell)
    if not cell_in_bounds(topologies[target[0]], target[1]) or not topologies[
        target[0]
    ].is_valid_cell(target[1]):
        raise StateMachineError("invalid_navigation_target", "Expected an approved walkable Cell")
    if not cell_in_bounds(topologies[start[0]], start[1]):
        raise StateMachineError("invalid_spatial_state", "Actor needs a current Cell")
    # Only the approved connection graph and static grids enter this search.
    ports = {}
    for passage in sorted(package.passages, key=lambda p: p.passage_id):
        if passage.passage_id not in passage_ids:
            continue
        for endpoint in passage.endpoints:
            other = next(e for e in passage.endpoints if e.scene_id != endpoint.scene_id)
            if (
                dict(package.scene(endpoint.scene_id).exit_passages).get(other.scene_id)
                == passage.passage_id
            ):
                ports.setdefault(endpoint.scene_id, []).append((passage, endpoint, other))
    queue = [(0, start)]
    best = {start: 0}
    paths = {start: []}
    pruned = False
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != best[node]:
            continue
        if node == target:
            return paths[node]
        choices = []
        if node[0] == target[0]:
            choices.append((target, None, target[1]))
        choices.extend(
            ((other.scene_id, other.standing_cell), passage, endpoint.standing_cell)
            for passage, endpoint, other in ports.get(node[0], ())
        )
        for destination, passage, standing in choices:
            local = topologies[node[0]].shortest_path(node[1], standing)
            if not local:
                continue
            legs = [
                {"kind": "step", **asdict(StepActor(node[0], a, b))}
                for a, b in zip(local[:-1], local[1:], strict=True)
            ]
            if passage:
                legs.append(
                    {
                        "kind": "passage",
                        **asdict(TraversePassage(passage.passage_id, node[0], standing)),
                    }
                )
            total = cost + len(legs)
            if total < best.get(destination, 1_000_001):
                if total > 4096:
                    pruned = True
                    continue
                best[destination] = total
                paths[destination] = paths[node] + legs
                heapq.heappush(queue, (total, destination))
    if pruned:
        raise StateMachineError("navigation_plan_limit", "No route within bounded step count")
    raise StateMachineError("navigation_route_unavailable", "No route in approved static knowledge")
